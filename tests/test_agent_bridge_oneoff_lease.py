"""A one-off viewer command from an external agent gives the viewer back.

tests/js/agent_bridge_oneoff_lease_probe.mjs runs the real
services/agentBridge.js under node. An agent that calls viewer_navigate or
viewer_open_tool once (Claude Code through the plexora MCP server) sends one
mutating command and never `restore_viewer`; the command leases the viewer,
and the lease suspends the sidebar's channel saves. Once the server has said
the agent is no longer attached for longer than the attach TTL, the tab must
hold no lease, and a channel edit the user makes must be saved as any is.

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "agent_bridge_oneoff_lease_probe.mjs"

CHECKS = tuple(
    f"{label}: {line}"
    for label in ("viewer_navigate", "viewer_navigate to a cell", "viewer_open_tool")
    for line in (
        "the agent's one command is run and acknowledged done",
        "once the agent has been detached past the attach TTL, the tab holds no lease",
        "a channel edit the user makes afterwards is saved to the project",
    )
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_a_one_off_agent_command_does_not_keep_the_users_channel_edits_unsaved(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert probe.stdout.count("PASS ") + probe.stdout.count("FAIL ") == len(CHECKS)
