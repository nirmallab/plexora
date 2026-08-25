"""The HTTP surface, including the parts the client acts on differently.

The status codes are the contract, not decoration:

    400  the request is wrong; retrying it unchanged fails the same way.
    404  no such figure -- probably deleted, quite possibly in the other tab.
    409  the request was fine but somebody else saved first, and the caller has
         work worth keeping, so the client asks rather than discarding.
    422  the stored figure cannot be read by this build.

A surface that answered 400 to all four would be describing every failure as the
client's fault, and the client would have no way to tell a conflict worth
prompting about from a malformed operation.

The two PAGES are here too, because they are the thing that makes this plugin
different: a figure spans datasources, so it has to be openable with no project
loaded at all.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest

import plexora
from plexora.plugins.figure_builder.server import repository
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model, database_model
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

API = "/plugins/figure_builder/api"
STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app with a data_path of its own.

    `plexora.paths` is what the figure store resolves on every call, and
    `config_json_path` is what the source routes read -- both redirected here so
    a test can neither see nor touch the user's own figures.

    The plugin is not installed here: `create_app` already did that at import
    time, and Flask refuses to register the same blueprint twice. Skipped rather
    than failed when it is absent, so a deliberately core-only run does not
    report this file as broken.
    """
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demo": project("demo", image=image_spec(channels=("DNA", "CD3"),
                                                 width=4000, height=3000),
                        confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")

    if plugin_registry.find(plexora.app, "figure_builder") is None:  # pragma: no cover
        pytest.skip("figure_builder is not installed")
    return plexora.app.test_client()


def create(client, title="Figure 1"):
    response = client.post(f"{API}/figures", json={"title": title})
    assert response.status_code == 200
    return response.get_json()["figure_id"]


# -- the library --------------------------------------------------------

def test_the_library_is_readable_with_no_project_open(client):
    """The claim the whole plugin rests on. A figure spans datasources, so
    requiring one before the library can be read would make "reopen my figure"
    mean "guess which of its four images you meant"."""
    response = client.get(f"{API}/figures")
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "figures": []}


def test_a_figure_can_be_created_and_read_back(client):
    figure_id = create(client, "Figure 1")
    body = client.get(f"{API}/figures/{figure_id}").get_json()
    assert body["document"]["title"] == "Figure 1"
    assert body["document"]["revision"] == 0
    assert body["source_status"] == {}


def test_a_figure_that_does_not_exist_is_404_not_400(client):
    """The library shows the two differently: a deleted figure is not a bad
    request, and telling the user to check their input would be wrong."""
    response = client.get(f"{API}/figures/fig_deadbeefcafe")
    assert response.status_code == 404
    assert response.get_json()["error"] == "unknown_figure"


def test_a_figure_id_that_could_be_a_path_is_refused(client):
    """This value is joined onto a filesystem path. Werkzeug's own routing
    normalises most of it away, so this is the belt to that braces."""
    for bad in ("..", "fig_..%2Fx", "config"):
        response = client.get(f"{API}/figures/{bad}")
        assert response.status_code in (400, 404), bad


# -- editing ------------------------------------------------------------

def test_an_edit_against_the_current_revision_is_applied(client):
    figure_id = create(client)
    response = client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0,
        "operations": [{"op": "set_meta", "changes": {"title": "Renamed"}}]})
    assert response.status_code == 200
    assert response.get_json()["revision"] == 1


def test_a_stale_edit_is_409_and_says_what_the_revision_now_is(client):
    """The client needs the number to decide what to offer -- reload, or keep
    mine -- so a bare 409 would leave it unable to say anything useful."""
    figure_id = create(client)
    client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0, "operations": [{"op": "set_meta", "changes": {"title": "First"}}]})

    response = client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0, "operations": [{"op": "set_meta", "changes": {"title": "Stale"}}]})
    assert response.status_code == 409
    assert response.get_json() == {"success": False, "error": "stale_revision", "revision": 1}


def test_a_bad_operation_is_400_and_names_what_was_wrong(client):
    figure_id = create(client)
    response = client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0, "operations": [{"op": "nonsense"}]})
    assert response.status_code == 400
    assert "nonsense" in response.get_json()["error"]


def test_a_body_that_is_not_an_object_is_400(client):
    figure_id = create(client)
    response = client.patch(f"{API}/figures/{figure_id}", json=[1, 2, 3])
    assert response.status_code == 400


def test_a_damaged_figure_is_422_rather_than_500(client):
    """Distinct from every other failure because the client must not draw or
    write anything until the user has been told -- an autosave over a document
    that could not be read is how "damaged" becomes "gone"."""
    figure_id = create(client)
    _corrupt(plexora.paths.data_root(), figure_id)

    response = client.get(f"{API}/figures/{figure_id}")
    assert response.status_code == 422
    assert response.get_json()["error"] == "unreadable_figure"

    response = client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0, "operations": [{"op": "set_meta", "changes": {"title": "x"}}]})
    assert response.status_code == 422


def test_a_figure_can_be_deleted_and_then_reads_as_gone(client):
    figure_id = create(client)
    assert client.delete(f"{API}/figures/{figure_id}").status_code == 200
    assert client.get(f"{API}/figures/{figure_id}").status_code == 404


def test_a_figure_can_be_duplicated(client):
    figure_id = create(client, "Figure 1")
    response = client.post(f"{API}/figures/{figure_id}/duplicate", json={})
    assert response.status_code == 200
    copy_id = response.get_json()["figure_id"]
    assert copy_id != figure_id
    assert client.get(f"{API}/figures/{copy_id}").get_json()["document"]["title"] == "Figure 1 copy"


# -- rasters ------------------------------------------------------------

def test_a_preview_round_trips_as_bytes(client):
    """Sent as the image itself rather than as a base64 field: base64 would add
    a third to every panel the user captures."""
    figure_id = create(client)
    response = client.post(
        f"{API}/figures/{figure_id}/previews/pnl_1?render_revision=1&width=64&height=48",
        data=b"webp-bytes", content_type="image/webp")
    assert response.status_code == 200
    assert response.get_json()["stored"] is True

    response = client.get(f"{API}/figures/{figure_id}/previews/pnl_1")
    assert response.status_code == 200
    assert response.get_data() == b"webp-bytes"
    assert response.mimetype == "image/webp"


def test_a_stale_render_is_accepted_but_not_stored(client):
    """Not an error: a render that lost the race did nothing wrong, and a 4xx
    here would put a failure in front of the user for something they cannot act
    on. `stored: false` is the honest answer."""
    figure_id = create(client)
    client.post(f"{API}/figures/{figure_id}/previews/pnl_1?render_revision=5",
                data=b"newer", content_type="image/webp")
    response = client.post(f"{API}/figures/{figure_id}/previews/pnl_1?render_revision=2",
                           data=b"older", content_type="image/webp")
    assert response.status_code == 200
    assert response.get_json()["stored"] is False
    assert client.get(f"{API}/figures/{figure_id}/previews/pnl_1").get_data() == b"newer"


def test_a_preview_that_was_never_rendered_is_404(client):
    figure_id = create(client)
    assert client.get(f"{API}/figures/{figure_id}/previews/pnl_1").status_code == 404


def test_previews_are_revalidated_rather_than_cached(client):
    """A preview is replaced in place, at the same URL, whenever its panel
    changes -- a cached copy would show the user the view they just edited away
    from."""
    figure_id = create(client)
    client.post(f"{API}/figures/{figure_id}/previews/pnl_1?render_revision=1",
                data=b"x", content_type="image/webp")
    response = client.get(f"{API}/figures/{figure_id}/previews/pnl_1")
    assert "no-cache" in response.headers["Cache-Control"]


def test_a_thumbnail_round_trips(client):
    figure_id = create(client)
    assert client.put(f"{API}/figures/{figure_id}/thumbnail",
                      data=b"thumb", content_type="image/webp").status_code == 200
    assert client.get(f"{API}/figures/{figure_id}/thumbnail").get_data() == b"thumb"
    assert client.get(f"{API}/figures").get_json()["figures"][0]["has_thumbnail"] is True


# -- imported assets ----------------------------------------------------

def test_an_image_can_be_imported_into_a_figure_only(client):
    """A schematic is not a project, and making the user create one to drop a
    PNG into a figure is exactly the setup step this plugin exists to remove."""
    figure_id = create(client)
    response = client.post(f"{API}/figures/{figure_id}/assets?filename=schematic.png",
                           data=b"\x89PNG fake", content_type="image/png")
    assert response.status_code == 200
    asset_id = response.get_json()["asset_id"]

    served = client.get(f"{API}/figures/{figure_id}/assets/{asset_id}")
    assert served.status_code == 200
    assert served.get_data() == b"\x89PNG fake"


def test_importing_something_that_is_not_an_image_is_refused(client):
    figure_id = create(client)
    response = client.post(f"{API}/figures/{figure_id}/assets?filename=notes.txt",
                           data=b"hello", content_type="text/plain")
    assert response.status_code == 400


# -- sources ------------------------------------------------------------

def test_a_project_can_be_described_as_a_figure_source(client):
    response = client.get(f"{API}/sources/demo")
    assert response.status_code == 200
    source = response.get_json()["source"]
    assert source["datasource"] == "demo"
    assert source["image"] == {"width": 4000, "height": 3000}
    # Channels are identified by their URL key, which survives a rename --
    # `fullname` is precisely what does not.
    assert [c["key"] for c in source["channels"]] == ["DNA", "CD3"]
    assert source["fingerprint"]["channel_keys"] == ["DNA", "CD3"]


def test_describing_a_project_that_does_not_exist_is_404(client):
    assert client.get(f"{API}/sources/nope").status_code == 404


def test_a_source_that_changed_underneath_a_figure_is_reported(client, monkeypatch):
    """Never silently rerendered. Panels drawn on the old image render
    perfectly plausibly on the new one, in the wrong places, and nothing about
    the pixels says otherwise."""
    figure_id = create(client)
    client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0,
        "operations": [{"op": "add_source", "source": {
            "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
            "image": {"width": 4000, "height": 3000},
            "fingerprint": {"image_width": 4000, "image_height": 3000,
                            "channel_keys": ["DNA", "CD3"], "has_segmentation": False}}}]})

    # The project is re-imported with a different slide.
    (plexora.paths.config_path()).write_text(json.dumps({
        "demo": project("demo", image=image_spec(channels=("DNA", "CD3"),
                                                 width=9999, height=8888),
                        confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")

    status = client.get(f"{API}/figures/{figure_id}").get_json()["source_status"]["src_1"]
    assert status["status"] == "changed"
    assert "dimensions_changed" in status["reasons"]


def test_a_source_whose_project_is_gone_is_reported_as_missing(client):
    figure_id = create(client)
    client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0,
        "operations": [{"op": "add_source", "source": {
            "source_id": "src_1", "kind": "plexora_project", "datasource": "deleted_project",
            "image": {"width": 10, "height": 10}}}]})

    status = client.get(f"{API}/figures/{figure_id}").get_json()["source_status"]["src_1"]
    assert status["status"] == "missing"
    assert status["reasons"] == ["no_such_project"]


# -- export -------------------------------------------------------------

def _panel_on_a_page(client, figure_id):
    return client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0,
        "operations": [
            {"op": "add_source", "source": {
                "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
                "image": {"width": 4000, "height": 3000}}},
            {"op": "add_panel", "panel": {
                "panel_id": "pnl_1", "source_id": "src_1",
                "scene": {"viewport": {"x": 0, "y": 0, "w": 400, "h": 300}, "channels": []},
                "placement": {"page_id": "pg_1", "x_mm": 10, "y_mm": 10,
                              "w_mm": 40, "h_mm": 30, "z": 0}}},
        ]})


def test_an_export_of_a_figure_with_nothing_placed_is_refused(client):
    """Not a 500 and not an empty file: a figure whose panels are all still in
    the tray is a figure the user has not laid out yet, and saying so is more
    use than handing back a blank page."""
    figure_id = create(client)
    response = client.post(f"{API}/figures/{figure_id}/export", json={"format": "pdf"})
    assert response.status_code == 400
    assert "no panels" in response.get_json()["error"]


def test_an_unknown_format_is_refused_with_the_list(client):
    figure_id = create(client)
    _panel_on_a_page(client, figure_id)
    response = client.post(f"{API}/figures/{figure_id}/export", json={"format": "eps"})
    assert response.status_code == 400
    assert response.get_json()["formats"] == ["pdf", "png", "tiff"]


def test_an_export_runs_as_a_job_that_can_be_followed(client):
    """An eighteen-panel figure at 600 DPI is minutes, which is longer than any
    browser holds a request open. So starting and following are separate."""
    import time

    figure_id = create(client)
    _panel_on_a_page(client, figure_id)
    started = client.post(f"{API}/figures/{figure_id}/export",
                          json={"format": "png", "dpi": 96})
    assert started.status_code == 200
    job_id = started.get_json()["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"{API}/figures/{figure_id}/export/{job_id}").get_json()
        if body["job"]["status"] != "running":
            break
        time.sleep(0.05)

    assert body["job"]["status"] == "done", body["job"].get("error")
    download = client.get(f"{API}/figures/{figure_id}/export/{job_id}/download")
    assert download.status_code == 200
    assert download.get_data()[:8].startswith(b"\x89PNG")


def test_a_job_belonging_to_another_figure_is_not_reachable(client):
    """The job id is not a capability on its own: a figure's exports are
    addressed under that figure, so a stray id cannot fetch somebody else's
    render."""
    first = create(client)
    second = create(client)
    _panel_on_a_page(client, first)
    job_id = client.post(f"{API}/figures/{first}/export",
                         json={"format": "png", "dpi": 96}).get_json()["job_id"]

    assert client.get(f"{API}/figures/{second}/export/{job_id}").status_code == 404
    assert client.get(f"{API}/figures/{second}/export/{job_id}/download").status_code == 404


def test_downloading_before_the_job_finishes_is_409(client):
    figure_id = create(client)
    _panel_on_a_page(client, figure_id)
    response = client.get(f"{API}/figures/{figure_id}/export/job_nope/download")
    assert response.status_code == 404


def test_preflight_answers_before_anything_is_rendered(client):
    figure_id = create(client)
    _panel_on_a_page(client, figure_id)
    response = client.post(f"{API}/figures/{figure_id}/export/preflight",
                           json={"dpi": 600})
    assert response.status_code == 200
    body = response.get_json()
    assert body["dpi"] == 600
    assert body["panels"] == 1


# -- pages of its own ---------------------------------------------------

def test_the_library_page_renders_with_no_project_open(client):
    response = client.get("/plugins/figure_builder/figures")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="fb_library_results"' in html
    # Its own assets, through the descriptor -- so the eager path here cannot
    # drift from what the tool path serves.
    assert "figureLibrary.js" in html


def test_the_library_page_links_the_stylesheet_its_classes_come_from(client):
    """It is built out of core's Open Project furniture -- .open-project-page,
    .project-card, .project-thumb -- and base.html links openProject.css only
    for the page core owns. Borrowing the markup without asking for the sheet
    shipped a library with no layout at all, and nothing else notices: unstyled
    HTML is a working page to every test that reads it."""
    html = client.get("/plugins/figure_builder/figures").get_data(as_text=True)
    assert "css/openProject.css" in html
    # Before this plugin's own sheet, so a .fb- rule refines a .project- one
    # instead of losing the tie to it.
    assert html.index("css/openProject.css") < html.index("figure_builder.css")


def test_library_cards_wear_no_class_the_workspace_takes_out_of_flow():
    """The library's cards are laid out by core's grid; the workspace's are
    floating surfaces pinned over a canvas. `.fb-card` came to mean the second
    long after the library had put it on the first, and every figure in the
    grid stacked on top of the first one.

    Read off the sources because the cards are built in the browser, so no
    rendered page shows this. Structural rather than a ban on one name: it is
    the next class that acquires `position: absolute` that this is for."""
    styles = (STATIC / "figure_builder.css").read_text(encoding="utf-8")
    markup = (STATIC / "figureLibrary.js").read_text(encoding="utf-8")

    positioned = set()
    for rule in styles.split("}"):
        selector, _, body = rule.partition("{")
        if re.search(r"position:\s*(absolute|fixed)", body):
            positioned |= set(re.findall(r"\.(fb-[\w-]+)", selector))
    assert positioned, "no positioned .fb- classes found -- the scan is broken"

    worn = set()
    for attribute in re.findall(r"""class="([^"$]*)\"""", markup):
        worn |= set(attribute.split())
    trespass = sorted(worn & positioned)
    assert not trespass, (
        f"library cards carry {trespass}, which figure_builder.css positions "
        "out of flow -- the grid collapses onto one cell.")


def test_a_figures_own_page_renders(client):
    figure_id = create(client)
    response = client.get(f"/plugins/figure_builder/figure/{figure_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'data-figure-id="{figure_id}"' in html


def test_a_page_for_a_figure_that_is_gone_is_404(client):
    assert client.get("/plugins/figure_builder/figure/fig_deadbeefcafe").status_code == 404
    assert client.get("/plugins/figure_builder/figure/not-an-id").status_code == 404


def test_the_plugins_own_pages_still_carry_the_file_menu(client):
    """base.html renders it on every page, this one included -- so the entry
    that leads here has to be present here too, or the menu loses items the
    moment the user follows one of them."""
    html = client.get("/plugins/figure_builder/figures").get_data(as_text=True)
    assert 'id="nav_figure_builder_figures"' in html


# -- helpers ------------------------------------------------------------

def _corrupt(root, figure_id):
    path = root / repository.FIGURES_DIRNAME / figure_id / repository.DB_FILENAME
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute("UPDATE document SET json = ? WHERE id = 1", ("{broken",))
    finally:
        connection.close()
