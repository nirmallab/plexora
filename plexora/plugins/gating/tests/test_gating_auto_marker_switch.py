"""Auto's fit lands on the marker it was pressed on, even if the marker moved.

The checks are in `tests/js/gating_auto_marker_switch_probe.mjs`, which calls
the real controller methods (onAutoClick, autoGate, setGateRange, stepMarker)
and CSVGatingList#getGatingGMM, with the server's fit answered by hand after
Z / X has moved the panel to another marker. That marker's hand-set gate must
stay as it is and must not be saved. This drives the probe and names every
check, so one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_auto_marker_switch_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "the fit asked for is the marker Auto was pressed on",
    "X while the fit is in flight leaves the next marker's hand-set gate alone",
    "X while the fit is in flight schedules no save of the next marker",
    "a gate that did change under Auto can always be reverted",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_auto_marker_switch_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
