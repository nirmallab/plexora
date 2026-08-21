"""The plugin's client comes up when loaded the way the server loads it.

Nothing else here runs this plugin's JavaScript. The Python suite renders the
panel's HTML and stops; `node --check` sees syntax. So the entire client can be
broken -- a file missing from `PLUGIN.scripts`, a constructor that throws the
moment it runs, a registration that never happens -- while every server-side
test passes, and the only symptom is a panel that appears and does nothing.

The file list is read off the descriptor rather than restated here, so what gets
exercised is what the server will actually send.

The lookup-table checks are the other half of a seam. tests/js/cell_color_probe.mjs
pins how the renderer READS those shapes; this pins that the plugin emits them.
Each side passing on its own while the two disagree is a viewer that draws
nothing and reports nothing.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.cell_explorer import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "cell_explorer_boot_probe.mjs"


def _run(scripts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE), *scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


@pytest.fixture(scope="module")
def report():
    return _run(list(PLUGIN.scripts))


def test_the_declared_order_loads(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0
    assert data["loaded"] == list(PLUGIN.scripts)


def test_the_plugin_registers_itself_and_claims_the_cell_layer(report):
    """Core never names a plugin -- it activates whatever registered. And the
    claim is not optional here: without it nothing hands this plugin the layer,
    so the panel works and the image never changes."""
    _, data = report
    assert len(data["registered"]) == 1
    assert data["registered"][0]["name"] == "cell_explorer"
    assert data["registered"][0]["ownsCellLayer"] is True


def test_the_cell_layer_comes_up_filled(report):
    """This tool gives every cell a colour that means something, and an outline
    shows that colour as a one-pixel ring around tissue that is still the
    tissue's own colour -- at any zoom with more than a few hundred cells on
    screen, the map it exists to draw is not legible. Opacity is the control for
    seeing through it. Core falls back to outlines by itself for a mask stored
    already reduced to boundaries, so this is a preference, not an assumption."""
    _, data = report
    assert data["registered"][0]["preferredCellMode"] == "filled"


def test_the_selection_provider_is_deliberately_inert(report):
    """The provider interface is about GATING -- which cells to draw at all, and
    the shader's colour-coded range table. This plugin decides what colour a
    cell is, through a different channel entirely. Reporting colour support here
    would put an empty gate on the layer and hide every cell."""
    _, data = report
    assert data["provider"] == {"colorCoding": False, "ranges": {}}


def test_a_controller_can_be_built_from_a_plugin_context(report):
    """Loading without throwing is not the same as working: a class can define
    fine and still name something that does not exist when it is used."""
    _, data = report
    assert data["controller"] == {
        "column": None, "generation": 0, "opacity": 0.7, "cacheLimit": 8,
        "picker": True, "trigger": "button", "menuSearch": True,
    }


# --------------------------------------------------------------------------
# The core widgets this panel is built out of
# --------------------------------------------------------------------------

def test_the_panel_uses_cores_widgets_rather_than_its_own(report):
    """Colour-by is core's SearchableSelect and a category's colour is core's
    ColorSwatchPicker -- the same two controls the image channels use. Growing a
    private copy of either is how one panel ends up with a search box that
    behaves differently from every other search box in the viewer."""
    _, data = report
    assert data["controller"]["picker"] is True
    assert data["legend"]["core"] is True
    assert data["legend"]["built"] == 3, "one picker per legend row, Unassigned included"


def test_the_colour_by_control_carries_its_own_search(report):
    """Two things at once, and they point the same way. A button is sized to
    the column name rather than to a text field, which is what lets it share a
    line with the legend's filter and its All/None; and the search then lives
    inside the menu, where somebody looking for a search box will look. A field
    that doubles as the value display gives no sign at all that it can be typed
    into -- which is exactly how it read."""
    _, data = report
    assert data["controller"]["trigger"] == "button"
    assert data["controller"]["menuSearch"] is True


def test_the_three_toolbar_controls_share_one_line(report):
    """The template's half of the same claim. The filter and All/None sit in
    the colour-by control's row rather than in a header of their own, and they
    are wrapped together so they can come and go with the legend they act on --
    two controls left behind on a numeric column have nothing to act on."""
    panel = (REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "templates"
             / "cell_explorer" / "panel.html").read_text(encoding="utf-8")
    toolbar = panel.split('class="cex-toolbar"', 1)[1].split('id="cell_explorer_override"', 1)[0]
    for element in ("cell_explorer_variable", "cell_explorer_category_controls",
                    "cell_explorer_search", "cell_explorer_visibility"):
        assert element in toolbar, f"{element} is not in the toolbar row"
    assert "cex-legend-header" not in panel, "the legend's own header row is gone"


def test_a_run_of_colour_changes_costs_one_rebuild(report):
    """The native colour input fires `input` continuously while the pointer
    moves around the OS picker. Each one rebuilds a lookup table with an entry
    per cell id and re-renders every label tile on screen, so on a real slide
    the drag became a slideshow that kept painting after the pointer stopped.
    Coalescing to a frame loses nothing: a repaint cannot show more than one
    state per frame however many times it is asked to."""
    _, data = report
    assert data["recolor"] == {"duringDrag": 0, "afterFrame": 1, "frames": 1}


def test_rebuilding_the_legend_hands_back_the_pickers_it_replaces(report):
    """Each one parks a popover on <body> and subscribes to document. The
    category filter re-renders this list on every keystroke, so a render that
    does not release the previous batch leaks a set of both per row per
    character typed."""
    _, data = report
    assert data["legend"]["afterRerender"] == data["legend"]["built"]
    assert data["legend"]["afterDestroy"] == 0


def test_the_core_widgets_are_loaded_by_every_viewer_page():
    """The probe loads them because base.html does. If that ever stops being
    true, the probe would keep passing while the shipped panel threw
    ReferenceError on the first render."""
    base = (REPO_ROOT / "plexora" / "client" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "views/searchableSelect.js" in base
    assert "views/colorSwatchPicker.js" in base


def test_the_dense_lookup_table_is_what_the_renderer_reads(report):
    """A flat Uint8Array of 4*(maxId+1) RGBA bytes -- see
    ImageViewer.setCellColorLUT and tests/js/cell_color_probe.mjs."""
    _, data = report
    assert data["lut"]["isTypedArray"] is True
    assert data["lut"]["maxId"] == 3
    assert data["lut"]["length"] == 16


def test_a_chosen_colour_beats_the_default(report):
    _, data = report
    assert data["lut"]["overridden"] == [255, 0, 0, 255]


def test_hidden_and_missing_are_told_apart(report):
    """Both are expressed as alpha, and they are not the same thing: a hidden
    category is undrawn, while a cell with no value is drawn as Unassigned in a
    neutral colour so it never disappears without explanation."""
    _, data = report
    assert data["lut"]["hiddenAlpha"] == 0
    assert data["lut"]["missingAlpha"] == 255


def test_the_whole_legend_row_hides_a_category(report):
    """Hiding a category is the commonest thing done in this list, and the eye
    is a 17-pixel target at the far end of the row -- a list of them is a list
    of small targets to aim at one after another. The eye stays as the control
    that reports the state and as what the keyboard reaches; it just carries no
    handler of its own, because its click already reaches the row and two
    handlers for one gesture toggle twice."""
    _, data = report
    assert data["rowClick"]["onBody"] == 1
    assert data["rowClick"]["onEye"] == 2, "the eye must still toggle exactly once"


def test_the_colour_swatch_is_not_a_hide_button(report):
    """The one part of the row that means something else."""
    _, data = report
    assert data["rowClick"]["onSwatch"] == 2, "clicking the swatch changed visibility"
    assert data["rowClick"]["swatchClass"] == "cex-swatch"


def test_a_hidden_numeric_overlay_is_an_empty_table_not_a_missing_one(report):
    """A numeric column has no rows to put an eye on, so the eye beside its
    ramp takes the whole overlay off. It has to do that by handing core a table
    of the right shape with nothing in it: a null table means "this plugin is
    not colouring cells", which makes core draw the layer in its own default
    white -- brighter than what was there before, not gone."""
    _, data = report
    assert data["lut"]["blankIsTable"] is True
    assert data["lut"]["blankLength"] == 16
    assert data["lut"]["blankDrawsNothing"] is True


def test_nan_is_transparent_rather_than_the_bottom_of_the_ramp(report):
    """Mapping it to zero says the cell measured zero, which it did not."""
    _, data = report
    assert data["lut"]["rampNaNAlpha"] == 0
    assert data["lut"]["rampLowAlpha"] == 255


def test_high_cell_ids_produce_the_sparse_form(report):
    """Mask label values run far above the row count, and a dense table over
    them would be tens of megabytes of mostly nothing."""
    _, data = report
    assert data["lut"]["sparseIsMap"] is True
    assert data["lut"]["sparseHasEntry"] is True
    assert data["lut"]["sparseHasNoColors"] is True


def test_every_declared_asset_exists(report):
    static = REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "static"
    for name in PLUGIN.scripts:
        assert (static / name).is_file(), f"{name} is declared but not shipped"
    for name in PLUGIN.styles:
        assert (static / name).is_file(), f"{name} is declared but not shipped"


def test_the_order_of_the_declared_scripts_does_not_matter(report):
    """Stated as a test rather than assumed, because the descriptor orders them
    as if it did. Every cross-file reference is inside a method or a
    constructor, and toolLoader awaits all six before anything activates -- so a
    reordering is harmless, and knowing that is what makes the omission case
    below the failure worth guarding."""
    returncode, data = _run(list(reversed(PLUGIN.scripts)))
    assert returncode == 0, "\n".join(data["problems"])
    assert data["registered"][0]["name"] == "cell_explorer"


def test_the_probe_catches_a_file_dropped_from_the_descriptor(report):
    """The real failure, produced on purpose: a file the others depend on left
    out of PLUGIN.scripts, so the browser never fetches it. Everything
    server-side is unaffected -- the panel still renders."""
    scripts = [name for name in PLUGIN.scripts if name != "cellExplorerColors.js"]

    returncode, data = _run(scripts)
    assert returncode == 1
    assert any("CellExplorerColors" in problem for problem in data["problems"])
