"""The project edit page's Image Scale control starts on a routed visit too
(views/projectEdit.js as a PlexoraPage controller, named from the page's own
project, not the viewer's flaskVariables) -- tests/js/project_edit_scale_probe.mjs.

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "project_edit_scale_probe.mjs"


def test_the_image_scale_control_starts_on_every_visit():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert proc.stdout.count("ok - ") == 3, out
