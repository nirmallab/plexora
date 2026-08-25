"""Attaching a mask no longer means waiting on a form for it.

A mask attached from the edit page is usually not pyramidized, and converting a
real slide's mask is minutes of work. That wait used to run on the EDIT page:
the save handler put a blocking overlay over the form and reloaded into the
viewer only when the job finished. The image was already imported and one
navigation away, the job was running server-side and needed nothing from the
browser, and the user spent the whole conversion looking at a form.

Now the save goes straight to the viewer and the job is shown there -- as a
dismissible modal, then as a navbar chip once it is closed -- and the mask is
drawn by itself when it lands.

Four files have to agree for that, and each holds one end of the same promise:

  projectEdit.js      does not wait, and goes to the viewer
  index.html          loads the panel the viewer shows it in
  main.js             opens that panel, and draws the mask when the job lands
  viewerControls.js   remembers whether the user chose what is on screen

The behaviour of the panel itself is in tests/js/segmentation_wait_probe.mjs,
run in node below, because nothing in the Python suite executes client JS.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "segmentation_wait_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


# -- the panel ---------------------------------------------------------------


def test_the_wait_opens_over_the_image_and_says_it_can_be_left(probe):
    assert "start() puts the modal up" in probe, probe
    assert "...saying the work is already running, in the background" in probe, probe
    assert "...and that closing it costs nothing" in probe, probe
    assert "...and that the mask arrives by itself" in probe, probe


def test_closing_it_hands_the_wait_to_the_navbar(probe):
    """The claim a user cannot check without losing several minutes finding out
    they were wrong: the job keeps running, and keeps being reported."""
    assert "the wait moves to the navbar rather than vanishing" in probe, probe
    assert "...labelled for the job it is" in probe, probe
    assert "readings keep landing after the modal is gone" in probe, probe
    assert "clicking the chip brings the detail back" in probe, probe


def test_the_two_endings_are_told_apart(probe):
    assert "finishing reopens nothing for a user who had closed it" in probe, probe
    assert "a failure DOES reopen it" in probe, probe
    assert "dismissing a failure ends it" in probe, probe


# -- the wiring --------------------------------------------------------------


def test_saving_a_mask_no_longer_holds_the_edit_page():
    """The whole point. `awaitSegmentationThenOpen` is the import pages'
    blocking overlay; calling it here is what made attaching a mask a wait."""
    edit = source("src", "js", "views", "projectEdit.js")
    assert "awaitSegmentationThenOpen" not in edit
    assert "segmentation_pending" not in edit, (
        "a pending job no longer changes where saving goes")
    assert "window.location.href = plexoraUrl(encodeURIComponent(project.name));" in edit

    # ...and the page stops paying for a script it no longer calls, while the
    # import page -- which has no viewer to hand the wait to -- keeps it. The
    # loaded tag, not the name: project_edit.html says in a comment why the tag
    # went, which is worth keeping and is not a load.
    tag = "views/segmentationProgress.js?"
    templates = CLIENT / "templates"
    assert tag not in (templates / "project_edit.html").read_text(encoding="utf-8")
    assert tag in (templates / "project_columns.html").read_text(encoding="utf-8")


def test_the_viewer_loads_the_panel_and_has_somewhere_to_put_the_chip():
    index = (CLIENT / "templates" / "index.html").read_text(encoding="utf-8")
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "views/segmentationWait.js" in index
    # Before main.js, which calls into it during init().
    assert index.index("views/segmentationWait.js") < index.index("js/main.js")
    assert 'id="segmentation_chip"' in base, "the navbar is base.html's"
    # PopoverPortal is a classic script and the overlay goes through it, so it
    # has to already be defined. base.html loads it un-deferred in <head>, which
    # runs before any deferred script in the body -- but only while it stays
    # un-deferred, which is what this pins.
    portal = 'views/popoverPortal.js'
    assert portal in base and " defer" not in base.split(portal, 1)[1].split(">", 1)[0]


def test_the_panel_is_opened_from_the_state_the_server_reports():
    """And opened EARLY -- main.js's own poll runs at the bottom of init(),
    behind the sidebar and every plugin, which is a long unexplained wait for a
    user who attached the mask seconds ago."""
    main = source("src", "js", "main.js")
    opener = "window.PlexoraSegmentationWait?.start();"
    assert opener in main
    assert main.index(opener) < main.index("const pollSegmentationStatus"), (
        "the panel must be up before the loop that fills it starts")
    # One loop asks the server. The panel is a listener, not a second poller.
    assert "getSegmentationStatus" not in source("src", "js", "views", "segmentationWait.js")


def test_a_mask_that_lands_draws_itself():
    """The last step the user asked for, and the one with a judgement in it.

    Through the whole conversion None is the only enabled button -- Outlines
    and Filled are greyed with "still being prepared" on them -- so a viewer
    sitting on "none" when the job lands is sitting where the control started,
    not where anyone put it. `userChose` is the difference, and without it this
    would overrule a real click on None."""
    main = source("src", "js", "main.js")
    adopt = main.split("function adoptSegmentation(path) {", 1)[1]
    assert "if (viewerControls.mode === 'none' && !viewerControls.userChose) {" in adopt
    # maskMode(), not a hardcoded "outlines": a plugin holding the cell layer
    # may want it filled, and this path is the one that runs on the page where
    # the mask actually arrives.
    assert "viewerControls.maskMode(viewerControls.ownerMaskPreference())" in adopt


def test_every_way_of_choosing_a_mode_records_that_the_user_chose_it():
    """Three surfaces move this control -- the sidebar's buttons, its arrow
    keys, and the View menu. One of them forgetting to say so is a silent
    regression: the mask would take over a mode the user had picked, minutes
    later, with nothing on screen explaining it."""
    controls = source("src", "js", "views", "viewerControls.js")
    navbar = source("src", "js", "views", "navbarControls.js")
    assert controls.count("this.userChose = true;") == 2, "the click and the arrow keys"
    assert "controls.userChose = true;" in navbar, "the View menu"
    assert "this.userChose = false;" in controls, "and it starts out false"
