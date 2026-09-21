"""The transcript layer's client-side arithmetic, actually run.

`node --check` sees syntax and the Python suite never executes a line of this
plugin's JavaScript, so four things are wrong in ways nothing else here can
see -- and each of them produces a picture that looks like data rather than
like a bug. The probe (tests/js/transcript_points_probe.mjs) says which four
and why; this runs it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "transcript_points_probe.mjs"


def test_the_client_arithmetic_holds():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)

    assert proc.returncode == 0, proc.stdout + proc.stderr
