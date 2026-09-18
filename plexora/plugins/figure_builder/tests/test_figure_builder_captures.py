"""The captures bin: what a capture is before it belongs to anything.

Capturing used to demand an answer to "which figure?" at the worst possible
moment -- before the user had decided which of the regions in front of them
were worth keeping -- and anything captured before an answer was given lived in
the viewer page's memory and died with the page. The bin is what makes "I do
not know yet" a storable answer.

Three properties are load-bearing here and each has its own test:

**A capture is not in a figure.** Nothing about a bin row names one, and
`list_figures()` must not notice the `.captures` directory sitting beside
`.figures` -- a bin that showed up as a damaged figure card would be a scary
row in the library for something that is working exactly as intended.

**A capture is validated on the way IN.** Every later adoption reads the scene
back and turns it into a panel, so a bad one poisons a figure rather than a
request. And a scene from a newer build of Plexora is refused the same way a
figure's would be, rather than stored and read back as a panel that cannot be
drawn.

**Leaving the bin is atomic per capture.** The raster is copied into the figure
and only then is the row deleted, because the row is the last remaining record
of a region the user may never be standing over again.
"""

import json
import sqlite3

import pytest

import plexora
from plexora.plugins.figure_builder.server import captures, repository
from plexora.server import plugins as plugin_registry
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

API = "/plugins/figure_builder/api"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app with a data_path of its own, so a test can neither see nor touch
    the user's own captures. Mirrors test_figure_builder_routes.py's fixture."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demo": project("demo", image=image_spec(channels=("DNA", "CD3"),
                                                 width=4000, height=3000),
                        confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")

    if plugin_registry.find(plexora.app, "figure_builder") is None:  # pragma: no cover
        pytest.skip("figure_builder is not installed")
    return plexora.app.test_client()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The bin repository alone, with no app around it."""
    use_data_root(monkeypatch, tmp_path)
    return captures


def scene(x=1000, y=750, w=2000, h=1500):
    """A capture's scene, in FULL-RESOLUTION image pixels -- which is the only
    coordinate system a panel can be re-rendered from at an arbitrary DPI."""
    return {
        "snapshot_version": 1,
        "source_id": "",
        "viewport": {"x": x, "y": y, "w": w, "h": h},
        "channels": [{"key": "demo_0", "fullname_at_capture": "DNA",
                      "color": {"r": 255, "g": 255, "b": 255},
                      "window": [0, 65535], "visible": True}],
        "core_overlays": {"cell_layers": [], "layers": [],
                          "hd_tiles": False, "scalebar_visible": False},
        "plugins": {},
        "captured_at": "2026-09-17T10:00:00Z",
    }


def source(datasource="demo"):
    """What a capture records about the image it came from.

    No `source_id`: the id this image will carry INSIDE some figure cannot be
    known, because no figure has been chosen. That is the whole point of the
    bin, and the store supplies a placeholder.
    """
    return {
        "kind": "plexora_project",
        "datasource": datasource,
        "display_name": datasource,
        "image": {"width": 4000, "height": 3000},
        "channels": [{"key": "demo_0", "fullname_at_capture": "DNA"}],
        "pixel_size": {"value": 0.325, "unit": "µm", "source": "metadata"},
        "status": "ok",
    }


def keep(client, capture_id="cap_one", datasource="demo", caption="", preview=b"webp-bytes"):
    response = client.post(f"{API}/captures", json={
        "capture_id": capture_id, "datasource": datasource,
        "source": source(datasource), "scene": scene(), "caption": caption,
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    if preview is not None:
        stored = client.post(f"{API}/captures/{capture_id}/preview",
                             data=preview, content_type="image/webp")
        assert stored.status_code == 200
    return capture_id


# -- keeping one --------------------------------------------------------


def test_a_capture_is_kept_with_nothing_asked_about_a_figure(client):
    """The claim the whole redesign rests on. Nothing in the request names a
    figure and nothing in the answer does either."""
    keep(client, "cap_one", caption="650 µm wide")

    listed = client.get(f"{API}/captures").get_json()
    assert listed["success"] is True
    assert len(listed["captures"]) == 1
    entry = listed["captures"][0]
    assert entry["capture_id"] == "cap_one"
    assert entry["datasource"] == "demo"
    assert entry["caption"] == "650 µm wide"
    assert entry["has_preview"] is True
    assert "figure_id" not in entry


def test_the_scene_survives_in_full_resolution_image_pixels(client):
    """A capture is adopted into a figure long after it was taken, possibly
    from a page with no viewer at all, so the scene in the bin is the only
    record of which pixels it was. Screen coordinates or viewport fractions
    are indistinguishable from these once written down, and only these can be
    re-rendered."""
    keep(client, "cap_one")
    entry = client.get(f"{API}/captures").get_json()["captures"][0]
    assert entry["scene"]["viewport"] == {"x": 1000.0, "y": 750.0, "w": 2000.0, "h": 1500.0}
    assert [c["key"] for c in entry["scene"]["channels"]] == ["demo_0"]


def test_the_image_is_described_on_the_capture_itself(client):
    """Dimensions, channel keys and the pixel size ride along, because the
    moment of capture is the only moment they are certainly true -- and the page
    that adopts the capture may have no datasource loaded and no viewer to ask.
    The pixel size in particular is what decides whether the panel gets a
    physical scale bar or one measured in image pixels."""
    keep(client, "cap_one")
    entry = client.get(f"{API}/captures").get_json()["captures"][0]
    assert entry["source"]["datasource"] == "demo"
    assert entry["source"]["image"] == {"width": 4000, "height": 3000}
    assert entry["source"]["pixel_size"]["value"] == pytest.approx(0.325)


def test_a_stored_source_carries_a_placeholder_id_for_the_figure_to_replace(client):
    """`normalize_source` requires a source id and a capture cannot have the
    real one: that id is the image's identity INSIDE a figure, and no figure has
    been chosen. The client swaps in a fresh `src_...` when it adopts."""
    keep(client, "cap_one")
    entry = client.get(f"{API}/captures").get_json()["captures"][0]
    assert entry["source"]["source_id"] == captures.PLACEHOLDER_SOURCE_ID


def test_the_raster_comes_back_as_an_image(client):
    keep(client, "cap_one", preview=b"pretend-webp")
    response = client.get(f"{API}/captures/cap_one/preview")
    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    assert response.get_data() == b"pretend-webp"
    # Replaced in place at one URL whenever it changes, like a panel's preview,
    # so a cached copy would show the user something they have edited away
    # from.
    assert "no-cache" in response.headers["Cache-Control"]


def test_a_capture_with_no_raster_yet_is_not_an_error_to_list(client):
    """The row is written first and the raster follows, because the browser
    produces the blob asynchronously. In between, the capture exists and has no
    picture -- which the strip renders as a placeholder rather than as a gap."""
    keep(client, "cap_one", preview=None)
    entry = client.get(f"{API}/captures").get_json()["captures"][0]
    assert entry["has_preview"] is False
    assert client.get(f"{API}/captures/cap_one/preview").status_code == 404


# -- browsing -----------------------------------------------------------


def test_the_bin_can_be_narrowed_to_one_image(client):
    """Which is what the in-viewer strip asks for: a capture's outline is drawn
    in the coordinates of the image it came from, so one from another slide has
    nowhere to be drawn on this one."""
    keep(client, "cap_one", datasource="demo")
    keep(client, "cap_two", datasource="other")

    everything = client.get(f"{API}/captures").get_json()["captures"]
    assert {entry["capture_id"] for entry in everything} == {"cap_one", "cap_two"}

    narrowed = client.get(f"{API}/captures?datasource=demo").get_json()["captures"]
    assert [entry["capture_id"] for entry in narrowed] == ["cap_one"]


def test_the_bin_reads_newest_first(client):
    """Where the eye goes in a strip. The figure a batch becomes runs the other
    way -- a strip is a view and a figure is a record -- and the client reverses
    it on adoption."""
    keep(client, "cap_old", preview=None)
    captures.add("cap_new", "demo", source(), scene(), created_at="2099-01-01T00:00:00Z")
    order = [entry["capture_id"]
             for entry in client.get(f"{API}/captures").get_json()["captures"]]
    assert order == ["cap_new", "cap_old"]


def test_an_empty_bin_is_an_answer_and_not_a_file_on_disk(client):
    """Opening the Captures page having never captured anything must not create
    a database. A store that exists because somebody looked at a page is a store
    that turns up in backups and in "what is this directory?" questions."""
    assert client.get(f"{API}/captures").get_json() == {"success": True, "captures": []}
    assert not captures.db_path().exists()


# -- deleting -----------------------------------------------------------


def test_captures_are_discarded_in_bulk(client):
    """Clearing a bin of forty is one thing the user decided, and forty requests
    would be forty chances for half of it to happen."""
    for index in range(3):
        keep(client, f"cap_{index}", preview=None)

    response = client.delete(f"{API}/captures",
                             json={"capture_ids": ["cap_0", "cap_2"]})
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "removed": 2}
    assert [entry["capture_id"]
            for entry in client.get(f"{API}/captures").get_json()["captures"]] == ["cap_1"]


def test_one_capture_can_be_discarded_on_its_own(client):
    keep(client, "cap_one", preview=None)
    assert client.delete(f"{API}/captures/cap_one").status_code == 200
    assert client.get(f"{API}/captures").get_json()["captures"] == []


def test_discarding_a_capture_that_is_gone_is_404(client):
    """Distinct from 400: the request was fine and the thing it named has been
    deleted -- quite possibly in the other tab, or by the adoption that turned
    it into a panel."""
    assert client.delete(f"{API}/captures/cap_missing").status_code == 404


# -- leaving the bin ----------------------------------------------------


def _figure(client):
    response = client.post(f"{API}/figures", json={"title": "Figure 1"})
    assert response.status_code == 200
    return response.get_json()["figure_id"]


def test_adoption_moves_the_raster_and_empties_the_row(client):
    """The one path out of the bin. The copy is made first and the row deleted
    after, because the row is the last remaining record of a region the user may
    never be standing over again.

    The PANELS are not made here: the client commits them in the one batch that
    makes "add these captures" a single undo step, and building them server-side
    as well would be a second implementation of the panel defaults to disagree
    with the first.
    """
    figure_id = _figure(client)
    keep(client, "cap_one", preview=b"the-pixels")

    response = client.post(f"{API}/figures/{figure_id}/previews/from_captures",
                           json={"pairs": [{"capture_id": "cap_one",
                                            "panel_id": "pnl_one",
                                            "render_revision": 1}]})
    assert response.status_code == 200
    assert response.get_json()["moved"] == ["cap_one"]

    # Into the figure...
    preview = client.get(f"{API}/figures/{figure_id}/previews/pnl_one")
    assert preview.status_code == 200
    assert preview.get_data() == b"the-pixels"
    # ...and out of the bin, which is what stops the strip offering a capture
    # that is already a panel.
    assert client.get(f"{API}/captures").get_json()["captures"] == []


def test_a_capture_with_no_raster_still_leaves_the_bin(client):
    """By the time this runs the panel exists, so a bin row for it would be the
    one thing the bin must never show: something already in a figure. The panel
    re-renders from its scene, which is slower and not wrong."""
    figure_id = _figure(client)
    keep(client, "cap_one", preview=None)

    body = client.post(f"{API}/figures/{figure_id}/previews/from_captures",
                       json={"pairs": [{"capture_id": "cap_one", "panel_id": "pnl_one"}]}
                       ).get_json()
    assert body["moved"] == []
    assert body["missing"] == ["cap_one"]
    assert client.get(f"{API}/captures").get_json()["captures"] == []


def test_adopting_into_a_figure_that_is_gone_is_404_and_keeps_the_captures(client):
    """A figure deleted in another tab must not cost the user the captures that
    were on their way into it. 404 rather than 400 because the request was
    fine, and the bin is untouched so the answer can be "they are still in your
    captures bin"."""
    keep(client, "cap_one", preview=b"x")
    response = client.post(f"{API}/figures/fig_deadbeefcafe/previews/from_captures",
                           json={"pairs": [{"capture_id": "cap_one", "panel_id": "pnl_one"}]})
    assert response.status_code == 404
    assert len(client.get(f"{API}/captures").get_json()["captures"]) == 1


# -- what the store refuses ---------------------------------------------


def test_a_capture_id_the_client_invented_badly_is_refused(client):
    """Ids are minted by the client, like panel ids, so the strip can show a
    thumbnail before the round trip comes back -- which means the pattern is
    checked here. This value also arrives from a URL."""
    for bad in ("../../etc/passwd", "pnl_one", "cap_", ""):
        response = client.post(f"{API}/captures", json={
            "capture_id": bad, "datasource": "demo",
            "source": source(), "scene": scene()})
        assert response.status_code == 400, bad


def test_a_scene_from_a_newer_plexora_is_refused_rather_than_stored(store):
    """Refused with the same 422-shaped error a figure's would be. Stored, it
    would be read back later as a panel this build cannot draw -- and the place
    that would surface is the figure, long after the capture."""
    from plexora.plugins.figure_builder.server import schema

    future = scene()
    future["snapshot_version"] = schema.SNAPSHOT_VERSION + 1
    with pytest.raises(schema.UnreadableFigure):
        store.add("cap_one", "demo", source(), future)


def test_the_route_reports_a_newer_scene_as_unreadable(client):
    from plexora.plugins.figure_builder.server import schema

    future = scene()
    future["snapshot_version"] = schema.SNAPSHOT_VERSION + 1
    response = client.post(f"{API}/captures", json={
        "capture_id": "cap_one", "datasource": "demo",
        "source": source(), "scene": future})
    assert response.status_code == 422
    assert response.get_json()["error"] == "unreadable_figure"


def test_an_oversized_raster_is_refused(store):
    """A WebP of a canvas crop is tens of kilobytes. Past the limit the client
    is sending the wrong thing, and it is told so rather than the row growing a
    megabyte at a time."""
    store.add("cap_one", "demo", source(), scene())
    with pytest.raises(ValueError):
        store.put_preview("cap_one", b"x" * (store.MAX_PREVIEW_BYTES + 1))


def test_a_raster_for_a_capture_that_is_gone_is_404(client):
    """The row is written first and the raster second, so this is the window a
    delete can land in -- and a blob with no row would be bytes nothing can
    ever reach or clean up."""
    response = client.post(f"{API}/captures/cap_nobody/preview",
                           data=b"webp", content_type="image/webp")
    assert response.status_code == 404


# -- the bin is not a figure --------------------------------------------


def test_the_bin_does_not_show_up_in_the_figure_library(store, tmp_path):
    """`.captures` is a sibling of `.figures` under the same root, so the two
    stores cannot collide -- but a library that scanned the wrong directory
    would report the bin as a figure that could not be read, which is a scary
    row for something working exactly as intended."""
    store.add("cap_one", "demo", source(), scene())
    repository.create(title="Figure 1")

    listed = repository.list_figures()
    assert len(listed) == 1
    assert listed[0]["title"] == "Figure 1"
    assert (tmp_path / ".captures").is_dir()
    assert (tmp_path / ".figures").is_dir()


def test_the_bin_lives_under_the_users_own_root(monkeypatch, tmp_path):
    """Never a shared root. A capture is taken by one person, belongs to no
    project and to no figure, and there is nothing for a site-managed root to
    hold. Resolved on every call, like every other path in the app, so a test
    -- or `plexora config set` -- can repoint it underneath a live process."""
    from plexora import paths

    use_data_root(monkeypatch, tmp_path)
    assert paths.captures_root() == tmp_path / ".captures"
    assert captures.db_path() == tmp_path / ".captures" / "captures.db"


def test_a_row_survives_being_written_twice(store):
    """Which is what makes the client's retry safe: a capture whose row landed
    and whose raster did not is re-sent whole. There is nothing to merge -- a
    capture is written once, at the moment the shutter closes."""
    store.add("cap_one", "demo", source(), scene(), caption="first")
    store.add("cap_one", "demo", source(), scene(), caption="second")
    assert store.count() == 1
    assert store.get("cap_one")["caption"] == "second"


def test_a_capture_that_cannot_be_kept_says_so_rather_than_vanishing(client):
    """The other half of the previous test, and the asymmetry is the point.

    A bin that cannot be READ costs the user a thumbnail, so it reads as empty
    and the viewer opens. A capture that cannot be WRITTEN is the shutter press
    not landing, and it has to be reported while the user is still standing
    over the region -- the dock keeps the capture on screen and marks it as not
    saved.

    503 rather than 400 or 500: the request was fine and so is the server.
    """
    keep(client, "cap_one", preview=None)
    with sqlite3.connect(str(captures.db_path())) as connection:
        connection.execute("DROP TABLE captures")
        connection.execute("CREATE TABLE captures (capture_id TEXT)")

    response = client.post(f"{API}/captures", json={
        "capture_id": "cap_two", "datasource": "demo",
        "source": source(), "scene": scene()})
    assert response.status_code == 503
    assert response.get_json()["error"] == "captures_unavailable"


def test_a_damaged_bin_reads_as_an_empty_one(store, tmp_path):
    """The capture path must not stop working because the bin is corrupt: the
    next capture still lands, and refusing to open the viewer over it would be
    the worse failure. A figure does the opposite -- see repository.load -- and
    the difference is what is at stake: a day's composition against a
    screenshot nobody has committed to anything yet."""
    store.add("cap_one", "demo", source(), scene())
    with sqlite3.connect(str(store.db_path())) as connection:
        connection.execute("DROP TABLE captures")
        connection.execute("CREATE TABLE captures (capture_id TEXT)")
    assert store.list_captures() == []


# -- the page -----------------------------------------------------------


def test_the_captures_page_renders_with_no_project_open(client):
    """Needing a datasource even less than the figure library does: the bin
    spans images by construction, and the person who wants to see what they
    collected on Tuesday most often has nothing open at all."""
    response = client.get("/plugins/figure_builder/captures")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="fb_captures_root"' in html
    # Its own assets, through the descriptor, so this page cannot drift from
    # what the tool path serves.
    assert "figureCaptureBinGrid.js" in html
    # And the sheet the borrowed .open-project- classes come from, which
    # base.html links only for the page core owns.
    assert "css/openProject.css" in html


def test_the_figures_page_offers_the_way_to_the_bin(client):
    """Discoverability is the whole reason the bin is a tab rather than only a
    dialog on the canvas: a store nobody can find is a store people assume has
    lost their work."""
    html = client.get("/plugins/figure_builder/figures").get_data(as_text=True)
    assert "/plugins/figure_builder/captures" in html
