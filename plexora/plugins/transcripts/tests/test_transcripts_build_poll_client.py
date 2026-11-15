"""The Transcripts panel client: the build poll ends on `missing`, an unreadable manifest is not a rebuild, and carried display controls reach the renderer."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "transcripts_build_poll_probe.mjs"


def test_a_missing_layer_stops_the_build_poll():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_carried_display_controls_reach_the_renderer():
    """tests/js/transcripts_carry_style_probe.mjs: size, opacity, point style
    and minQ carried from a sibling sample are pushed, not only stored."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    probe = REPO_ROOT / "tests" / "js" / "transcripts_carry_style_probe.mjs"
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
