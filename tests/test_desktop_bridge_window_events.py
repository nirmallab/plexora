"""The desktop bridge with two app windows open: an event the shell sends to
one window (a native menu item, a finished download, a file opened from
Finder or Explorer) is handled by that window only.

The shell sends these with `emit_to(<window label>)` (desktop/src-tauri/src/
menu.rs, downloads.rs, opens.rs). A page listener registered with Tauri's
global `event.listen` hears every window's events, so the bridge has to hear
them on its own webview, as it already does for drag and drop. The checks
live in tests/js/desktop_bridge_window_events_probe.mjs, which routes events
the way Tauri 2 does between two stubbed windows; this wrapper runs it and
pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "desktop_bridge_window_events_probe.mjs"

CHECKS = (
    "a menu item runs in the focused window",
    "and not in the other open window",
    "a finished download is announced in the window that downloaded it",
    "and not toasted or notified in the other open window",
    "pending opens are collected by the window they were sent to",
    "and not by the other open window",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize("check", CHECKS)
def test_each_check_ran_and_passed(probe, check):
    assert f"ok - {check}" in probe.stdout.splitlines(), probe.stdout
