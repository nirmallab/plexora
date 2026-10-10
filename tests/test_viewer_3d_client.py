"""The 3-D viewer's client probes: what the plane, the sections and the 3-D
view show while the user moves through a stack.

Each probe runs the real classic script in node against fakes it owns:

- plane_flash_probe.mjs: a plane step never shows a frame outside the two
  planes it joins (the fade's ghost is an opaque copy of ground + canvas);
- gl_init_probe.mjs: the tile shaders are fetched and compiled once per page,
  however often the viewer re-raises `open`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(probe_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / probe_name)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _passed(out, at_least):
    assert "checks passed" in out, out
    assert out.count("ok - ") >= at_least, out


def test_a_plane_step_never_flashes():
    _passed(_run("plane_flash_probe.mjs"), 10)


def test_the_tile_shaders_are_compiled_once_per_page():
    _passed(_run("gl_init_probe.mjs"), 9)
