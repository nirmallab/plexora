"""A colour (brightfield / H&E) z-stack changes plane with its slide.

`ViewerManager.setView` on a colour stack swaps the one true-colour base item
(`swapBrightfieldBase`, `handOverBrightfieldBase`) rather than channel pairs.
The probe (tests/js/brightfield_plane_swap_probe.mjs) runs it in node over a
world that answers `addTiledImage` a task later, as OpenSeadragon does: a
navigation revision waits for the slide's new plane, and two quick steps leave
exactly one base item on screen, at the newer plane.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CHECKS = (
    "the colour stack's base is on screen at z10",
    "a revision on a colour stack waits for the slide's new plane",
    "...and the new plane replaces the old once it can draw",
    "a quick second step leaves the newer plane's slide on screen",
    "two quick steps never leave two base images on screen",
)


def test_a_colour_stack_plane_step_waits_for_and_shows_the_newest_slide():
    """The revision's commit waits for the brightfield base at the new plane,
    and a second step started before the first plane's item landed ends with
    one base item, at the second plane -- not the superseded one handed over in
    its place, nor both."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / "brightfield_plane_swap_probe.mjs")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    out = proc.stdout
    for label in CHECKS:
        assert f"ok - {label}" in out, out + proc.stderr
    assert proc.returncode == 0, out + proc.stderr
