"""The home page: its own controls, somebody else's importer.

`GET /` with no datasource named renders the landing card -- a File / Folder
pair, a path box led by the machine it is on, and one button that is Browse
until there is a path and Load after. That is the page's design and it is
deliberate: the one thing somebody does on an empty install is open an image,
and a modal covering an otherwise empty page is a step rather than a shortcut.
Under the card, the three projects opened last.

What it is NOT is a second importer. Load POSTs to `/import/sample`, the route
the Import Sample dialog submits to, so every rule about what the files are,
which of them is the reference, what the sample is called and whether this data
is already registered is decided once, in `import_proposal` and
`import_sample`. `POST /quick_view` -- a whole parallel registration that could
open exactly one image -- is what that replaces, and these tests are what keep
it from growing back: the page is asserted to reach the shared route, and the
shared route is asserted to still do more than the old one ever could.

The behaviour of the route itself lives in test_single_image_import.py (one
image, the case this page exists for) and test_import_entry_points.py (the same
files through every door). Here: the markup, the wiring, and the two capability
floors the page must not drop below.
"""

import json
import re
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora

CLIENT = Path(plexora.__file__).parent / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def empty(tmp_path):
    """An install with nothing registered, which is what shows the landing."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


@pytest.fixture
def landing(empty):
    response = empty.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# -- the page is the page ----------------------------------------------------

def test_the_home_page_is_the_landing_card_and_not_a_button_that_opens_a_dialog(
        landing):
    """The whole point of the reversion.

    An empty install has one thing to do, and this page does it in one gesture:
    pick, and land in the viewer. A primary button whose only job is to open a
    modal over an empty page adds a press and a screen to that, and the modal
    it opens asks the same first question this card already asks.
    """
    assert 'id="quick_view_landing"' in landing
    assert 'id="quick_view_kind"' in landing           # the File/Folder pair
    assert 'id="quick_view_path_input"' in landing     # the typed way in
    assert 'id="quick_view_go"' in landing             # Browse, then Load

    # The dialog is not this page's primary action. Its button is in the
    # footer, after the card -- checked by position, because "it is present"
    # is true of the design this replaces too.
    card = landing[landing.index('id="quick_view_landing"'):]
    assert card.index('id="quick_view_go"') < card.index("sample-import-home")
    assert 'class="quick-view-footer"' in card
    assert card.index('class="quick-view-footer"') < card.index("sample-import-home")


def test_the_way_back_to_the_library_is_the_recent_list(empty, tmp_path):
    """`/` renders this card whenever the URL names no datasource, not only on
    an empty install -- so somebody with twenty samples lands here too, and
    the way back to them is on the page: the three opened last, each a link
    straight into its viewer, and the whole library one link further.
    """
    image = tmp_path / "slide.ome.tif"
    rng = np.random.default_rng(0)
    tifffile.imwrite(image, rng.integers(0, 3000, (2, 64, 64)).astype(np.uint16))
    assert empty.post("/import/sample", json={"paths": [str(image)]}).status_code == 200

    landing = empty.get("/").get_data(as_text=True)
    card = landing[landing.index('id="quick_view_landing"'):]
    recent = card[card.index('class="quick-view-recent"'):]
    assert "/open_project" in recent, "the library link left the recent list"
    assert 'href="/slide"' in recent
    assert "OME-TIFF" in recent, "the row lost the words a library card uses"


def test_an_install_with_nothing_to_reopen_has_no_recent_list(landing):
    """An empty heading over an empty list is a section that says nothing --
    and the library it would link to is empty too."""
    assert 'class="quick-view-recent"' not in landing


def test_recent_is_the_three_opened_last_newest_first(empty, tmp_path):
    """Ordered by `lastOpenedAt`, which opening a project stamps -- the same
    key the library's "Recently Opened" sort reads -- and cut to three before
    any project is loaded, so a large install does not pay for the rest."""
    from plexora.server.routes.project_routes import recent_projects

    rng = np.random.default_rng(0)
    names = []
    for index in range(4):
        image = tmp_path / f"slide{index}.ome.tif"
        tifffile.imwrite(image, rng.integers(0, 3000, (2, 32, 32)).astype(np.uint16))
        body = empty.post("/import/sample", json={"paths": [str(image)]}).get_json()
        names.append(body["name"])

    config_file = tmp_path / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    for day, name in zip((3, 9, 1, 7), names):
        config[name]["lastOpenedAt"] = f"2026-10-0{day}T12:00:00"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    assert [card["name"] for card in recent_projects(3)] == [names[1], names[3], names[0]]


def test_the_opened_phrase_is_the_librarys():
    """openProjectPage.js's `timeAgo`, worked out server-side against this
    machine's clock -- `lastOpenedAt` is this machine's local time."""
    import datetime

    from plexora.server.routes.page_routes import _opened_phrase

    now = datetime.datetime(2026, 10, 10, 12, 0, 0)
    assert _opened_phrase("2026-10-10T11:59:30", now) == "just now"
    assert _opened_phrase("2026-10-10T11:15:00", now) == "45 minutes ago"
    assert _opened_phrase("2026-10-10T11:00:00", now) == "1 hour ago"
    assert _opened_phrase("2026-10-08T12:00:00", now) == "2 days ago"
    assert _opened_phrase("2026-08-01T12:00:00", now) == "Aug 1, 2026"
    assert _opened_phrase("not a date", now) == ""


def test_the_landing_brings_its_own_stylesheet_and_controller(landing):
    """Both were deleted with the page and both are back, at a tag that is not
    a cached copy of either the old page's or the dialog's."""
    assert re.search(r'quickView\.css\?v=[^"]+', landing)
    assert re.search(r'quickViewLanding\.js\?v=[^"]+', landing)


def test_the_whole_page_takes_a_dropped_file_in_the_desktop_app():
    """In the desktop app a file dragged in from Explorer or Finder opens from
    anywhere on the page, not only over the card: the zone is the landing
    itself, one file goes through `take` -- what a Browse pick goes through --
    and several go to the Import dialog. The card is what lights."""
    text = source("src", "js", "views", "quickViewLanding.js")
    assert 'document.getElementById("quick_view_landing")' in text
    assert "attachDropTarget(landing" in text
    assert "take(paths[0])" in text
    assert "PlexoraImportSample?.open({paths})" in text
    assert ".quick-view-card.is-drop-over" in source("src", "css", "quickView.css")


def test_nothing_still_points_at_the_import_page_that_was_deleted(landing):
    """The footer used to end in a link to `/upload_page`, and that page is
    gone -- deliberately, with the three-field form it rendered. A reverted
    template that brought the link back with it would render a 404 in the one
    place this page sends somebody who needs more than one pick."""
    assert "/upload_page" not in landing
    assert "upload_page" not in source("templates", "index.html")


# -- one importer under it ---------------------------------------------------

def test_load_submits_to_the_shared_import_route():
    """The page is an entry point, not an importer.

    If this ever names a route of its own again, the rules diverge: the old
    `/quick_view` had its own name derivation, its own duplicate check and its
    own idea of what an image was, and keeping those three in step with the
    dialog's was never once done.
    """
    landing = source("src", "js", "views", "quickViewLanding.js")
    assert 'plexoraUrl("import/sample")' in landing
    # One pick, submitted as the list the route takes -- and no name and no
    # dataset, which are decisions this page does not get to ask about.
    assert "JSON.stringify({paths: [path]})" in landing
    assert '"quick_view"' not in landing


def test_the_only_registration_route_is_the_shared_one(empty):
    """Asserted against the URL map rather than the source: a second importer
    reachable at any URL is the thing being ruled out, whatever it is called."""
    rules = {str(rule) for rule in plexora.app.url_map.iter_rules()}
    assert "/import/sample" in rules
    assert "/quick_view" not in rules


def test_the_payload_the_page_sends_is_one_the_route_accepts(empty, tmp_path):
    """The wiring test above reads the request out of the source; this sends
    exactly that request and asserts the viewer is on the other side of it."""
    image = tmp_path / "slide.ome.tif"
    rng = np.random.default_rng(0)
    tifffile.imwrite(image, rng.integers(0, 3000, (2, 64, 64)).astype(np.uint16))

    response = empty.post("/import/sample", json={"paths": [str(image)]})

    assert response.status_code == 200, response.data
    body = response.get_json()
    assert body["name"] == "slide"
    assert body["redirect"] == "/slide"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "slide" in config


def test_the_same_image_twice_reopens_rather_than_duplicating(empty, tmp_path):
    """The one rule the old landing page had that mattered, kept -- and now
    kept in one place for every surface instead of in a route of its own.

    Two projects over one slide diverge: each collects its own ROIs, gates and
    figures, and nothing afterwards can tell you the other exists.
    """
    image = tmp_path / "slide.ome.tif"
    rng = np.random.default_rng(0)
    tifffile.imwrite(image, rng.integers(0, 3000, (2, 64, 64)).astype(np.uint16))

    first = empty.post("/import/sample", json={"paths": [str(image)]}).get_json()
    second = empty.post("/import/sample", json={"paths": [str(image)]}).get_json()

    assert second["name"] == first["name"]
    assert second["existing"] is True
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert list(config) == [first["name"]]


# -- and it can now do what the old page refused -----------------------------

def test_a_pick_with_more_than_an_image_in_it_is_no_longer_refused(empty,
                                                                   tmp_path):
    """The capability floor this reversion must not drop through.

    `POST /quick_view` registered an image and nothing else -- a mask or a
    table beside it was somebody else's problem, and the footer link was the
    apology. The landing page submits the same one path to the same route the
    dialog does, so a folder whose files belong together arrives as one sample
    with all of them in it.
    """
    run = tmp_path / "run"
    run.mkdir()
    rng = np.random.default_rng(0)
    tifffile.imwrite(run / "slide.ome.tif",
                     rng.integers(0, 3000, (2, 128, 128)).astype(np.uint16))
    labels = np.zeros((128, 128), dtype=np.uint32)
    for index in range(1, 6):
        labels[index * 18:index * 18 + 12, index * 18:index * 18 + 12] = index
    tifffile.imwrite(run / "slide_mask.tif", labels)
    (run / "slide.csv").write_text(
        "CellID,X_centroid,Y_centroid,DNA\n1,10,10,5\n2,20,20,7\n",
        encoding="utf-8")

    # The folder, which is what pressing "Select Folder" hands over.
    response = empty.post("/import/sample", json={"paths": [str(run)]})
    assert response.status_code == 200, response.data

    from plexora.server.models.project import Project

    project = Project.load(response.get_json()["name"])
    assert project.segmentation.source, "the mask beside the slide was dropped"
    assert project.dataset is not None, "the table beside the slide was dropped"


def test_the_switch_still_lets_the_image_be_on_another_machine(landing):
    """The landing page's machine chip, at the start of the path box. It is
    the shared Local/Remote state underneath (attached `bare`, moved with
    `setWhere`), and it is `submitValue()` that turns Remote into the
    `node://<node>/<resource>` the route reads -- so a slide on a cluster is
    the same one gesture."""
    assert 'id="quick_view_where"' in landing
    assert 'id="quick_view_where_menu"' in landing
    assert 'id="quick_view_where_status"' in landing

    controller = source("src", "js", "views", "quickViewLanding.js")
    assert "PlexoraDataLocation" in controller
    assert "bare: true," in controller
    assert "location.setWhere(where, place)" in controller
    assert "location.submitValue()" in controller
    # The menu adds and connects machines where it stands, through the modal
    # that owns connecting -- never a trip to Settings first.
    assert "PlexoraConnectionModal.open(" in controller


# -- the dialog is still everywhere it was -----------------------------------

def test_the_import_dialog_is_reachable_from_every_surface_it_was(landing,
                                                                  empty):
    """Reverting the home page's PRIMARY action must not unreach the dialog.

    Five surfaces open it, and the home page's footer is one of them: a pick
    that makes several samples, or wants a name or a dataset chosen first, is a
    decision, and this page cannot ask.
    """
    assert 'id="sample-import-home"' in landing

    library = empty.get("/open_project").get_data(as_text=True)
    assert 'id="sample-import"' in library
    assert 'id="sample-import-empty"' in library
    assert 'id="sample-import-menu"' in library      # the navbar, on every page

    dialog = source("src", "js", "views", "importSample.js")
    for surface in ("sample-import", "sample-import-empty", "sample-import-home",
                    "sample-import-menu"):
        assert f'"{surface}"' in dialog, surface
    # And the viewer's own "+ Add Layer", which is scoped to one sample.
    assert "PlexoraImportSample?.open({" in source(
        "src", "js", "views", "layerManager.js")


def test_a_pick_with_anything_to_decide_opens_the_dialog_rather_than_guessing():
    """The page is a quick start, not a place to tolerate a guess.

    POSTing straight to `/import/sample` takes every default unseen and keeps
    only the FIRST sample: a folder holding two slides imported one and
    dropped the other without a word. So the pick is read first, and several
    samples, any question, a warning or a file it could not read opens the
    dialog -- on the proposal already read, so nobody waits for it twice. One
    sample with nothing to decide still opens in the same press.
    """
    landing = source("src", "js", "views", "quickViewLanding.js")
    body = landing[landing.index("async function uncertainty(path)"):]
    body = body[:body.index("\n    }\n")]
    assert "samples.length > 1" in body
    assert "(sample.questions || []).length > 0" in body
    assert "(proposal.warnings || []).length > 0" in body
    assert "(proposal.unrecognised || []).length > 0" in body
    # Nothing readable is not a guess: the POST says why, on the card.
    assert "if (!samples.length) return null;" in body
    assert "window.PlexoraImportSample.open({paths: [path], proposal});" in landing

    dialog = source("src", "js", "views", "importSample.js")
    assert "if (options.proposal) showRead(options.paths, options.proposal);" in dialog
