"""The 2-D tile renderer comes back after its WebGL context is lost.

gl_tile_context_restore_probe.mjs runs the real GLRenderer (services/glRenderer.js)
and glInit.js in node against a fake WebGL2 context, loses the context and
restores it. The renderer must claim the loss (preventDefault, or the browser
never restores it), then rebuild its program, drop the dead tile textures, make
its texture and buffer again and redraw -- otherwise every channel tile draws
blank until the page is reloaded.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CHECKS = (
    "before the loss: the program is built once and two tiles are resident",
    "the loss is claimed (preventDefault) so the browser will give the context back",
    "something listens for webglcontextrestored on the tile renderer's canvas",
    "the tile program is compiled and linked again on the restored context",
    "no tile texture from the lost context is left in the cache to be bound",
    "the renderer's own texture and quad buffer are made again",
    "the viewer is asked to redraw after the restore",
)


def _run():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / "gl_tile_context_restore_probe.mjs")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    return proc


def test_the_tile_renderer_recovers_from_a_lost_webgl_context():
    proc = _run()
    out = proc.stdout + proc.stderr
    for label in CHECKS:
        assert f"ok - {label}" in out, out
    assert proc.returncode == 0, out
    assert f"all {len(CHECKS)} checks passed" in out, out
