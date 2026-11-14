"""A `set_channels` the agent was asked to save survives the end of the run.

tests/js/agent_bridge_persist_restore_probe.mjs runs the real
services/agentBridge.js: the agent replaces the channels with `persist: true`,
the run ends (`restore_viewer`), and the user then edits a colour by hand. The
saved arrangement must stay what the screen shows, and the user's next save
must not write the pre-session channel list back over it.

Skipped without node, like every other probe wrapper.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "agent_bridge_persist_restore_probe.mjs"

CHECKS = (
    "set_channels persist:true is done and says it persisted",
    "...and the project's channel list on disk is the agent's",
    "restore_viewer is done and had a lease",
    "after the restore the screen still shows the arrangement the agent saved",
    "after the restore the screen and the saved channel list agree",
    "the user's next save keeps the persisted channels (it does not write the pre-session list back)",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)


def _report(probe):
    start = probe.stderr.find("{")
    assert start >= 0, f"no report from the probe:\n{probe.stdout}\n{probe.stderr}"
    return json.loads(probe.stderr[start:])


def test_the_probe_ran_every_check(probe):
    assert _report(probe)["checked"] == len(CHECKS), probe.stderr


@pytest.mark.parametrize("name", CHECKS)
def test_a_persisted_channel_arrangement_outlives_the_restore(probe, name):
    failed = {failure["name"]: failure for failure in _report(probe)["failures"]}
    assert name not in failed, json.dumps(failed[name], indent=2)
