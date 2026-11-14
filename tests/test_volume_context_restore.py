"""The 3-D view draws again after its WebGL context is lost and restored.

tests/js/volume_context_restore_probe.mjs runs views/volumeRenderer.js in node
against a WebGL2 context that models loss and restore: after the browser gives
the context back, opening 3-D again must draw on the live context -- a quad
buffer of that context, levels uploaded with UNPACK_ALIGNMENT 1 into its
textures, no INVALID_OPERATION -- and raise `plexora:volume-mounted` again so
the surfaces and the handles hook the view.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "volume_context_restore_probe.mjs"

CHECKS = (
    "the 3-D controller attaches",
    "3-D opens and mounts once",
    "the first levels upload cleanly",
    "a lost context hands the viewer back to planes",
    "the renderer asked for the context back",
    "plexora:volume-mounted is raised again, so surfaces and handles hook the live view",
    "the quad the ray caster binds is a buffer of the live context",
    "the levels are uploaded again after the restore",
    "every level is uploaded with UNPACK_ALIGNMENT 1, into a texture of the live context",
    "no INVALID_OPERATION after the restore",
)


def test_the_3d_view_draws_again_after_its_context_is_restored():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    for label in CHECKS:
        assert f"ok - {label}" in proc.stdout, out
