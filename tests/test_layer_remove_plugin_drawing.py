"""Removing a plugin-drawn layer from the Layers panel stops it being drawn
(tests/js/layer_remove_plugin_drawing_probe.mjs).

A transcripts layer and a Visium HD layer are points layers a plugin claims and
draws itself: a point renderer of its own, a raster through `ctx.layers.addTiled`,
or an overlay on core's OverlayHost tagged with the layer's id. The card's X
deletes the layer on the server and unregisters it from the stack; the picture
has to go with it, or the molecules, spots or bins stay on the image with no
card, eye or opacity left to hide them until the page is reloaded.

The probe asserts internally and exits non-zero on any failure; the lines below
are pinned by name so a check that is quietly dropped fails here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_remove_plugin_drawing_probe.mjs"

CHECKS = (
    "the claimed transcripts layer has a card with an X",
    "the X deletes the transcripts layer on the server",
    "...and the layer leaves the stack and the panel",
    "the transcripts panel stops drawing the molecules of a layer that was removed",
    "the layer's overlay is drawn while the layer is there",
    "the claimed Visium HD layer has a card with an X",
    "the X deletes the Visium HD layer on the server",
    "...and the Visium HD layer leaves the stack and the panel",
    "an overlay drawn for a layer is not drawn once that layer is removed",
    "the Visium HD panel stops drawing the bins of a layer that was removed",
)


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_layer_remove_plugin_drawing_probe_passes(probe):
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "all checks passed" in probe.stdout


@pytest.mark.parametrize("line", CHECKS)
def test_each_layer_remove_plugin_drawing_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout
