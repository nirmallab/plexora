"""Magic select's floating bar (services/magicToolbar.js): one bar, shared by
the ROI and QC tools, taken over rather than stacked, run in node against the
shipped script.

The checks live in tests/js/magic_toolbar_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "magic_toolbar_probe.mjs"
SERVICES = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services"
SCRIPTS = [SERVICES / name for name in ("magicToolbar.js",)]

CHECKS = (
    "MODES are add, remove, box and scribble",
    "show mounts one toolbar in the viewer's wrapper, Add lit",
    "a mode or the close button reports to the owner and never reaches the image",
    "setMode lights one radio; setBusy is aria-busy on the root",
    "a second owner's show takes the bar over rather than stacking",
    "...and the first owner's stale handle can neither repaint nor hide it",
    "hide by the owner takes it off the image",
    "shown again, it is the same one bar, back in the wrapper",
    "with no viewer on the page there is no bar",
    "nothing on it names the model",
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
