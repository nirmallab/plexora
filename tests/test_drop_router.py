"""What a file dropped on the window does where no field claimed it
(services/dropRouter.js).

The server decides what dropped files are relative to the open sample
(tests/test_drop_plan.py); the page carries the plan out -- an action marked
`auto` runs, one marked `needs_confirm` runs only after a yes, one marked
`blocked` only says why -- and refuses a drop that missed an open dialog
instead of opening the Import dialog over it, which is how a markers.csv
dropped beside "Add channel names" used to become a sample. All of that is
JavaScript, so the checks live in tests/js/drop_router_probe.mjs, run against
the shipped file with a stubbed page. This wrapper runs it and pins its lines,
so an edit that quietly drops a check does not read as a pass.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "drop_router_probe.mjs"

CHECKS = (
    'the viewer is the viewer',
    'a hidden viewer is not',
    'a modal dialog is',
    'a drop that misses an open dialog is refused with a notice',
    '...and nothing is asked, imported or opened',
    'the server is asked about the files, the open sample and where',
    'an auto action runs through the route it names, with no question',
    'an existing mask is asked about before anything happens',
    '...with Replace red and not the focused answer',
    '...and Cancel changes nothing',
    'the answer chosen is the route taken',
    'a mask of the wrong size is shown with both sizes',
    '...and Attach anyway is not the primary answer',
    'a blocked action only says why',
    'markers.csv on the viewer renames the channels with the column the server read',
    '...and never opens the Import dialog',
    'a file nothing could read is named once and does not stop the rest',
    "a plugin's kind runs the executor the plugin registered",
    'a kind nothing can run says so',
    'with nothing open the plan is asked without a sample',
    'a new image imports in one press and opens',
    'opening the sample an image belongs to asks first, then goes there',
    '...carrying the rest of the drop for that sample',
    'the sample that opens attaches what was carried',
    '...once',
    'another sample does not take it',
    'a stale one is dropped',
    'a mask with nothing open is attached to the sample chosen in the dialog, which opens',
    'a server with no plan gets the old answer: the Import dialog',
    'a drop while another is being added waits, and says so',
    '...then runs',
    'the home page hands over only what is more than a new sample',
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize("check", CHECKS)
def test_each_check_ran_and_passed(probe, check):
    assert f"ok - {check}" in probe.stdout.splitlines(), probe.stdout


def _source(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_every_page_loads_the_router_after_the_registry_it_extends():
    base = _source("plexora/client/templates/base.html")
    picker = base.index("services/browsePicker.js")
    router = base.index("services/dropRouter.js")
    assert picker < router


def test_the_figure_canvas_is_a_zone_in_the_registry_not_a_listener_of_its_own():
    """It used to add a document listener per bind that ignored a drop
    already claimed, and was blind to a dialog open over the canvas."""
    workspace = _source("plexora/plugins/figure_builder/static/figureWorkspace.js")
    assert "attachDropTarget(scroll" in workspace
    assert "plexora:native-drop" not in workspace


@pytest.mark.parametrize("relative, needle", [
    ("plexora/plugins/gating/static/csvGatingList.js", 'registerExecutor("upload_gates"'),
    ("plexora/plugins/gating/static/csvGatingList.js", "attachDropTarget(gatesList"),
    ("plexora/plugins/roi/static/roiSidebarController.js", "attachDropTarget(panel"),
    ("plexora/client/src/js/views/settingsPage.js", "attachDropTarget(licenceBox"),
    ("plexora/client/src/js/views/channelNamesUpload.js", "attachDropTarget(node"),
    ("plexora/client/src/js/views/importSample.js", "attachDropTarget(node"),
    ("plexora/client/src/js/main.js", "PlexoraDropRouter?.takeHandoff?.(datasource)"),
])
def test_each_place_a_file_lands_registers_itself(relative, needle):
    assert needle in _source(relative)


@pytest.mark.parametrize("relative, selector", [
    ("plexora/client/src/css/main.css", ".plx-drop-hint"),
    ("plexora/client/src/css/main.css", ".channel-names-modal.is-drop-over"),
    ("plexora/client/src/css/viewer.css", ".viewer-shell.is-drop-over #openseadragon_wrapper::after"),
    ("plexora/client/src/css/settings.css", ".license-drop.is-drop-over"),
    ("plexora/plugins/gating/static/gating.css", "#csv_gating_list.is-drop-over"),
    ("plexora/plugins/figure_builder/static/figure_builder.css", "#fb_canvas_scroll.is-drop-over"),
    ("plexora/plugins/roi/static/roi.css", "#roi_panel_section.is-drop-over"),
])
def test_each_zone_draws_its_lit_state(relative, selector):
    assert selector in _source(relative)
