"""Per-cell colour in the viewer: the renderer, and who is allowed to set it.

Two node probes, run here so they are part of the suite rather than something
somebody remembers to invoke. Both extract the real functions out of
imageViewer.js instead of reimplementing them -- a copy would pass happily while
the shipped code was wrong, which is the specific failure this whole area is
prone to (the shader has a `u_tile_fmt == 32` branch that looks like it draws
the label layer and never runs).

The assertion worth understanding is the first one. The cell layer predates
colouring: Thresholding draws white outlines through the same function, and so
does a plain viewer with no tool open. So the no-colour output is compared byte
for byte against the pixels the old code wrote. Nothing weaker would catch a
colour path that shifts white to 254 or drops the layer's alpha from 220 -- both
of which look fine on screen and are a silent regression for every existing
project.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tests" / "js"


def _run(probe):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE_DIR / probe)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_a_viewer_with_no_cell_colours_renders_exactly_as_it_did():
    """The guard for every project that does not use a cell-colouring plugin."""
    output = _run("cell_color_probe.mjs")
    # The probe exits 0 if it cannot find what to extract, so assert the checks
    # that matter actually ran rather than trusting the exit code.
    assert 'null LUT is byte-identical white on a "filled" pyramid' in output, output
    assert 'null LUT is byte-identical white on a "outlines" pyramid' in output, output


def test_a_colour_table_recolours_cells_without_moving_them():
    output = _run("cell_color_probe.mjs")
    assert "colouring draws exactly the same boundary pixels as white did" in output, output
    assert "filled mode paints interiors in the cell's colour" in output, output
    assert "an alpha-0 cell is not drawn at all" in output, output


def test_filled_falls_back_on_a_pyramid_with_no_interiors_to_paint():
    """A mask stored already reduced to boundaries cannot be filled -- there are
    no interior pixels in the file. The control offers it disabled; this pins
    that asking for it anyway renders what it always did rather than something
    different and wrong."""
    output = _run("cell_color_probe.mjs")
    assert "filled on a stored-outlines pyramid falls back to what it can draw" in output, output


def test_gating_and_colouring_stay_separate_channels():
    """Gating filters WHICH cells draw; a colour table says what colour they
    are. Both must work at once, and neither may quietly become the other."""
    output = _run("cell_color_probe.mjs")
    assert "a gate still restricts drawing while a LUT supplies colours" in output, output


def test_only_the_cell_layer_owner_may_set_colours():
    output = _run("cell_layer_claim_probe.mjs")
    assert "a non-owner is refused while someone else holds the layer" in output, output
    assert "a change of owner drops the previous colours" in output, output
    assert "re-claiming the layer keeps the colours" in output, output


def test_the_opacity_control_is_inert_without_a_colouring_plugin():
    """Its default is 0.7. Applying that unconditionally would dim the outlines
    of every existing project the moment this shipped."""
    output = _run("cell_layer_claim_probe.mjs")
    assert "a viewer with no colours composites at full strength" in output, output
    assert "dragging the slider does not re-render tiles" in output, output
