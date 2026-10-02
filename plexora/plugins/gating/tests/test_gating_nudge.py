"""‹ and › on the Thresholding plot step the lower threshold, through the slider.

The checks are in `tests/js/gating_nudge_probe.mjs`, which calls the real
controller methods; the step itself is PlexoraSlider#nudge, pinned against an
arrow key in tests/test_slider.py. This drives the probe and names every
check, so one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_nudge_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "› asks the slider for one step up on the lower handle",
    "‹ asks for one step down",
    "a step that went nowhere is reported as one",
    "a click on the plot reaches the button under it",
    "a click leaves the button focused, so the keys can carry on",
    "after a click Right and Up raise, Left and Down lower, one step a key",
    "a key at the end of the track is still taken",
    "other keys, modified arrows and keys elsewhere on the plot are left alone",
    "a click elsewhere on the plot does nothing",
    "with no marker or no slider a click is a no-op",
    "the buttons are off while the slider is",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          # node writes UTF-8; Windows would decode the
                          # check names' ‹ and › as cp1252.
                          text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_nudge_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
