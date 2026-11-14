"""N during a classifier review labels the cell negative; it never walks the
dataset chip to the next sample.

The review answers P, N, S and Esc (gatingClassifier.js); the dataset chip
(views/datasetNav.js) walks to the next sample on a bare N unless the key was
already taken. Both listen on `document`, and the chip's listener is added at
page load, before the classifier's whenever the tool is opened from the menu.

The checks are in `tests/js/classifier_review_keys_probe.mjs`, which loads both
real files into one page and dispatches keydown in the browser's order. This
drives the probe and names every check, so one quietly deleted fails here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "classifier_review_keys_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "N during a review labels the cell negative and does not walk to the next sample",
    "N during a review still holds after the panel was hidden and shown again",
    "with no review going, N is the chip's and walks to the next sample",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_review_keys_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
