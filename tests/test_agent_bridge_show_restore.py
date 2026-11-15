"""A local restore during an `animate_contrast` show ends the show.

tests/js/agent_bridge_show_restore_probe.mjs runs the real
services/agentBridge.js (audit leads cl-agent-ui-1 and cl-agent-ui-2):

  A. Stop mid-show: the show's later moves must not land on the user's
     restored channel (they address the slot by index), the queued marker is
     never staged, and the user's next save writes the user's own window.
  B. "Continue in background" mid-show: a look held behind the show must not
     run once the show is over, take a new lease, or leave channel saving
     suspended.

Skipped without node, like every other probe wrapper.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "agent_bridge_show_restore_probe.mjs"

CHECKS = (
    "A: the local restore had a lease and rebuilt the channels",
    "A: the user's CD3 is back in the second slot with its own window",
    "A: no show move lands after the restore",
    "A: the queued CD8 is never staged",
    "A: saving is the user's again and no lease is held",
    "A: the user's next save writes CD3's own window, not the show's",
    "B: the session's set_channels during the show is held",
    "B: the detach restore had a lease",
    "B: the held look does not come back after the show",
    "B: no new lease is taken once the show is over",
    "B: channel saving is not left suspended",
    "B: the user's next channel edit is saved",
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
def test_a_restore_ends_the_contrast_show(probe, name):
    failed = {failure["name"]: failure for failure in _report(probe)["failures"]}
    assert name not in failed, json.dumps(failed[name], indent=2)
