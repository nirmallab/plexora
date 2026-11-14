"""A tool asked for again while its first open is still loading is loaded once.

toolLoader.js puts a tool in `loadedTools` only when loadToolUntimed finishes,
so while the panel, scripts and activatePlugin are in flight `isToolOpen()` is
false and openTool's "already loaded" guard misses. A second request in that
window -- the shortcut pressed again while the placeholder card is up, the
sidebar launcher, or the agent bridge's open_tool beside the user's click --
runs a whole second open: the panel is fetched and written over the same
mount again, the header actions are lifted into the card twice, and a
registered definition is activated twice, leaving two plugin instances and two
sidebar controllers of which main.js's record keeps only the last.

tests/js/tool_double_open_probe.mjs holds the panel fetch open by hand, so the
second request always lands inside the first open.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tool_double_open_probe.mjs"


@pytest.fixture(scope="module")
def probe_report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_a_second_press_while_a_tool_loads_does_not_load_it_again(probe_report):
    """The row (and so its shortcut), then the launcher, before the first open
    finished: one panel fetch and one activation, not two."""
    _, report = probe_report
    pressed = report["pressed"]
    assert pressed["fetches"] == 1, pressed
    assert pressed["activations"] == 1, pressed


def test_a_tool_opened_twice_at_once_gets_its_header_actions_once(probe_report):
    """A second lift appends a second, dead set of header actions with the
    same element ids as the first."""
    _, report = probe_report
    assert report["pressed"]["extras_lifted"] == 1, report["pressed"]


def test_the_agent_and_the_user_opening_one_tool_share_one_load(probe_report):
    """open_tool from the agent bridge while the user's open is loading: both
    callers are told the tool loaded, and it was activated once."""
    returncode, report = probe_report
    raced = report["raced"]
    assert all(outcome.get("loaded") for outcome in raced["outcomes"]), raced
    assert raced["fetches"] == 1, raced
    assert raced["activations"] == 1, raced
    assert not report["problems"] and returncode == 0, report["problems"]
