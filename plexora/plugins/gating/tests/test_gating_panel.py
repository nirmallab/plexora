"""The Thresholding panel, as one line per thing rather than one row each.

Four changes, and the first three are all the same change: this panel had grown
its own answers to questions core had already answered for the image sidebar,
so it read as a different kind of control for no reason its domain could
account for.

- THE MARKER PICKER IS ONE LINE. Its caption stood above it, spending a row of
  a 300px sidebar on six characters, while everything below it was a one-line
  control. It sits beside the picker now, on core's `.control-row`.
- THE THRESHOLD IS ONE LINE. Two typeable numbers had a row of their own above
  the track and "Auto Threshold" a full-width button below it -- three lines for
  one control in a 300px sidebar. It is now `value — slider — value — icon`,
  exactly what an image channel's contrast window is (viewerSidebar's
  createChannelSlot), down to the shared classes.
- THE CSV PAIR IS IN THE PANEL. They were two unlabelled arrows lifted into the
  card's header, whose only account of themselves was a tooltip. They are
  labelled actions now, on the line the image card puts Opacity and "Upload
  channel names" on.
- THE ACCENT IS THE VIEWER'S. The gate slider was orange, `--accent-gate`, which
  DESIGN.md retires by name: it predates gating becoming a plugin and read as a
  second chrome colour beside the cyan handles two panels up.

The fourth is new work rather than tidying. WHETHER THE THREE INPUTS AGREE is
shown under the distribution plot: a table, a mask and an image come out of
three steps of a pipeline and nothing in any of them says they belong together,
so a table paired with the wrong image produces a panel that works and answers
about a different sample. Core decides the findings (models/consistency.py);
this panel only shows them, quietly.

Source-level rather than rendered, for the reason the rest of this repo's
markup checks are: the panel is a template fragment that only reaches a page
with a project, a mask and a feature table behind it, and what is being pinned
here is which vocabulary it is written in.
"""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "templates" / "gating" / "panel.html"
CONTROLLER = PLUGIN_ROOT / "static" / "gatingSidebarController.js"
STYLES = PLUGIN_ROOT / "static" / "gating.css"
CORE = PLUGIN_ROOT.parent.parent / "client" / "src"

#: Jinja/HTML, block and line comments. Every "this is gone" assertion below
#: reads the file WITHOUT them: these files carry long explanations of what was
#: removed and why, so a naive substring search finds the prose describing the
#: absence and reports it as the thing still being there.
_COMMENTS = re.compile(r"\{#.*?#\}|<!--.*?-->|/\*.*?\*/|//[^\n]*", re.S)


def _code(path):
    return _COMMENTS.sub("", path.read_text(encoding="utf-8"))


def test_the_threshold_is_one_line_and_auto_ends_the_marker_line():
    """The same row the image channel's contrast window is: `.slider-auto-row`
    holds it. Auto Threshold is `.slider-auto-button` still, but it ends the
    MARKER's line now -- it answers "gate this marker", and one line fewer is
    one line fewer in a 300px sidebar."""
    panel = _code(PANEL)
    assert 'class="slider-auto-row"' in panel
    assert 'id="gate_slider" class="sidebar-slider"' in panel
    assert 'id="gate_auto_button" class="slider-auto-button gate-marker-auto"' in panel
    # On the marker's row, after the picker, and not on the slider's.
    marker_row = panel[panel.index('class="control-row"'):panel.index('id="gate_distribution_plot"')]
    assert marker_row.index('id="gate_marker_select"') < marker_row.index('id="gate_auto_button"')
    slider_row = panel[panel.index('class="slider-auto-row"'):panel.index('id="gate_provenance"')]
    assert "gate_auto_button" not in slider_row
    # Core's row stretches every child; the plugin's two-class rule stops it.
    assert ".slider-auto-button.gate-marker-auto {" in _code(STYLES)
    # The row the two numbers used to sit on, and the option that put them
    # there, are both gone -- either one alone leaves them off the track.
    assert "gate_threshold_fields" not in panel
    assert "fieldsSlot" not in _code(CONTROLLER)
    assert "gate-threshold-fields" not in _code(STYLES)


def test_the_numbers_show_two_decimals_and_keep_every_one():
    """Two decimals at rest, the whole gate on focus -- PlexoraSlider's
    `display`, which never touches the value held, saved or exported. The
    gate's own formatter is still what a focused box and aria use."""
    controller = _code(CONTROLLER)
    assert "display: (value) => this.formatGateShort(value)" in controller
    assert "format: (value) => this.formatGate(value)" in controller
    short = controller[controller.index("formatGateShort(value) {"):]
    short = short[:short.index("}")]
    assert "Math.min(2," in short
    # The boxes are sized for what they show at rest; a focused box widens
    # itself (slider.js), so a six-decimal gate needs no room reserved here.
    sizing = controller[controller.index("sizeGateFields(range, values = []) {"):]
    sizing = sizing[:sizing.index("}")]
    assert "formatGateShort" in sizing


def test_the_distribution_sits_under_the_slider_on_its_axis():
    """One value, one x: the plot is directly below the slider, its plotting
    area is measured off the slider's rail, and its domain is the slider's own
    bounds -- so a line and its handle stay vertically aligned at any width."""
    panel = _code(PANEL)
    assert panel.index('id="gate_slider"') < panel.index('id="gate_distribution_plot"')
    controller = _code(CONTROLLER)
    draw = controller[controller.index("drawGateDistribution() {"):]
    draw = draw[:draw.index("plotX(event) {")]
    assert "nodes?.rail?.getBoundingClientRect" in draw
    assert "rail.left - originX" in draw and "originX + width - rail.right" in draw
    # The slider's bounds, not the histogram's extent, unless those are empty.
    assert "const bounds = this.getGateRange(this.gateMarker);" in draw
    # The track moves with the sidebar and the number boxes; the plot follows.
    assert "new ResizeObserver(" in controller
    assert "observe(track)" in controller


def test_the_threshold_lines_drag_through_the_one_gate():
    """The lines are a second handle on the gate, not a second copy of it: a
    drag writes through setGateRange -- which moves the slider, the numbers and
    the cells -- per tick, and commits once on release, as a slider does."""
    controller = _code(CONTROLLER)
    move = controller[controller.index("onGripMove(event) {"):]
    move = move[:move.index("onGripUp(event) {")]
    assert "PlexoraSlider.snap(" in move, "a line lands on the slider's own step grid"
    assert "this.setGateRange(gate, CSVGatingList.events.GATING_BRUSH_MOVE)" in move
    up = controller[controller.index("onGripUp(event) {"):]
    up = up[:up.index("\n    }\n")]
    assert "CSVGatingList.events.SELECTION_CHANGED" in up
    assert "setPointerCapture" in controller
    assert ".gate-distribution-grip {" in _code(STYLES)


def test_the_viewer_contrast_is_the_image_channels_window():
    """The contrast slider over the viewer holds no window of its own: it
    reads the base sidebar's slot for the gated marker and writes through the
    same path that slot's slider takes, and redraws on the BRUSH_MOVE a change
    from the sidebar raises. Shown only while this panel is."""
    controller = _code(CONTROLLER)
    contrast = controller[controller.index("class GateContrastControl {"):]
    assert "this.sidebar.slotShowing?.(marker)" in contrast
    assert "this.sidebar.setSlotWindow(this.slotIndex, values, { commit })" in contrast
    assert "events.BRUSH_MOVE" in contrast
    assert 'scale: "log"' in contrast and "integer: true" in contrast
    # Under the "Toggle selected cells" caption, by class, not a core id.
    assert 'document.querySelector(".viewer-canvas-caption")' in contrast
    for hook in ("this.contrast?.show()", "this.contrast?.hide()", "this.contrast?.dispose()"):
        assert hook in controller
    sidebar = (CORE / "js" / "views" / "viewerSidebar.js").read_text(encoding="utf-8")
    for api in ("contrastBounds(name) {", "slotShowing(name) {", "setSlotWindow(slotIndex, values"):
        assert api in sidebar
    window = sidebar[sidebar.index("setSlotWindow(slotIndex, values"):]
    window = window[:window.index("\n    }\n")]
    assert "this.setSlotRange(slotIndex, values, true)" in window
    assert "this.updateSlotReadout(slot)" in window
    styles = _code(STYLES)
    rule = styles[styles.index("#gate_contrast_control {"):]
    rule = rule[:rule.index("}")]
    assert "pointer-events: auto;" in rule
    # Text on the image like the caption, not a chip of its own.
    assert "background" not in rule and "border" not in rule
    # The track recedes at rest and comes up under the pointer; the name does not.
    track = styles[styles.index(".gate-contrast-slider {"):]
    assert "opacity:" in track[:track.index("}")]
    assert "#gate_contrast_control:hover .gate-contrast-slider," in styles


def test_the_two_numbers_are_the_sliders_own_drawn_as_text():
    """Every slider's numbers are text until clicked, in main.css beside
    `.plx-number` itself -- not a copy of those declarations in this plugin's
    stylesheet, which is what would drift."""
    controller = _code(CONTROLLER)
    # It used to be an opt-in; now there is nothing to ask for, and Enter
    # giving the number back as text is the slider's own number field's.
    assert "is-plain-numbers" not in controller
    assert "blurFieldsOnEnter" not in controller
    assert ".plx-slider .plx-number," in (
        CORE / "css" / "main.css").read_text(encoding="utf-8")
    # No copy of the treatment here.
    assert ".plx-number" not in _code(STYLES)


def test_auto_is_an_icon_with_an_undo_on_it():
    """Auto replaces whatever gate is on screen, and on a marker set by eye --
    or copied off a paper -- that gate is the only copy of a number pressing
    Auto again will not return. Same bargain the contrast window strikes, same
    two glyphs."""
    controller = _code(CONTROLLER)
    assert "preAutoGates" in controller, "nothing is taken down before the fit"
    for icon in ("fa-wand-magic-sparkles", "fa-rotate-left"):
        assert icon in controller, f"{icon} is how one of the two states is drawn"
    for tooltip in ('"Auto threshold"', '"Restore previous threshold"'):
        assert tooltip in controller
    # A Map, not a field: the control is one slider shown for whichever marker
    # is selected, so a single pending revert would offer marker A's gate back
    # while marker B is on screen.
    assert "this.preAutoGates = new Map()" in controller
    # And the full-width grey bar is gone from both sides.
    assert ">Auto Threshold<" not in _code(PANEL)
    assert "#gate_auto_button" not in _code(STYLES)


def test_the_csv_pair_is_a_labelled_line_in_the_panel():
    """`data-tool-extras` is what lifts markup into the card's header
    (views/toolLoader.js). Dropping it is half the move; the other half is the
    words, which is what an icon in a header could never have room for."""
    panel = _code(PANEL)
    assert "data-tool-extras" not in panel
    assert 'class="layer-card-actions"' in panel
    for label in ("Load Gates", "Download Gates"):
        assert f">{label}<" in panel
    # The ids are unchanged: csvGatingList.js binds both by id, and this is a
    # move rather than a rewrite.
    for element_id in ("gating_upload_icon", "gating_download_icon"):
        assert f'id="{element_id}" class="layer-card-action"' in panel
    # The hidden file input the upload arrow triggers has to come with them.
    assert 'id="gating-upload-from-arrow"' in panel


def test_the_gate_slider_wears_the_viewers_accent():
    """DESIGN.md: don't use `--accent-gate`. It predates gating becoming a
    plugin, and a second chrome accent is exactly what the per-tool hue system
    replaced."""
    controller = _code(CONTROLLER)
    assert "--accent-gate" not in controller
    assert 'accent: "var(--accent-channel)"' in controller


def test_the_threshold_lines_are_the_same_colour_as_the_handles():
    """The lines on the distribution ARE the two handles twelve pixels above
    them -- the same two numbers, drawn against the data being thresholded. A
    raw `#ff3131` made one control two colours, and red says something failed
    where nothing has.

    Core's rule, because the distribution plot is core's (`.distribution-plot`
    in viewer.css); the plugin only fills it."""
    css = _code(CORE / "css" / "viewer.css")
    rule = css[css.index(".gate-threshold-line {"):]
    rule = rule[:rule.index("}")]
    assert "stroke: var(--accent-channel);" in rule
    assert "#ff3131" not in css


def test_the_marker_and_its_picker_share_a_line():
    """`.control-row` is core's label-beside-control shape, the one the Point
    size and Opacity rows use. Written out here rather than restyled locally,
    which is what a fourth private copy of six declarations would have been."""
    panel = _code(PANEL)
    row = panel[panel.index('class="control-row"'):]
    assert row.index(">Marker<") < row.index('id="gate_marker_select"')
    assert 'class="control-row"' in panel
    assert ".control-row {" in (CORE / "css" / "viewer.css").read_text(encoding="utf-8")
    # No copy of the shape in this plugin's stylesheet.
    assert "control-row" not in _code(STYLES)


def test_the_panel_says_where_the_three_inputs_disagree():
    """Under the distribution, because the distribution is what is being
    doubted: these are the marker's own numbers, and this is what may be wrong
    with the frame they sit in."""
    panel = _code(PANEL)
    assert 'id="gate_consistency"' in panel
    assert panel.index('id="gate_distribution_plot"') < panel.index('id="gate_consistency"')
    controller = _code(CONTROLLER)
    assert "paintConsistency()" in controller
    # Asked of core, once, and not recomputed here: the same question belongs
    # to every tool that draws per-cell results.
    assert "getConsistencyReport()" in controller


def test_the_mismatch_notes_are_muted_and_not_alarming():
    """DESIGN.md keeps amber for a non-blocking warning, and every state these
    notes report is one a real project is allowed to be in -- so the amber is
    spent on the glyph and a hairline edge, and the sentence stays Muted.

    Not the soft amber FILL the same page offers: two filled blocks in a 300px
    column are the loudest thing on the panel, which is the opposite of what a
    heuristic that can be wrong about a legitimate crop should look like."""
    styles = STYLES.read_text(encoding="utf-8")
    note = styles[styles.index(".gate-consistency-note {"):]
    note = note[:note.index("}")]
    assert "border-left: 2px solid var(--accent-warning);" in note
    assert "color: var(--text-muted);" in note
    assert "background" not in note
    # Red is for something that failed. Nothing here has.
    assert "--accent-danger" not in styles


def test_the_marker_note_is_only_for_a_project_whose_names_overlap():
    """A feature table's columns and an image's channel names are frequently
    different sets of strings by design. On such a project "no image channel is
    named CD3" is true of every marker, which is a description of the project
    and not a warning about it."""
    controller = _code(CONTROLLER)
    assert "markersShareTheImageVocabulary()" in controller
    # The guard runs BEFORE the per-marker question, not after.
    body = controller[controller.index("markerFinding() {"):]
    body = body[:body.index("markersShareTheImageVocabulary() {")]
    assert body.index("markersShareTheImageVocabulary()") < body.index("image.has(")
