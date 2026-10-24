"""A file dropped on a path field in the desktop app (services/browsePicker.js).

In the desktop app the shell hands the page a dropped file's PATH, and every
path box -- the home page's, the edit page's mask and data, the requirements
dialog, channel names, gene groups, the settings data folder, the Import
dialog's per-sample rows -- takes one with nothing added to it: the box lights
while a file it would take is over it, and the drop fills it as Browse does.
All of that is decided in JavaScript, so the checks live in
tests/js/path_drop_probe.mjs, run against the shipped files with a stubbed
page. This wrapper runs it and pins its lines, so an edit that quietly drops a
check does not read as a pass.

The bridge that turns the shell's drag into page events is
tests/test_desktop_bridge.py's; the home page's wiring is
tests/test_home_landing.py's.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "path_drop_probe.mjs"

CHECKS = (
    "outside the app attachDropTarget attaches nothing",
    "attachBrowseButton makes the field's row a drop zone",
    "a box under the pointer lights while one file is dragged over it",
    "and unlights when the drag moves off it",
    "an over phase with no paths keeps what the enter phase brought",
    "and when the drag leaves the window",
    "one path dropped on a box fills it and fires input, keyup and change",
    "and the drop is claimed so the Import dialog stays shut",
    "the drop unlights the box it filled",
    "two paths over a one-box field light nothing and are not claimed",
    "a suffix the box rejects lights it refused and the drop says why",
    "a name without a suffix is taken",
    "the drop unlights the box whether or not it was claimed",
    "a box that cannot be typed in lets the drop through",
    "a folder box refuses a file name",
    "a store folder is taken by a box that reads stores",
    "with a modal open only boxes inside it count",
    "a box set to another machine is switched to Local before it is filled",
    "a box with its own Remote switch set to Remote refuses with a reason",
    "onOver hears the zone light and go out",
    "a zone that takes several is handed every path",
    "several files over a one-path row light and go to the zone around it",
    "one file over the row is still the row's",
    "a zone that says a file is not its kind passes it to the zone around it",
    "and keeps the files that are",
    "a one-file zone with `several` refuses a drag of several with its reason",
    "drop: false leaves the row unregistered",
    "the zone's label is drawn beside the pointer",
    "the plan is asked for once per drag",
    "the label is redrawn when the plan lands",
    "the plan reaches the drop",
    "and the label goes with the drop",
    "the next drag asks again",
    "leaving the window takes the label away",
    "a refused box's label says why, in the refused tone",
    "a zone inside a modal lights itself and draws no label",
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


def test_the_import_dialogs_rows_take_a_drop_too():
    """The per-sample mask / data / layer row is the one path box that does
    not go through attachBrowseButton -- a browsed path goes straight to
    `submitSlot` -- so it registers its own zone, and a drop on it is sent the
    same way."""
    text = (REPO_ROOT / "plexora/client/src/js/views/importSample.js").read_text(encoding="utf-8")
    assert "attachDropTarget(wrap" in text
    assert 'submitSlot(path, "drop", adding)' in text
    css = (REPO_ROOT / "plexora/client/src/css/main.css").read_text(encoding="utf-8")
    assert ".plx-import-inline-pick.is-drop-over .plx-import-field" in css
    assert ".import-field-row.is-drop-over > .form-control" in css
