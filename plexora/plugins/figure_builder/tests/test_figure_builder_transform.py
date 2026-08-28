"""One fold for everything spatial, and the two ways an object is turned over.

Align, Distribute, Match size, Layout and Order folded into a popover called
"Arrange" some time ago, for a reason of arithmetic: five tiles whose words are
near-synonyms, on a bar floating over a sheet narrower than the bar. Beside that
fold sat a SIXTH tile called "Transform", holding the width, the height and the
angle as numbers.

That split is the thing this file is mostly about, because it was invisible in
the code and obvious the moment anybody used it. "Make these two the same size"
is Match size; "make this one 40mm wide" is Transform. That is a distinction
between a command and a field -- not between two subjects -- and there is no
rule a person could hold in their head for guessing which tile a given intention
is behind. They are one fold now, under the name that covers all of it, and Flip
joins them rather than becoming a third tile for the identical reason.

Four claims are worth pinning here, and each of them is a bug that ships green:

* ONE fold, not two. A second spatial tile is the whole defect coming back, and
  nothing about a stray `group:` in the registry looks wrong in review;

* a typed number is not committed per keystroke. Rotation to 20 degrees went in
  as a rotation to 2 -- and the commit re-rendered the canvas, which rewrote the
  bar, which replaced the field under the caret, so the second digit went
  nowhere and the box looked like it had closed itself. `line_width_pt` had
  carried a guard against exactly this since the shape panel hit it; the other
  three fields never got one;

* the four rotation zones exist and sit UNDER the resize handles. A zone drawn
  over the corner handle is an object that can no longer be resized from its
  corners, which is a far worse thing to have broken than rotation is to gain;

* a mirrored panel exports mirrored. The canvas flips an `<img>` with CSS and
  the exporter flips a raster with Pillow; if only one of them learns, the
  figure on screen and the figure in the PDF disagree, and the PDF is the one
  nobody looks at until it is submitted.

`tests/js/figure_annotation_probe.mjs` owns the flip and rotation ARITHMETIC --
what reflects, about what axis, and what a sweep of the pointer adds. This file
owns whether any of it is wired up.
"""

import re
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import export, operations, schema

STATIC = Path(__file__).resolve().parents[1] / "static"


def _source(name):
    return (STATIC / name).read_text(encoding="utf-8")


# -- one fold ---------------------------------------------------------------


def test_everything_spatial_is_in_one_fold():
    """Align, Distribute, Match size, Layout, Order, Flip and Transform all
    declare the same group, and that group is the only one that collapses.

    Asserted on the group NAMES rather than through the bar, because the bar
    renders what the registry clusters: an action left in `object` would draw
    its own tile beside the fold and look, to a reader of the bar's code, like
    it was meant to."""
    actions = _source("figureActions.js")

    for action_id in ("align", "distribute", "resize", "layout", "arrange",
                      "transform", "flip"):
        entry = re.search(rf'id: "{action_id}",.*?group: "(\w+)"', actions, re.S)
        assert entry, f"{action_id} is no longer a registered action"
        assert entry.group(1) == "transform", \
            f"{action_id} is spatial and sits outside the Transform fold"

    folds = re.findall(r"(\w+): \{ collapse:", actions)
    assert folds == ["transform"], \
        f"the bar folds {folds}, and two spatial folds is the bug this replaced"
    assert 'label: "Transform"' in actions and 'short: "Transform"' in actions


def test_the_transform_action_no_longer_opens_a_popover_of_its_own():
    """It is a SECTION of the fold now, the way Align is.

    An action still declaring `popover: true` would be a button the bar has to
    build a body for and cannot -- an empty box, which reads as a rendering bug
    rather than as a missing handler. The image-panel suite has a general test
    for that; this one names the action it caught."""
    actions = _source("figureActions.js")
    block = re.search(r'id: "transform",(?:[^{}]|\n)*?applies:', actions)
    assert block, "the Transform action has gone"
    assert "popover: true" not in block.group(0)


def test_the_panel_draws_every_section_it_replaced():
    """Nothing that was reachable before is missing from the merged panel.

    The five commands, the four z-order rows, the three numbers and the figure's
    unit were spread over two popovers. Losing one in the merge is silent: the
    fold still opens, and what is gone is a heading nobody is looking for."""
    bar = _source("figureContextBar.js")
    assert 'act === "group:transform"' in bar, "nothing opens the merged panel"
    assert "transformPanel()" in bar
    assert "arrangePopover" not in bar, "the popover it replaced is still here"
    assert "transformPopover" not in bar

    for title in ('"Align"', '"Distribute"', '"Match size"', '"Layout"',
                  '"Order"', '"Flip"', '"Transform"'):
        assert title in bar, f"the panel has no {title} section"

    # The commands themselves, by the vocabulary FigureCanvas answers to.
    for command in ("left", "center", "right", "top", "middle", "bottom",
                    "distribute_h", "distribute_v", "same_width", "same_height",
                    "same_size", "row", "column", "grid", "flip_h", "flip_v"):
        assert f'"{command}"' in bar, f"the panel offers no {command}"

    # The unit is the FIGURE's, in its own footer under the fields it governs.
    # Inside the Transform section it read as "the unit of this box".
    assert "unitsFooter()" in bar and 'data-field="units"' in bar
    assert "All dimensions use the selected units." in bar

    # Order stays labelled rows rather than becoming icons: "Bring forward" is
    # not a shape, and the four are near-identical chevrons drawn as glyphs.
    assert "FigureActions.ARRANGE.map" in bar


def test_the_sections_fold_and_are_remembered():
    """Seven sections is a tall panel, and which half of it a given person never
    touches is not something this file can know.

    Toggled in place rather than by rebuilding: a rebuild drops the caret out of
    whichever number field had it, and folding Align is not a reason to stop
    somebody typing a width."""
    bar = _source("figureContextBar.js")
    assert "static get FOLD_KEY()" in bar and "storedFolds()" in bar
    assert "rememberFolds()" in bar
    assert "toggleFold(" in bar and "data-fold=" in bar
    assert 'classList.toggle("is-folded"' in bar
    assert 'setAttribute("aria-expanded"' in bar, \
        "a fold nothing announces is a fold a screen reader cannot see"

    css = _source("figure_builder.css")
    assert ".fb-tf-section.is-folded .fb-tf-body" in css, \
        "folding a section hides nothing"


def test_opening_the_panel_arms_nothing():
    """The panel takes the focus itself, rather than a control inside it.

    The old rule put the caret in the first field, which was right while this
    popover WAS one short form. In a panel of seven sections the first field is
    the rotation, five sections down -- past everything a keyboard user wants to
    reach first, and a box that turns the object if they start typing."""
    bar = _source("figureContextBar.js")
    focus = re.search(r'if \(act === "group:transform"\) \{(.*?)\}', bar, re.S)
    assert focus, "the panel no longer decides where its focus goes"
    assert "this.popover.focus()" in focus.group(1)
    assert "tabIndex = -1" in focus.group(1), \
        "focusing an element with no tabindex does nothing at all"


# -- the bug that started this ----------------------------------------------


def test_a_typed_number_waits_until_the_typing_stops():
    """Rotation, width and height commit on `change` -- the field being left,
    Return, or a press of the spinner -- and never on `input`.

    Every prefix of a number is a valid number, so committing keystrokes rotated
    to 2 degrees on the way to 20 and left one undo entry per digit. Worse, the
    commit re-rendered the canvas, which rewrote the bar, which replaced the
    input under the caret -- so the box appeared to close itself half-way
    through the number.

    Named rather than counted: `line_width_pt` alone carried this guard, and the
    three that did not are precisely the three the Transform form added."""
    bar = _source("figureContextBar.js")
    fields = re.search(r"static get TYPED_FIELDS\(\) \{\s*return \[([^\]]*)\]",
                       bar, re.S)
    assert fields, "the guarded fields are no longer declared in one place"
    named = set(re.findall(r'"([\w]+)"', fields.group(1)))
    assert named == {"line_width_pt", "tf_rotation", "tf_w", "tf_h"}

    assert ("FigureContextBar.TYPED_FIELDS.includes(field)"
            ' && event.type !== "change"') in bar, \
        "the guard no longer stands between a keystroke and a commit"


def test_an_open_panel_survives_the_bar_being_redrawn():
    """Committing anything rewrites the bar's innerHTML, which detaches the
    button an open popover hangs off.

    `positionPopover` measures that button, and a detached element measures
    zero -- so the panel jumped to the corner of the page the first time
    anything was committed while it was open, which after the fix above is
    every time a width is typed."""
    bar = _source("figureContextBar.js")
    assert "reanchor()" in bar
    assert re.search(r"this\.el\.innerHTML = this\.markup\(ids\);\s*\n(?:\s*//[^\n]*\n)*"
                     r"\s*this\.reanchor\(\);", bar), \
        "the popover is not re-pointed at the freshly drawn bar"


# -- turning an object by hand ----------------------------------------------


def test_an_object_can_be_turned_from_any_of_its_corners():
    """Four zones just outside the corners, and the handle above the top edge
    that advertises rotation is kept.

    They are `.fb-handle` because that is the class `pointerDown` looks for, and
    they must sit UNDER the resize handles: a zone laps a few pixels over the
    handle sharing its corner, and in those pixels the visible control has to
    win or the object can no longer be resized from its corners at all."""
    canvas = _source("figureCanvas.js")
    zones = re.search(r'\["nw", "ne", "se", "sw"\]\.map\(\(corner\) =>(.*?)\.join\(""\)',
                      canvas, re.S)
    assert zones, "the corner rotation zones are gone"
    assert 'data-handle="rotate"' in zones.group(1)
    assert "fb-rotate-zone" in zones.group(1)
    assert 'fb-handle-rotate" data-handle="rotate"' in canvas, \
        "the visible rotate handle went with them, and nothing advertises rotation"

    css = _source("figure_builder.css")
    for corner in ("nw", "ne", "se", "sw"):
        assert f".fb-rotate-{corner} {{" in css, f"the {corner} zone is not placed"

    zone = re.search(r"\.fb-rotate-zone \{(.*?)\}", css, re.S)
    handle = re.search(r"^\.fb-handle \{(.*?)\}", css, re.S | re.M)
    assert zone and handle
    assert "cursor: url(" in zone.group(1), \
        "hovering a corner says nothing about what will happen there"
    depth = lambda block: int(re.search(r"z-index: (\d+)", block).group(1))
    assert depth(zone.group(1)) < depth(handle.group(1)), \
        "a rotation zone is drawn over the resize handle it shares a corner with"


def test_a_rotation_follows_the_hand_rather_than_a_bearing():
    """The angle added is how far the pointer has SWEPT since it went down.

    The old arithmetic used the pointer's bearing from the centre, plus ninety
    degrees -- and the ninety was the tell. It was there because the only place
    a rotation could start was the handle standing due north, so "pointer due
    north" had to mean "at rest". Start the same drag at a corner and the object
    snapped forty-five degrees before it had moved."""
    canvas = _source("figureCanvas.js")
    assert "static sweep(centre, from, to)" in canvas
    assert "FigureCanvas.sweep(" in canvas
    body = re.search(r"previewRotate\(snap\) \{(.*?)\n    \}", canvas, re.S)
    assert body, "previewRotate has gone"
    assert "180 / Math.PI + 90" not in body.group(1), \
        "the bearing-plus-ninety is back, and corner rotation jumps again"
    assert "item.start.rotation" in body.group(1), \
        "a sweep must add to the angle the object already had"


def test_the_bar_stays_up_while_something_is_being_turned():
    """Every other kind of drag hides it; a rotation does not.

    A bar that followed a moving object would be controls under the pointer, and
    one that stayed put would point at where the object used to be -- neither is
    worth having for the second a drag lasts. A rotation turns about a centre
    that does not move, so the bar is not in the way, and the angle counting up
    as the object turns is the entire reason anybody is looking at it."""
    canvas = _source("figureCanvas.js")
    assert "this.onGesture(true, kind);" in canvas
    assert "this.onGesture(false, gesture.kind);" in canvas
    assert "this.onRotatePreview(shown)" in canvas, \
        "nothing tells the panel what angle the drag is at"

    workspace = _source("figureWorkspace.js")
    assert 'suppress(active && kind !== "rotate")' in workspace
    assert "onRotatePreview: (degrees) => this.contextBar?.previewRotation(degrees)" \
        in workspace

    bar = _source("figureContextBar.js")
    assert "previewRotation(degrees)" in bar
    assert 'querySelector(\'[data-field="tf_rotation"]\')' in bar

    # And the press that STARTS the rotation must not dismiss the panel. It
    # lands on the canvas, so by every other test in the outside-click handler
    # it is a click somewhere else -- which closed the readout at the instant
    # it began reading, leaving the feature working and invisible.
    assert """event.target.closest?.('[data-handle="rotate"]')""" in bar, \
        "starting a rotation dismisses the panel that shows its angle"


# -- mirroring --------------------------------------------------------------


def test_flip_reaches_the_canvas_by_the_path_the_other_commands_use():
    """`data-arrange` is the vocabulary FigureCanvas already answers to, so the
    two buttons add words rather than a route.

    The branch has to come BEFORE the two-object guard: flip is the one command
    here that means something for a single object, because what it reverses is
    that object's own handedness rather than the order of several."""
    bar = _source("figureContextBar.js")
    assert 'data-arrange="flip_h"' not in bar, \
        "the flip buttons should be built by the shared command helper"
    assert '["flip_h", "Flip horizontal"' in bar
    assert '["flip_v", "Flip vertical"' in bar
    # Flipping is the one command anybody presses twice -- flip, look, flip
    # back -- so the panel stays open under the pointer.
    assert 'startsWith("flip_")' in bar

    canvas = _source("figureCanvas.js")
    body = re.search(r"    arrange\(command\) \{(.*?)const items = this\.arrangeItems",
                     canvas, re.S)
    assert body, "arrange no longer starts by gathering items"
    assert 'command === "flip_h"' in body.group(1), \
        "flip is behind the guard that needs two objects"


def test_the_flip_glyphs_are_drawn_rather_than_named():
    """Font Awesome's free set has no mirror icon, and a name it does not ship
    draws nothing at all and says nothing -- an empty square on a button.

    So these two are inline SVG: a shape and its mirror image either side of a
    dashed axis, which is the drawing every tool uses for this and the only one
    that says which way round the flip goes."""
    bar = _source("figureContextBar.js")
    for glyph in ("FLIP_GLYPH_H", "FLIP_GLYPH_V"):
        block = re.search(rf"static get {glyph}\(\) \{{(.*?)\n    \}}", bar, re.S)
        assert block, f"{glyph} has gone"
        assert "stroke-dasharray" in block.group(1), \
            f"{glyph} draws no axis, so it cannot say which way round it flips"
    assert 'glyph.charAt(0) === "<"' in bar, \
        "the command button no longer accepts raw markup, so the flips draw nothing"


def test_a_placement_remembers_which_way_round_its_picture_is():
    """Two flags on the placement, not a rewritten viewport.

    A flip is a fact about how the picture is PRESENTED on the page: the crop it
    was captured with has not changed, and a linked split-channel row has to
    stay in step whichever way round its members are shown."""
    place = schema.normalize_placement({"page_id": "pg_1"})
    assert place["flip_h"] is False and place["flip_v"] is False, \
        "a panel is not flipped until somebody flips it"

    place = schema.normalize_placement(
        {"page_id": "pg_1", "flip_h": True, "flip_v": False})
    assert place["flip_h"] is True and place["flip_v"] is False


def test_a_flip_survives_the_operation_that_carries_it():
    """`move_panels` is what a flip compiles to, so the flags have to go through
    its merge and come back out of `normalize_placement` intact."""
    document = {
        "panels": {"pn_1": {
            "panel_id": "pn_1",
            "placement": {"page_id": "pg_1", "x_mm": 10.0, "y_mm": 10.0,
                          "w_mm": 40.0, "h_mm": 30.0, "z": 0,
                          "flip_h": False, "flip_v": False},
        }},
        "pages": [{"page_id": "pg_1"}],
    }
    operations._move_panels(document, {
        "op": "move_panels",
        "moves": [{"panel_id": "pn_1",
                   "placement": {**document["panels"]["pn_1"]["placement"],
                                 "flip_h": True}}],
    })
    place = document["panels"]["pn_1"]["placement"]
    assert place["flip_h"] is True
    assert (place["x_mm"], place["w_mm"]) == (10.0, 40.0), \
        "flipping a panel moved or resized it"


def test_a_mirrored_panel_exports_mirrored():
    """Both writers take their pixels from `_render_one`, so the flip lives
    there and neither of them has to know about it.

    Put it in the PDF path instead and a figure exports mirrored to PDF and
    unmirrored to PNG, which is a disagreement nobody finds until the two are
    side by side."""
    from PIL import Image

    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 0, 255))

    same = export._mirrored(image, {"flip_h": False, "flip_v": False})
    assert same.getpixel((0, 0)) == (255, 0, 0)

    flipped = export._mirrored(image, {"flip_h": True, "flip_v": False})
    assert flipped.getpixel((0, 0)) == (0, 0, 255), "the raster was not mirrored"
    assert flipped.getpixel((1, 0)) == (255, 0, 0)

    tall = Image.new("RGB", (1, 2))
    tall.putpixel((0, 0), (255, 0, 0))
    tall.putpixel((0, 1), (0, 0, 255))
    assert export._mirrored(
        tall, {"flip_h": False, "flip_v": True}).getpixel((0, 0)) == (0, 0, 255)

    # A missing source renders as None further up; mirroring nothing is nothing
    # rather than an AttributeError inside an export that had otherwise worked.
    assert export._mirrored(None, {"flip_h": True}) is None


def test_only_the_picture_is_mirrored():
    """The scale bar, the colour bar, the labels and the panel letter are laid
    over the raster afterwards, from the same millimetres either way round.

    A scale bar drawn right-to-left is wrong, a reversed "50 µm" is unreadable,
    and a panel lettered "A" that flips to a mirrored A is a figure nobody can
    cite. On the canvas the same line is drawn by transforming only the
    `<img>`."""
    canvas = _source("figureCanvas.js")
    assert "static flipStyle(place)" in canvas
    style = re.search(r"static flipStyle\(place\) \{(.*?)\n    \}", canvas, re.S)
    assert "scaleX(-1)" in style.group(1) and "scaleY(-1)" in style.group(1)
    assert 'class="fb-panel-image"' in canvas
    image_tag = re.search(r'<img class="fb-panel-image".*?>', canvas, re.S)
    assert "FigureCanvas.flipStyle(place)" in image_tag.group(0), \
        "the flip is not on the picture"

    panel = re.search(r"return `<div class=\"fb-panel\$\{.*?\n        </div>`;",
                      canvas, re.S).group(0)
    for furniture in ("scaleBarMarkup", "colorBarMarkup", "panelLabelsMarkup"):
        after = panel.index(furniture)
        assert after > panel.index("fb-panel-image"), \
            f"{furniture} is inside the element the flip transforms"


@pytest.mark.parametrize("axis", ["flip_h", "flip_v"])
def test_flipping_twice_is_the_identity(axis):
    """Toggled rather than set, so a flip and a flip back leaves the figure
    byte-identical -- which is what makes the command safe to press to see what
    it does."""
    canvas = _source("figureCanvas.js")
    assert 'const flag = horizontal ? "flip_h" : "flip_v";' in canvas
    assert "[flag]: !box[flag]" in canvas, \
        f"{axis} is assigned rather than toggled"
