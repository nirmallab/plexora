"""The 2-D tile renderer recovers from a failure once, and keeps no stale pixels.

gl_tile_render_recovery_probe.mjs runs the real GLRenderer (services/glRenderer.js)
and glInit.js against a fake WebGL2 context, and forceRepaint sliced from
imageViewer.js:

- a provisional (coarse) section tile and the final tile at the same address
  are two textures, so the final bytes are uploaded rather than the coarse
  texture bound in their place;
- a shader that fails to fetch, or a program that does not link, makes
  ready() refuse, and the next call builds it again -- never the shader's URL
  compiled as GLSL and kept for the page;
- a repaint whose loadBuffers failed puts `ready` back, so later repaints run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "gl_tile_render_recovery_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "the final bytes of a provisional section tile are uploaded, not bound from the coarse texture",
    "...and the final tile, drawn again, is a cache hit",
    "a shader that fails to fetch is not compiled: its URL is not GLSL",
    "...and ready() refuses rather than keeping a broken program",
    "the next ready() fetches again and builds from the shaders' source",
    "a program that does not link is refused, and the next ready() builds it again",
    "a repaint after one whose loadBuffers failed still runs",
    "...and the failed one still redraws what is there, without an unhandled rejection",
    "...and forgets the markers it could not load, so the next one fetches them again",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT, timeout=60,
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_passed(probe, line):
    assert f"ok - {line}" in probe.stdout, f"{probe.stdout}\n{probe.stderr}"


def test_no_check_was_quietly_dropped(probe):
    assert f"all {len(CHECKS)} checks passed" in probe.stdout
