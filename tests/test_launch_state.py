"""Opening a viewer already showing something, without changing the project.

`plexora.view(..., tool=, overlay=, channels=)` puts state in the entry URL and
the page applies it in place of what the project has saved. The whole design
rests on one property: **none of it is written back**. A notebook that opens the
same project a dozen ways in a dozen cells must leave the project remembering
whatever the user last arranged in the browser, or the feature is a way to lose
your channel setup by looking at something.

The three halves are tested where each lives:

  the URL      tests/test_jupyter_viewer.py -- what `.url` builds
  the route    tests/test_page_routes.py    -- `?launch=` parsed and validated
  the page     here                         -- which channels and which column

This file is the page half: the two decisions (tests/js/launch_state_probe.mjs)
and the persist calls they must not reach.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "launch_state_probe.mjs"
SIDEBAR = REPO_ROOT / "plexora/client/src/js/views/viewerSidebar.js"
CONTROLLER = (REPO_ROOT
              / "plexora/plugins/cell_explorer/static/cellExplorerSidebarController.js")


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


def test_the_requested_channels_are_the_ones_turned_on(probe):
    assert "the requested channels come through in the order they were given" in probe
    assert "...carrying whatever options each was given" in probe


def test_a_channel_the_image_does_not_have_is_dropped_not_fatal(probe):
    assert "a channel this image does not have is dropped" in probe
    assert "...and said out loud" in probe


def test_a_page_that_asked_for_nothing_behaves_exactly_as_before(probe):
    """The branch every existing viewer takes, pinned so the feature stays
    invisible to everyone who did not ask for it."""
    assert "an ordinary page load asks for nothing" in probe
    assert "with no request, the saved selection still wins" in probe


def test_the_requested_overlay_outranks_the_saved_one(probe):
    assert "what the launch asked for beats what was showing last time" in probe
    assert "a request for a column this table lost falls through to the saved one" in probe


def test_a_paste_goes_through_the_launch_path_on_its_own_terms(probe):
    """The Image card's "Paste rendering settings" (layerManager.js) reuses
    applyLaunchChannels: an off slot stays off, and its auto-level is saved
    because a paste is an edit, where a launch's is not."""
    assert "a launch row turns its channel on, and a pasted off slot stays off" in probe
    assert "...and a launch's auto-level is still kept off the project" in probe
    assert "a paste's auto-level is the user's edit, and is saved" in probe


def test_a_copy_never_calls_a_byte_window_raw(probe):
    assert "a copy records each slot's channel, position, colour and state" in probe
    assert "...a window only where it can be said in raw units" in probe


def test_a_scoped_sidebar_ignores_the_pages_launch_state(probe):
    assert "a scoped sidebar takes no launch state at all" in probe


# -- the property the whole design rests on ------------------------------


def test_a_launch_restore_does_not_write_the_channel_list_back():
    """`persistChannelList` is what makes a project remember its channels. The
    default branch calls it (a new project needs a starting point); the launch
    branch must not, or one cell's arguments become the project's channels."""
    source = SIDEBAR.read_text(encoding="utf-8")
    call = next(line for line in source.splitlines()
                if "this.persistChannelList()" in line and "if (" in line)

    assert "!launchChannels.length" in call, call
    # The same property, for the other restore that must not be written back:
    # channels carried from the sample next door (services/carryOver.js). Saving
    # those would make one walk through a dataset rewrite every sample in it to
    # match whichever one the user started from.
    assert "!carriedChannels.length" in call, call


def test_the_launch_overlay_is_applied_without_persisting():
    """`select(column, {persist: false})` is the existing seam -- it is how the
    panel already restores a saved selection without immediately saving it
    back. The launch overlay rides the same call."""
    source = CONTROLLER.read_text(encoding="utf-8")

    assert "flaskVariables?.launch?.overlay" in source
    assert "await this.select(column, { persist: false })" in source
