"""Magic select's view record (services/viewSnapshot.js) and the `immediately`
option services/viewerScene.js takes from it, run in node against the shipped
scripts.

The checks live in tests/js/view_snapshot_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "view_snapshot_probe.mjs"
SERVICES = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services"
SCRIPTS = [SERVICES / name for name in ("viewSnapshot.js", "viewerScene.js")]

CHECKS = (
    "capture records the sample, viewport, zoom, HD mode and channels, rounded",
    "only enabled, visible, named channels, at most eight, a bad colour left off",
    "the sample falls back to the page's datasource",
    "a window that cannot be converted to raw is recorded without a range",
    "...but in HD mode the slot's window is raw already and is kept",
    "HD mode is read off the checkbox when the panel cannot say",
    "with no viewer open there is no viewport or zoom, and nothing throws",
    "forStorage spells out width and height and drops the version",
    "sameView: a 0.5 % pan is the same picture",
    "sameView: a 15 % zoom is not",
    "sameView: a changed window, colour, HD mode or sample is not",
    "sameView: colour case does not matter, and w/h spellings compare",
    "key is equal for equal views and differs across channels",
    "contains: every point inside the viewport, edges included",
    "restoreViewport animates by default: {immediately: false} reaches the scene",
    "...and immediately when asked; a degenerate viewport moves nothing",
    "restoreHdMode flips the checkbox and fires one change only when it differs",
    "viewerScene.restoreViewport jumps by default (fitBounds told true)",
    "...and animates with {immediately: false}, upright and turned",
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
