"""The drawing tools' bar on the image (core's services/drawToolbar.js), run
in node against the shipped scripts with the real RoiInteraction behind it,
and as the QC panel builds it.

The checks live in tests/js/draw_toolbar_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing what
is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "draw_toolbar_probe.mjs"
SCRIPT = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services" / "drawToolbar.js"

CHECKS = (
    "the bar is built when first shown, over the image, with five tools and none lit",
    "hidden, it leaves the image",
    "a tool button picks its tool, and the bar lights it",
    "what cannot be picked is disabled, but never the tool in hand",
    "the wand opens magic select's options out of the bar, in Box",
    "a mode on the bar is magic select's mode",
    "the wand clicked again folds them away, back to no tool on a fresh panel",
    "...and back to the tool that was in hand before it",
    "another tool picked while they are out folds them too",
    "the wand breathes while an outline is on its way",
    "Through planes is offered on a z-stack, with Plexora 3D, and only there",
    "disarming the tools folds the options",
    "a button's name shows under the bar while the pointer is on it, with no system tooltip",
    "options opened before the bar is on screen are out from its first frame",
    "an owner with a category gets it before the tools, named and coloured, and × after",
    "the category asks its owner, × closes, and a tool is picked",
    "the ROI panel's bar has neither",
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
