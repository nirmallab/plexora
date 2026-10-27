"""Settings > Usage data, run in node against the shipped settingsPage.js.

The checks live in tests/js/settings_telemetry_probe.mjs; this pins their
lines, so a check that is deleted or renamed fails here rather than silently
reducing what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "settings_telemetry_probe.mjs"

CHECKS = (
    "a refused registration is shown under the install id",
    "Send now says the install is not registered, and why",
    "...and gives the button back",
    "a healthy install says when it last sent, and nothing about a refusal",
    "with no telemetry service configured, there is no refusal to report",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
