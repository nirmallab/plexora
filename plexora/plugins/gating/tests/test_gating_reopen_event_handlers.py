"""Thresholding closed with its card's X and opened again leaves nothing of
the old instance listening on the page's event bus.

The checks are in `tests/js/gating_reopen_events_probe.mjs`, which runs the
real gating definition's `bindEvents` (csvGatingList.js) on the real
SimpleEventHandler through main.js's activate/deactivate contract: open,
close, open again, then fire a gate commit, a brush tick, Load Gates and a
Cells-mode change. Each must reach the open instance once and a closed one
never. This drives the probe and names every check, so one quietly deleted
fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_reopen_events_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "the resize listener of a closed Thresholding is removed",
    "a gate commit after reopening runs the canvas gate once",
    "a brush tick after reopening runs the canvas gate once",
    "Load Gates resets only the open instance, never a closed one",
    "with Thresholding closed, a Cells-mode change reaches no gating handler",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_reopen_events_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
