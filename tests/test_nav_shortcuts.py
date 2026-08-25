"""What the navbar's accelerators print, and when they fire.

keyboardShortcuts.js drives both halves from one `data-shortcut` attribute --
the key printed into the menu row and the binding that runs it -- so that the
two cannot drift apart the way a hand-maintained accelerator table does. The
probe runs the shipped file against a DOM stand-in, once per platform, because
the platform is read once at load time and cannot be changed afterwards.

The Python half of the contract (which specs a plugin may declare, and which
the browser will not release) lives in tests/test_plugins.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "nav_shortcuts_probe.mjs"


def test_a_declared_shortcut_prints_and_fires_the_same_chord():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe asserts internally, but pin its lines here too so a future edit
    # that quietly drops a check is not mistaken for a passing test.
    for line in (
        "mac prints the glyph form and paints it into the row",
        "mac fires on cmd and swallows the browser default",
        "mac leaves ctrl, cmd+ctrl and cmd+shift alone",
        "mac ignores strokes aimed at a text field",
        "windows prints the word form and paints it into the row",
        "windows fires on ctrl and ignores meta",
        "a disabled row swallows the default without acting",
        "a clash resolves to the first row and the loser prints nothing",
        "specs normalise the same way the server normalises them",
    ):
        assert line in proc.stdout
