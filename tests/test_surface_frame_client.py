"""A surface is of one frame (services/surfaceLayers.js).

On a time series a surface built at frame t is drawn in 3-D, and its cuts
painted on the plane and in the section panes, at frame t only; another frame
hides it. An imported mesh, of no frame, is drawn on every one
(tests/js/surface_frame_probe.mjs).

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "surface_frame_probe.mjs"


def test_a_surface_is_drawn_on_its_own_frame_only():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "all checks passed" in proc.stdout, out
    assert proc.stdout.count("ok - ") == 6, out
