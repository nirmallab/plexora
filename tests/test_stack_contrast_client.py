"""How the sidebar sets contrast on a z-stack (tests/js/stack_contrast_probe.mjs).

A light-sheet stack whose tissue sits under 1000 beside saturated voxels at
65535 had two 8-bit levels across its default range, and its projections and
3-D view were drawn with the single plane's window. The probe runs the shipped
sidebar methods that turn HD on for such a stack and that choose which view's
statistics auto-contrast reads.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "stack_contrast_probe.mjs"


def test_stack_contrast_follows_the_view_and_asks_for_hd():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    for line in (
        "a stack whose default range spans two byte levels asks for HD",
        "a stack whose default range spans 69 byte levels stays on fast tiles",
        "a flat image, an HD page and a scoped sidebar are left alone",
        "a plane reads the channel's statistics, a projection its own, the 3-D view its level's",
        "the 3-D view's hints level only channels the user has not set, and only in 3-D",
    ):
        assert line in proc.stdout, proc.stdout
