"""The client keys a centroid by the tile the server filed it under, past
the image edge included (tests/js/centroid_tile_clamp_probe.mjs)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "centroid_tile_clamp_probe.mjs"


def test_a_centroid_past_the_image_edge_is_kept_in_the_last_tile():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report == {"0/0/0": [1, 5], "0/1/0": [2], "0/1/1": [3, 4]}, report
