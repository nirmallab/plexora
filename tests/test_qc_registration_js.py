"""The Registration Check's browser half, run in node against the shipped script.

The checks live in tests/js/qc_registration_keys_probe.mjs; this pins their
lines, so a check that is deleted or renamed fails here rather than silently
reducing what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_registration_keys_probe.mjs"

CHECKS = (
    "turning it on puts the pair in slots 1 and 2 and leaves slot 3 alone",
    "a slot the user coloured keeps its colour",
    "flicker runs by default and repaints only the overlay, never a channel",
    "Z and X step the comparison while QC is the tool",
    "F toggles the flicker through the server state",
    "the keys stand down while typing, in a dialog, or under another tool",
    "stopping the flicker stops the timer and leaves both channels as they were",
    "a hidden tab, a lost focus, or a hidden panel stops the timer",
    "turning it off puts slots 1 and 2 back and stops everything",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
