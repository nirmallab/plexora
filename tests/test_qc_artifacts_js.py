"""The Artifact Detector's browser half, run in node against the shipped script.

The checks live in tests/js/qc_artifacts_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_artifacts_probe.mjs"
SCRIPT = REPO_ROOT / "plexora" / "plugins" / "qc" / "static" / "qcArtifacts.js"

CHECKS = (
    "four category rows come from the status, each with its colour, threshold and count",
    "a slider filters the objects at once and asks the server nothing while dragged",
    "the release stores that category's threshold once, typed in full",
    "a value from outside moves only its own slider, and none is echoed back",
    "a colour picked is that category's and recolours its slider",
    "with no channel listed, All channels' eye shows and hides everything",
    "+ adds an empty line whose pick lists the channel and moves no threshold",
    "with channels listed, an object shows when a channel it is seen in is listed and on",
    "a line's remove takes one channel off; the last one removed is every channel again",
    "a category's eye hides its objects and disables its slider",
    "the hit test returns every shown object under the point, topmost first, edges included",
    "a click on one object selects it and switches on a slot already holding its channel",
    "otherwise one slot is kept for the section and reused, never Registration's pair nor the nuclear slot",
    "several objects under a click open a menu to choose; nothing is selected until one is chosen",
    "choosing one selects it; a second click at the same spot steps to the next",
    "the progress bar shows the job's share and phase, and Play is held",
    "a finished run is refreshed and its objects fetched once per result",
    "a running job in the status is followed",
    "stale and failed runs say so in the note",
    "the settings menu writes the kept regions to ROI QC after a confirm",
    "summary() reports what get_state needs",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def test_the_script_parses():
    done = subprocess.run([node, "--check", str(SCRIPT)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
