"""Z and X step the Thresholding marker, and stand down when they should.

The checks are in `tests/js/gating_marker_keys_probe.mjs`, which calls the real
controller methods. This drives the probe and names every check, so one
quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_marker_keys_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "X steps to the next marker, as a pick from the list would",
    "Z steps to the previous marker",
    "Caps Lock still steps",
    "at either end the key does nothing and is not swallowed",
    "with nothing selected X picks the first marker",
    "typing into the marker search is typing",
    "a dialog owns the window",
    "the keys belong to the selected tool only",
    "a modified Z is somebody else's shortcut",
    "put away, the panel stops listening",
    "arming is idempotent and disarming removes the listener",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)


def test_the_marker_keys_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
