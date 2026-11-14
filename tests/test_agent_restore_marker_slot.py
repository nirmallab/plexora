"""An agent that only moves Thresholding's marker gives the channel slots back.

`set_active_marker` and `preview_gate` (on a marker other than the one shown)
reach GatingSidebarController.setGateMarker with only `persist: false`, which
mirrors the marker into channel slot 2 over the user's own channel. When the
agent hands the viewer back (`restore_viewer`, Stop, finish) that slot must be
the user's again, and the first channel save once saving resumes must not
write the agent's marker into the project's channel list.

tests/js/agent_restore_marker_slot_probe.mjs runs the real
services/agentBridge.js, the real gatingAgentBridge.js listeners and the real
setGateMarker against a ViewerSidebar stand-in. Skipped without node, like
every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "agent_restore_marker_slot_probe.mjs"

CHECKS = (
    "set_active_marker is claimed by Thresholding and done",
    "...and mirrors the agent's marker into channel slot 2 over the user's CD3 (setup)",
    "restore_viewer is done with the lease it gave back",
    "restore_viewer after set_active_marker puts the user's channel back in slot 2",
    "restore_viewer after set_active_marker returns the panel to the user's marker",
    "the first channel save after set_active_marker's restore does not write the agent's marker",
    "preview_gate on another marker is claimed by Thresholding and done",
    "...and mirrors that marker into channel slot 2 (setup)",
    "restore_viewer after preview_gate puts the user's channel back in slot 2",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)


@pytest.mark.parametrize("line", CHECKS)
def test_restoring_the_viewer_gives_back_the_slot_the_marker_took(probe, line):
    assert f"PASS {line}" in probe.stdout, f"{probe.stdout}\n{probe.stderr}"


def test_the_probe_passes_every_check(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"
    assert probe.stdout.count("PASS ") == len(CHECKS)
