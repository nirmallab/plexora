"""The home page: its own controls, somebody else's importer.

`GET /` with nothing registered renders the landing card -- a Local/Remote
switch, a Select File / Select Folder pair, a path box and Load. That is the
page's design and it is deliberate: the one thing somebody does on an empty
install is open an image, and a modal covering an otherwise empty page is a
step rather than a shortcut.

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
    assert 'id="quick_view_open"' in landing           # the File/Folder pair
    assert 'id="quick_view_path_input"' in landing     # the typed way in
    assert 'id="quick_view_path_load"' in landing      # and Load

    # The dialog is not this page's primary action. Its button is in the
    # footer, after the status line -- checked by position, because "it is
    # present" is true of the design this replaces too.
    card = landing[landing.index('id="quick_view_landing"'):]
    assert card.index('id="quick_view_path_load"') < card.index("sample-import-home")
    assert 'class="quick-view-footer"' in card
    assert card.index('class="quick-view-footer"') < card.index("sample-import-home")


def test_the_way_back_to_the_library_survived_the_reversion(landing):
    """`/` renders this card whenever the URL names no datasource, not only on
    an empty install -- so somebody with twenty samples lands here too.

    The pre-redesign page had no link to them at all and left it to the File
    menu. The redesign added one, and it is kept: it costs a clause in the
    footer that already existed, so the layout is the old one either way.
    """
    assert "/open_project" in landing
    card = landing[landing.index('id="quick_view_landing"'):]
    footer = card[card.index('class="quick-view-footer"'):]
    assert "/open_project" in footer, "the link drifted out of the footer line"


def test_the_landing_brings_its_own_stylesheet_and_controller(landing):
    """Both were deleted with the page and both are back, at a tag that is not
    a cached copy of either the old page's or the dialog's."""
    assert re.search(r'quickView\.css\?v=[^"]+', landing)
    assert re.search(r'quickViewLanding\.js\?v=[^"]+', landing)


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
    tifffile.imwrite(run / "slide_mask.tif",
                     (np.arange(128 * 128).reshape(128, 128) % 90).astype(np.uint32))
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
    """The landing page's Local/Remote switch, mounted once above both
    controls. It is `submitValue()` that turns Remote into the
    `node://<node>/<resource>` the route reads, and `/import/sample` handles
    that address itself -- so a slide on a cluster is the same one gesture."""
    assert 'id="quick_view_where_control"' in landing
    assert 'id="quick_view_where_status"' in landing

    controller = source("src", "js", "views", "quickViewLanding.js")
    assert "PlexoraDataLocation" in controller
    assert "location.submitValue()" in controller


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
