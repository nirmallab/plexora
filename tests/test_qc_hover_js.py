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
    "a region QC named itself is called by its subtype on a cell's card",
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
    "with the cell layer on, the cell under the pointer is asked for at once",
    "the cell's card wins over the region's, and says a click opens the region",
    "moving anywhere over a cell already seen asks nothing more",
    "while one question is in flight no other is sent",
    "...and the card shown stays until the answer lands",
    "then only the newest point is asked about, and its cell shown",
    "a cell answered on the way is in hand too",
    "a clean cell inside a region leaves the region's card",
    "glass is asked about once, not on every small move",
    "a press hides the card",
    "...and so does a drag",
    "...and a wheel",
    "no card and no question while a stroke is drawn or the ROI tool is up",
    "three failures in a row pause cell questions, with one warning",
    "off every region a cell's card says a click shows its call",
    "a click on a flagged cell shows that cell's call in the panel",
    "a click where the card showed the nearest cell shows that cell",
    "a click inside a region opens the region, even on a flagged cell",
    "...and stops OSD's own click-to-zoom from moving it off centre",
    "...not a drag's release, nor a click on nothing, which keeps OSD's own behaviour",
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
