"""A channel switched on comes in rather than lands (services/channelFade.js).

The probe runs the real classic script, and the real ViewerManager, in node
against fakes it owns: held at nothing until the channel can draw, then both
halves of its pair together, eased out, to the base card's opacity; ended at
once when switched off or with motion off; and on the plane only a channel
arriving fades -- an HD swap's replacements land in full when it commits.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_channel_switched_on_fades_in():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / "channel_fade_probe.mjs")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checks passed" in proc.stdout, proc.stdout
    assert proc.stdout.count("ok - ") >= 20, proc.stdout
