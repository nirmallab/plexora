"""The website's pool of helper Pythons for contrast fits
(services/fitHelpers.js; tests/js/fit_helpers_probe.mjs): how many a machine
runs, one loading at a time from Python's own bundle, a fit's states, a failed
helper's fits failing so Python fits for itself."""

import shutil
import subprocess
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parent / "js" / "fit_helpers_probe.mjs"


def test_the_pool_of_helper_pythons():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "fit helpers: all ok" in done.stdout
