"""The layer panel's cards, run for real (tests/js/layer_manager_probe.mjs).

The probe asserts internally and exits non-zero on any failure; the lines
below are the Image card's overflow menu, pinned by name so a check that
is quietly dropped fails here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_manager_probe.mjs"

MENU_CHECKS = (
    "the fluorescence Image card has a menu button in its header",
    "...which survives the card being rebuilt",
    "...and clicking it without a menu primitive loaded neither folds nor throws",
    "...which opens two rows of copy and paste glyphs, each sentence a tooltip",
    "a registered layer gets no menu",
)


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_layer_manager_probe_passes(probe):
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "all checks passed" in probe.stdout


@pytest.mark.parametrize("line", MENU_CHECKS)
def test_each_menu_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout
