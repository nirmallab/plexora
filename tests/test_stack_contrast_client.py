"""How the sidebar sets contrast on a z-stack (tests/js/stack_contrast_probe.mjs).

A light-sheet stack whose tissue sits under 1000 beside saturated voxels at
65535 had two 8-bit levels across its default range. The probe runs the shipped
sidebar methods that turn HD on for such a stack, and that level a channel once
for every plane, projection and the 3-D view rather than once per view.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "stack_contrast_probe.mjs"


def test_stack_contrast_is_one_per_channel_and_asks_for_hd():
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
        "a plane, a projection and the 3-D view share the channel's one contrast, "
        "with no request of their own",
        "nothing re-levels a channel when the view changes",
    ):
        assert line in proc.stdout, proc.stdout
