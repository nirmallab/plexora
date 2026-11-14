"""A channel whose first add waits for its volume window is drawn by one pair.

On a volume in 8-bit tiles, ``ViewerManager.addChannelItems`` waits for the
channel's quantization window before its first add, and nothing of the
channel is on the world meanwhile. A plane step, the HD toggle, or the base
eye closed and reopened in that wait finds no items for the channel and asks
for an ordinary add of its own; once the window arrives both adds must not
land, or the channel is drawn by two cover/paint pairs -- its colour added
twice and the ground taken away twice, so it shows at about twice its
brightness against the other channels.

The probe runs the real ViewerManager in node with a window answered only
when told to and a world that lands its adds only when told to.

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "channel_window_wait_double_pair_probe.mjs"

CHECKS = (
    "a plane stepped while a channel's first add waits for its window leaves one pair of it",
    "and one pair of it in the reference layer after the plane step",
    "HD switched on while a channel's first add waits for its window leaves one pair of it",
    "the base eye reopened while a channel's first add waits for its window leaves one pair of it",
)


def test_a_channel_waiting_for_its_window_is_drawn_by_one_pair():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    out = proc.stdout + proc.stderr
    for label in CHECKS:
        assert f"ok - {label}" in proc.stdout, out
    assert proc.returncode == 0, out
    assert "all checks passed" in proc.stdout, out
