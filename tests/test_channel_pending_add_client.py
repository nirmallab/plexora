"""A channel switched off before its pair has landed stays off.

OpenSeadragon adds an item a task or more after ``addTiledImage`` is called
(a setTimeout, the tile source's ``ready``, then its add queue in order), so
``ViewerManager.channel_add`` has a window in which the world holds nothing of
the channel. The probe runs the real ViewerManager in node against a world
that lands its adds only when told to: a channel switched off in that window,
or the base layer's eye closed in it, must not come back on screen when the
pair lands -- with no slot the colorize pass draws it white at the full range
and nothing can take it down.

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "channel_pending_add_probe.mjs"

CHECKS = (
    "a channel switched off before its pair landed leaves nothing of it on the world",
    "and nothing of it in the reference layer",
    "the channel still on is untouched",
    "a pair that lands after the base eye closed is not put on screen",
)


def test_a_channel_switched_off_before_its_pair_lands_stays_off():
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
