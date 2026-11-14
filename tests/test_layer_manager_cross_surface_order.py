"""The Layers panel with a raster stored above a points or surface layer
(tests/js/layer_manager_cross_surface_order_probe.mjs).

The panel groups its cards by surface; the stack it reads keeps the order
layers were added in. A surface and then a pinned projection, or a claimed
transcripts layer and then an H&E, leaves the two disagreeing, and the panel
has to render the grouped list once and stop -- not recurse until the call
stack overflows, which during boot stops the viewer opening at all.

The probe asserts internally and exits non-zero on any failure; the lines
below are pinned by name so a check that is quietly dropped fails here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_manager_cross_surface_order_probe.mjs"

CHECKS = (
    "a raster stored above a surface does not stop the panel's init",
    "...and the cards come out grouped by surface, the surface first",
    "an opacity change afterwards repaints without a listener failing",
    "...and the eye state still reaches the card",
    "a project with a claimed points layer boots the panel",
    "an image added above a claimed points layer repaints without a listener failing",
    "...and the points card stays above the image cards",
)


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_cross_surface_order_probe_passes(probe):
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "all checks passed" in probe.stdout


@pytest.mark.parametrize("line", CHECKS)
def test_each_cross_surface_order_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout
