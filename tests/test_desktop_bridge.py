"""The desktop app's page-side bridge (services/desktopBridge.js).

The bridge decides, in JavaScript, everything the shell's native features do
to a page: which element a menu item clicks, where a dropped path goes, which
chords the page runs for the shell. None of it is visible from Python, so the
checks live in tests/js/desktop_bridge_probe.mjs, run against the shipped file
with a stubbed shell. This wrapper runs it and pins its lines, so an edit that
quietly drops a check does not read as a pass.

The shell's own half is tested in Rust (`cargo test` under desktop/src-tauri)
and end to end by `scripts/release.py validate`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "desktop_bridge_probe.mjs"

CHECKS = (
    'a browser tab gets PlexoraDesktop === null',
    'and no is-desktop class',
    "the app's window gets the bridge",
    'and the is-desktop class',
    "the bridge's methods cannot be swapped out from under its callers",
    "the shell's dialogs are one kind at a time",
    "File > Settings clicks the navbar's Settings row",
    "a Tools row clicks that tool's own menu row",
    'a tool with no row on this page says to open a sample',
    'a view that claims a menu item stops the default click',
    'drop positions are converted from physical to CSS pixels',
    'an unclaimed drop opens the Import dialog with the paths',
    'a drop a view claims goes nowhere else',
    'dragging a file over the window marks the page',
    'and leaving unmarks it',
    'Ctrl+N opens a window from the page on Windows',
    'Ctrl+W closes this window',
    'Ctrl+Shift+B opens this page in the browser',
    'a chord the page owns is left to the page',
    'saveBlob sends raw bytes',
    'with the name percent-encoded in a header',
    'and says where the file went',
    'a project handed over by a second launch is opened',
    'on macOS the menu owns Cmd+N and the page leaves it alone',
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
