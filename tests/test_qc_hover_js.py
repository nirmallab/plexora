"""QC's hover card, run in node against the shipped script.

The checks live in tests/js/qc_hover_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_hover_probe.mjs"
STATIC = REPO_ROOT / "plexora" / "plugins" / "qc" / "static"
SCRIPTS = ("qcHover.js", "qcLayers.js", "qcApi.js", "qcSidebarController.js")

CHECKS = (
    "a hand-drawn region says what it is and that it was drawn by hand",
    "...with no subtype row for its category's own class, and its cells",
    "...and a click opens it in the panel",
    "an image check's region explains itself by its score over the bar",
    "...names where the bar came from, the channels and the agent",
    "...and a warning region says it only warns",
    "the agent's own notes lead, the numbers said under them",
    "...and the score is then kept as a row",
    "a detector's region names the detector and the agent's judgment",
    "...is titled by its subtype, with its score, channels and the agent's words",
    "a name the user gave is kept as the title; QC's own generated name is not",
    "a cell QC has nothing to say about gets no card",
    "a flagged cell's card leads with its primary reason, the value and the bar",
    "...the module's notes under it",
    "...its channels and the agent's verdict on that side",
    "...then its other reasons, markers, regions and Segmentation QC, in that order",
    "...a region reason says which region and how much of the cell is in it",
    "...a marker flag its value over its bar",
    "...a region already named is not listed again",
    "...and Segmentation QC's call with its scores and partner",
    "several other reasons are capped, the rest pointed to the panel",
    "a reason or marker whose group is hidden is left off",
    "without calls to read, the panel's groups say what the cell is coloured for",
    "...and a cell in no group gets no card",
    "the card sits below and right of the pointer",
    "...flips left and up at the image's far edges",
    "...and never leaves the image where nothing fits",
    "the card is portaled and never takes the pointer's role",
    "moving about inside one region renders its card once",
    "...with the pointer's client position as the anchor",
    "...hit-testing once per frame",
    "...however many moves arrive within the frame",
    "with the cell layer off the server is never asked about a cell",
    "leaving the region's outline hides the card",
    "the pointer leaving the image hides it",
    "with the cell layer on, nothing is asked until the pointer rests",
    "...then the cell under it is asked for once, with a small radius",
    "the cell's card wins over the region's, and says the region is a click away",
    "a pointer that has barely moved is not asked about again",
    "an answer to an older question is dropped",
    "a clean cell inside a region leaves the region's card",
    "a press hides the card",
    "...and so does a drag, cancelling the cell question",
    "...and a wheel",
    "no card while a stroke is drawn or the ROI tool is up",
    "three failures in a row pause cell questions, with one warning",
    "a click on a region opens it in the panel",
    "...not a drag's release, nor a click off every region",
    "disarming cancels everything and lets go of the viewer",
    "destroying removes the card",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_scripts_parse(script):
    done = subprocess.run([node, "--check", str(STATIC / script)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
