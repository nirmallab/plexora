"""Magic select in the ROI tool (roiTools.js's "magic" tool, key E), run in
node against the shipped scripts.

The checks live in tests/js/roi_magic_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "roi_magic_probe.mjs"
SCRIPTS = [REPO_ROOT / "plexora" / "client" / "src" / "js" / "services" / "segmentService.js",
           REPO_ROOT / "plexora" / "plugins" / "roi" / "static" / "roiGeometry.js",
           REPO_ROOT / "plexora" / "plugins" / "roi" / "static" / "roiTools.js"]

CHECKS = (
    "a fresh panel holds magic select in Box, and arming it starts the setup",
    "...and a server that cannot run the model gets freehand instead",
    "'Hold Space to pan' is up while a drawing tool is in hand, and only then",
    "arming magic select starts the one-time setup, once",
    "a click makes one region, filed as magic select's, and selects it",
    "a second click near it refines that region, not a new one",
    "a Shift-click adds an excluding point to the same outline",
    "Esc ends the outline being made",
    "...so the next click far away starts a new region",
    "with no category and nothing selected, a click says why and asks nothing",
    "a click inside a locked selected region says it is locked and asks nothing",
    "a click inside a selected region refines it, with no category needed",
    "an empty answer stores nothing and says so",
    "a click while one is still outlining is ignored",
    "Remove inside a selected region carves it, seeded with its own outline",
    "...and so does a Shift-click in Add",
    "Remove with nothing to take from says so and asks nothing",
    "in Add a drag pans: no box, and the viewer keeps the drag",
    "in Box a drag draws a box and prompts with it",
    "...and a click in Box does nothing",
    "in Scribble a drag is a line, sent as up to eight include points along it",
    "a Shift-scribble begun inside the outline being made takes its line away",
    "a scribble that finds nothing is taken back whole",
    "...and a tap with the brush is one point",
    "the bar shows with magic select (in Box) and goes with any other tool",
    "a mode from the bar keeps the outline being made",
    "the bar's close goes back to the tool in hand",
    "disarming hides the bar; arming with magic in hand shows it again",
    "busy is shown on the bar while an outline is on its way",
    "E toggles magic select on and back to the tool in hand",
    "a freehand stroke is still filed as freehand, self-intersection recorded",
    "...and a rectangle as rectangle",
    "drawing into a hidden category shows it again, in the same undoable step",
    "dragging a vertex of a magic-select region keeps it magic select's",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_scripts_parse(script):
    done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_the_probe_parses():
    done = subprocess.run([node, "--check", str(PROBE)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
