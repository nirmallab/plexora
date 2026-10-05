"""The quiet key hint at the bottom of the image (services/canvasHint.js): one
hint, shared by the ROI and QC tools, taken over rather than stacked, run in
node against the shipped script.

The checks live in tests/js/canvas_hint_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "canvas_hint_probe.mjs"
SERVICES = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services"
SCRIPTS = [SERVICES / name for name in ("canvasHint.js",)]

CHECKS = (
    "show mounts one hint in the viewer's wrapper: a key cap and its words",
    "a second owner takes it over, and the first one's hide leaves it up",
    "the owner's hide takes it off the image; shown again it is the same one",
    "with no viewer on the page there is nothing to show",
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
