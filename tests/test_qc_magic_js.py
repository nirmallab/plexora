"""Magic select in the QC panel (qcDraw.js's QcMagic and the controller's side
of it), run in node against the shipped scripts.

The checks live in tests/js/qc_magic_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_magic_probe.mjs"
SERVICES = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services"
STATIC = REPO_ROOT / "plexora" / "plugins" / "qc" / "static"
SCRIPTS = ([SERVICES / name for name in ("viewSnapshot.js", "segmentService.js")]
           + [STATIC / name for name in ("qcTree.js", "qcDraw.js", "qcSidebarController.js")])

CHECKS = (
    "methodWords says how an outline was made, never the technical value",
    "originOf: a user's magic-select region is 'magic select', a hand-drawn one 'manual'",
    "currentView is the whole view when the server takes one",
    "...else the legacy channel list",
    "a region that remembers its view gets it back, animated, not a box fit",
    "...and the real restore reaches the scene with {immediately: false}",
    "a region without one is framed by its box",
    "the region menu offers 'Show it as it was drawn' only when it remembers a view",
    "E toggles magic select when the QC panel owns the keyboard",
    "...and is left alone while typing, with a modifier, a dialog, or another tool up",
    "toggleMagic: off when on, on in the category in hand, else the last one reopened",
    "toggleMagic with nothing in hand opens the picker at the header wand, in magic mode",
    "startDrawing puts magic select in hand by default; the pen only when asked",
    "...and the pen where the server cannot run the model",
    "setDrawMode switches the pen and the wand, and does nothing with no category",
    "magicSelected hands the planner the selected region's box and outline",
    "saveGeometry stores a new outline as magic select's, with the view it was made in",
    "QcMagic: arming starts the setup and takes clicks from the viewer",
    "QcMagic: a click commits a new outline, the next one near it grows the same region",
    "QcMagic: with no category it says what to pick, and asks nothing",
    "QcMagic: it yields while the ROI tool is on screen",
    "QcMagic: the bar shows in Box on start and goes on stop",
    "QcMagic: the bar's close is the controller's onClose, else a stop",
    "QcMagic: in Add a drag pans -- no box, the viewer keeps the drag",
    "QcMagic: in Box a drag draws a box and prompts with it; a click does nothing",
    "QcMagic: in Scribble a drag is a line, sent as points along it, drawn as it goes",
    "QcMagic: Remove inside the selected region carves it, seeded with its outline",
    "QcMagic: a plain click inside it refines it, with no seed",
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
