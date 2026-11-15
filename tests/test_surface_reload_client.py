"""An edit to a surface while its mesh is still downloading
(services/surfaceLayers.js reload, tests/js/surface_reload_probe.mjs): the
download asked for before the edit brings the pre-edit mesh, which must not be
drawn; the edited version is fetched after it and drawn.

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "surface_reload_probe.mjs"


def test_an_edit_during_a_mesh_download_draws_the_edited_mesh():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "all checks passed" in proc.stdout, out
    assert proc.stdout.count("ok - ") == 4, out
