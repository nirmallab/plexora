"""Thresholding a z-stack plane by plane, in the browser: the gating panel's
plane control (plugins/gating/static/gatingPlanes.js), run in node.

A gate is always applied to each cell's whole-cell value; the panel's slab only
changes what the image shows (the viewer's following, clipped projection --
tests/js/view_state_probe.mjs pins that half). The checks in
tests/js/gating_planes_probe.mjs pin the panel's: that a plane's "positive"
count is the gate's calls as drawn, that Slab asks the viewer for the right
planes, and what the readout claims. This pins their lines, so a check that is
deleted or renamed fails here. The cells on a plane themselves are
tests/test_plane_cells_route.py's.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "gating_planes_probe.mjs"
SCRIPTS = (
    "plexora/plugins/gating/static/gatingPlanes.js",
    "plexora/plugins/gating/static/gatingSidebarController.js",
)

CHECKS = (
    "positives are the gate's calls as the viewer drew them",
    "without the viewer's table, the values under low < v <= high",
    "the plane counted is the one whose outlines are drawn",
    "Slab asks the viewer for a following, clipped projection; Plane takes it back",
    "microns become planes centre to centre, from the z step alone",
    "without a known z step the thickness is in planes",
    "the slab comes back as the user left it",
    "the plane's cells are read once per plane and marker",
    "the planes ahead of the hand are read once it is still",
    "the plane's shading: on the plot's own axis, smoothed, NaN left out",
    "smoothing keeps a lone bin's shape and does not dim the ends",
    "the readout says what the gate uses and what the image shows",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_scripts_parse(script):
    done = subprocess.run([node, "--check", str(REPO_ROOT / script)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert lines == list(CHECKS)
