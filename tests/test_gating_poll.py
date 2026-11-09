"""The viewer's auto-contrast poll asks for a channel switched on while it
sleeps at once (tests/js/gating_poll_probe.mjs)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parent / "js" / "gating_poll_probe.mjs"


def test_a_channel_switched_on_while_the_poll_sleeps_is_asked_for_at_once():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "asked for at once" in done.stdout
