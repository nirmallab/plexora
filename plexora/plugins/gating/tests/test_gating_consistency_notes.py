"""The mismatch notes the Thresholding panel draws under its distribution plot.

Core decides whether a project's table, mask and image describe the same sample
(`server/models/consistency.py`, and `tests/test_consistency.py` covers the
findings themselves). This is the other half: what the panel does with them,
plus the one finding core cannot have -- that the marker on screen is not an
image channel, which is a question about the marker the user just picked.

The checks are in `tests/js/gating_consistency_probe.mjs`, because what they
assert is what ends up in the DOM. This drives the probe and names every check,
so one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_consistency_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "a project that agrees leaves no empty block behind",
    "core's findings are drawn, worst first, as core ordered them",
    "a marker with no image channel is said so, after core's findings",
    "...and not on a project whose two vocabularies never overlap",
    "the overlap is decided once and not per marker",
    "repainting replaces the notes rather than stacking them",
    "a finding with no code still draws its sentence",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)


def test_the_notes_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
