"""Blur QC's browser half, run in node against the shipped script.

The checks live in tests/js/qc_blur_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_blur_probe.mjs"
SCRIPT = REPO_ROOT / "plexora" / "plugins" / "qc" / "static" / "qcBlur.js"

CHECKS = (
    "the first three DNA channels are listed, each with its own slider and colour",
    "a row's slider previews its own channel's mask and never stores",
    "the release stores that channel's threshold once, typed in full",
    "a value from outside moves only its own slider, and none is echoed back",
    "a colour picked is that channel's and recolours its slider",
    "+ adds an empty line whose pick lists the channel and, the others measured, measures it",
    "a line's select swaps its channel in place",
    "a line's remove takes only that channel off the list",
    "the heatmap, the regions and each channel's eye are toggled independently",
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
