"""The small action menu (views/popoverMenu.js) and the plugin help content
(views/pluginHelp.js).

The checks are in `tests/js/popover_menu_probe.mjs`, which runs both files
against a DOM stand-in. This drives the probe and names every check, so one
quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "popover_menu_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    'a menu of items and a separator, with their roles',
    'it hangs under its button, right-aligned, and says it is open',
    'it stays on screen and flips above when there is no room below',
    'choosing an item closes the menu, then runs it',
    'a disabled item does nothing',
    'the opening click does not shut it; the next one does',
    'Escape shuts it and goes no further',
    'one menu at a time',
    'a second click on the open anchor closes it',
    'a row of icon actions carries its label as text and runs the one clicked',
    'a disabled action does nothing',
    'a label is text, never markup',
    "help lists the plugin's keys and then core's open/close row",
    'a chord is printed per platform, and several keys share a row',
    'notes are text bullets, and a tool with nothing extra gets no block',
    'open hands the summary and the table to the confirm dialog',
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
