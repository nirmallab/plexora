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


def test_only_a_registered_plugin_may_set_colours():
    output = _run("cell_layer_registry_probe.mjs")
    assert "a plugin with no layer cannot set colours" in output, output
    assert "a name that was never registered is refused" in output, output
    assert "a removed plugin's late response is refused" in output, output


def test_each_layer_keeps_its_own_colours_gate_and_mode():
    """The reason several plugins can be looked at together at all. Under the
    exclusive model a change of owner threw the previous plugin's table away --
    correct then, and now the specific thing that must not happen: rebuilding one
    is a whole pass over a column."""
    output = _run("cell_layer_registry_probe.mjs")
    assert "registering a second layer does not disturb the first one's colours" in output, output
    assert "each layer holds its own table" in output, output
    assert "and its own gate" in output, output
    assert "re-registering keeps the colours, mode and opacity" in output, output


def test_sidebar_order_is_composite_order_and_costs_a_redraw():
    """Dragging a card has to stay smooth on a slide with a million cells, which
    it only is because reordering changes blit order and nothing else."""
    output = _run("cell_layer_registry_probe.mjs")
    assert "a new layer goes on top of the stack" in output, output
    assert "and puts the layers in the order given, bottom first" in output, output
    assert "restacking is a redraw and never a re-render" in output, output
    assert "a layer the caller did not mention keeps a place in the stack" in output, output


def test_hiding_a_layer_frees_the_canvases_and_keeps_the_table():
    """The whole argument for not capping how many plugins may be loaded at once.
    Per label tile in view a visible layer costs ~4 MB of canvas; its lookup
    table costs four bytes a cell and does not grow as the user pans. So a
    loaded-but-hidden plugin is cheap, and there is no LRU to write."""
    output = _run("cell_layer_registry_probe.mjs")
    assert "hiding drops that layer's per-tile canvases" in output, output
    assert "and KEEPS its lookup table" in output, output
    assert "showing it again rebuilds only that layer" in output, output


def test_visible_and_active_are_different_questions():
    """Conflating them is what made the second plugin unusable: selecting a tool
    took the other one's colours off the screen, and there was no way to put them
    back."""
    output = _run("cell_layer_registry_probe.mjs")
    assert "selecting a different layer changes nothing about what is drawn" in output, output
    assert "hiding the active layer does not deselect it" in output, output


def test_a_viewer_with_no_plugin_still_draws_core_s_own_layer():
    """Its default opacity is 0.7. Applying that unconditionally would dim the
    outlines of every existing project the moment this shipped."""
    output = _run("cell_layer_registry_probe.mjs")
    assert "with nothing registered, the mask draws core's own layer" in output, output
    assert "and it composites at full strength" in output, output
    assert "and core's own layer takes the picture back" in output, output
    assert "opacity changes are a redraw, never a re-render" in output, output
