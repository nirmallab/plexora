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
import re
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
    """The control shows the column it is colouring by, and the search that
    finds another one lives inside the menu it opens, where somebody looking
    for a search box will look. A field that doubles as the value display
    gives no sign at all that it can be typed into -- which is exactly how it
    read."""
    _, data = report
    assert data["controller"]["trigger"] == "button"
    assert data["controller"]["menuSearch"] is True


PANEL = (REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "templates"
         / "cell_explorer" / "panel.html")
CONTROLLER = (REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "static"
              / "cellExplorerSidebarController.js")


def test_everything_that_acts_on_the_list_is_one_line_under_colour_by():
    """The template's half. Colour-by has a line of its own; the list's count,
    its filter and Show all / Hide all are one line under it, wrapped together
    so they come and go with the legend -- controls left behind on a numeric
    column have nothing to act on. The filter field shares the counts' slot on
    that line, inert until its icon opens it."""
    panel = PANEL.read_text(encoding="utf-8")
    toolbar = panel.split('class="cex-toolbar"', 1)[1].split('id="cell_explorer_override"', 1)[0]
    controls = toolbar.split('id="cell_explorer_category_controls"', 1)
    assert "cell_explorer_variable" in controls[0], "colour-by heads the toolbar"
    for element in ("cell_explorer_summary", "cell_explorer_search_toggle",
                    "cell_explorer_visibility", "cell_explorer_search"):
        assert element in controls[1], f"{element} is not with the list's controls"
    slot = controls[1].split('class="cex-summary-slot"', 1)[1].split('class="cex-summary-actions"', 1)[0]
    for element in ("cell_explorer_summary", "cell_explorer_search_row"):
        assert element in slot, f"{element} is not in the slot the counts and filter share"
    search_row = re.search(r'<div id="cell_explorer_search_row"[^>]*>', panel).group(0)
    assert re.search(r"\sinert\b", search_row), "the folded filter must be out of the Tab order"
    assert "cex-legend-header" not in panel, "the legend's own header row is gone"


def test_overlay_roi_is_the_foot_of_the_card_with_no_standing_hint():
    """A way out to another tool, at the bottom, named for what it does. The
    sentence about hovering a region moved to its tooltip: on the panel it was
    a line every session paid for and few read."""
    panel = PANEL.read_text(encoding="utf-8")
    launch = panel.index('id="cell_explorer_roi_launch"')
    for block in ("cell_explorer_toolbar", "cell_explorer_legend_section",
                  "cell_explorer_continuous"):
        assert panel.index(f'id="{block}"') < launch, f"Overlay ROI is above {block}"
    assert ">Overlay ROI<" in panel
    assert "Hover a region to see what it is made of" not in panel


def test_the_mask_wait_knows_every_block_in_document_order():
    """The wait for a mask hides every block named in PANEL_BLOCKS, and render()
    puts each one back. A block missing from that list stays on screen behind
    the wait; one listed out of order is a list nobody can check by eye."""
    source = CONTROLLER.read_text(encoding="utf-8")
    listed = re.findall(r'"(cell_explorer_\w+)"',
                        source.split("static PANEL_BLOCKS = [", 1)[1].split("];", 1)[0])
    panel = PANEL.read_text(encoding="utf-8")
    positions = [panel.index(f'id="{block}"') for block in listed]
    assert positions == sorted(positions), listed
    top_level = re.findall(r'^    <\w+ id="(cell_explorer_\w+)"', panel, re.MULTILINE)
    waits = {"cell_explorer_empty", "cell_explorer_nowhere", "cell_explorer_mask_wait"}
    assert sorted(set(top_level) - waits) == sorted(listed)


def test_the_summary_line_counts_the_column_on_screen(report):
    """From the same descriptor the rows are built from, so the line and the
    list cannot disagree. It counts the whole column even while a filter
    narrows the list; the filter's field covers it then."""
    _, data = report
    assert data["list"]["summary"] == "4 categories · 201.7K cells"
    assert data["list"]["filtered"] == {"rows": 1, "summary": "4 categories"}


def test_counts_are_compact_with_the_exact_figure_on_hover(report):
    """At most six characters, so a column of them lines up by its last digit.
    999,960 is 1M, not 1000K; the exact figure and its share are a hover away."""
    _, data = report
    assert data["list"]["formats"] == [
        "13", "999", "1K", "5.4K", "190K", "201.7K", "999.9K", "1M", "1.2M"]
    assert data["list"]["tip"] == "5,412 cells (2.7%)"


def test_folding_the_filter_away_clears_it(report):
    """A filter left applied behind a closed field is a list with rows missing
    and nothing on screen to say why. Escape folds it and is consumed, so the
    same press does not also close something further out."""
    _, data = report
    assert data["list"]["opened"] is True
    assert data["list"]["closed"] == {
        "open": False, "inert": True, "value": "", "filter": "", "rows": 4, "consumed": True}


def test_a_list_of_thousands_is_built_a_page_at_a_time(report):
    """Every row carries a colour picker with a popover on <body> and two
    document listeners. An identifier-like column has a category per cell, and
    building them all on every keystroke of the filter froze the panel. The
    next page is built only as the list nears its end, and a row acted on
    keeps the list where it was."""
    _, data = report
    lst = data["list"]
    assert lst["firstPage"] == {"built": 200, "pickers": 200, "children": 200}
    assert lst["farFromEnd"] == 200
    assert lst["nearEnd"] == 400
    assert lst["sameList"] == {"built": 400, "scrollTop": 5164}
    assert lst["afterDestroy"] == 0


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



def test_z_and_x_step_the_overlay_column(report):
    """Thresholding's next marker, for the overlay: the dropdown's own list, one
    key at a time, as if the column had been picked there. With none chosen, X
    starts at the top and Z does nothing; at either end the step stops rather
    than wrapping. Numeric columns are on the list like any other."""
    _, data = report
    assert data["steps"]["trail"] == [
        False, None, True, "phenotype", True, "qc_status", True, "Area",
        False, "Area", True, "qc_status"]


def test_the_keys_answer_only_while_armed_and_bare(report):
    """Armed while the panel is on screen (onShow to onHide), never with a
    modifier held -- Cmd+X is cut. Upper or lower case both step."""
    _, data = report
    assert data["steps"]["keyed"] == [
        False, "qc_status", True, "phenotype", False, "phenotype", True, "qc_status",
        False, "qc_status"]


def test_the_footer_steps_are_buttons_in_the_card_foot():
    """The panel's last block, after Overlay ROI, with one button per key."""
    panel = PANEL.read_text(encoding="utf-8")
    footer = panel.split('<footer id="cell_explorer_footer"', 1)[1].split("</footer>", 1)[0]
    assert panel.index('id="cell_explorer_roi_launch"') < panel.index('id="cell_explorer_footer"')
    assert 'data-column-step="-1"' in footer and 'data-column-step="1"' in footer
    assert '<kbd class="plx-kbd">Z</kbd>' in footer and '<kbd class="plx-kbd">X</kbd>' in footer
