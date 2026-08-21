"""Cell outlines derived in the browser, for datasources that store filled labels.

`segmentationMode = "filled"` (the import page's "Outline cells while viewing")
stores the label mask whole and leaves boundary-finding to the client. The
natural place to look for that is frag.glsl's `u32_rgba_map`, and it is the
wrong place: `handleTileLoaded` renders every label tile through
`renderLabelTile()` -- once per layer drawn from it -- into
`tile._layerContexts`, and the tile-drawing handler blits those canvases, so the
shader's `u_tile_fmt == 32` branch never executes for the label layer. The
derivation lives in `renderLabelTile`, and this runs it.

The probe extracts the real function out of imageViewer.js rather than
reimplementing it, and compares its output against a boundary computed with
independent index arithmetic -- the failure this guards against is a plausible
off-by-one in the flat-index neighbour lookups, which would show up as either a
bright grid along every tile seam or an outline that quietly drops pixels.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "label_outline_probe.mjs"


def test_filled_label_tiles_are_reduced_to_cell_boundaries():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe skips silently if it cannot find the function to extract, so
    # assert it actually ran its checks rather than trusting the exit code.
    assert "matches an independently computed boundary" in proc.stdout, proc.stdout
