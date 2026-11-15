"""What goes where on a page, in millimetres, independent of the output format.

One layout, several backends. The PDF writer emits these as vector objects and
the PNG/TIFF writer rasterises them, and because both read the same list they
cannot disagree about where a label sits -- which is exactly what happens when
each format grows its own copy of the placement arithmetic and one of them is
fixed.

Everything here is millimetres from the page's TOP-LEFT, which is how the
document stores geometry and how the canvas draws it. Turning that into
PDF's bottom-left origin is the PDF writer's business and nowhere else's.

Instructions are deliberately dumb -- a rectangle, a string, a swatch. Anything
a backend has to interpret is a decision made twice.
"""

from __future__ import annotations

from plexora.plugins.figure_builder.server import shapegeom, strokegeom, textmetrics

MM_PER_INCH = 25.4

#: Padding between a panel's edge and the furniture drawn inside it.
INSET_MM = 1.2

#: Height of a scale bar, in millimetres.
SCALEBAR_HEIGHT_MM = 0.8

#: Between a scale bar and its caption. Kept to the digit it has always been so
#: that re-exporting an existing figure does not move the caption.
SCALEBAR_LABEL_GAP_MM = 0.4

#: Between two captions sent to the SAME corner. They used to sit on top of each
#: other; one gesture can now add a caption per channel, and three names in one
#: corner have to be three lines. See `_panel_label_instructions`.
PANEL_LABEL_GAP_MM = 0.4

#: How long a colour bar is, as a fraction of the panel side it runs along.
#: A third: long enough to read a ramp along, short enough that three channels
#: stacked in a corner are still furniture rather than the subject.
COLORBAR_LENGTH_FRACTION = 1 / 3

#: Colour stops per ramp. `schema.COLORBAR_STOPS`, repeated rather than imported
#: because this module is deliberately free of the schema's validation
#: machinery; `test_the_colour_bar_stop_count_agrees_with_the_schema` pins them
#: together.
COLORBAR_STOPS = 48

#: The alpha the renderer draws every channel with -- `render.CHANNEL_ALPHA`,
#: which is `frag.glsl`'s. A colour bar's brightest end has to be the brightest
#: pixel the panel can actually contain, or the bar is a key to a picture that
#: was never drawn. Repeated rather than imported because `render` pulls in
#: numpy and the whole source-reading stack, and this module is on the PNG path.
CHANNEL_ALPHA = 0.9

#: Nominal width of one character as a fraction of the font size.
#:
#: Used ONLY to decide how much room a block of furniture claims when it is
#: anchored -- never to break a line or to place a glyph, both of which need a
#: real font engine and are done elsewhere (see `_text_layout`). Helvetica's
#: digits are 0.556 em and its lower case averages near it; tick labels are
#: digits and a unit, so this is close enough to keep a vertical colour bar
#: inside its panel.
NOMINAL_CHAR_EM = 0.55


def page_instructions(document, page, panels, annotations):
    """Everything drawn on one page, back to front.

    Order is the order it is drawn in, the canvas's stacking order
    (`stacking_order`): each panel with its furniture, and each annotation, at
    its place in the stack, so a figure exports stacked the way it looked.
    """
    style = document["settings"]["style"]
    label_style = document["settings"]["label_style"]
    instructions = []

    # Two orders, kept apart. The LETTER comes from reading order (rows, then
    # columns), which is how a reader finds panel C; the STACKING comes from
    # `z`, which is how the canvas draws them. This used to draw in reading
    # order too, so two overlapping panels exported stacked differently from
    # the way they looked on screen.
    #
    # Hidden objects keep their place in the reading order and are only left
    # undrawn, so hiding panel B does not reletter C as B in the exported file
    # while the canvas still says C.
    reading = reading_order(panels)
    letter_index = {panel["panel_id"]: index for index, panel in enumerate(reading)}
    for kind, item in stacking_order(document, reading, annotations):
        if item.get("hidden"):
            continue
        if kind == "annotation":
            instructions.extend(_annotation(item, style))
            continue
        panel = item
        place = panel["placement"]
        drawn = [{
            "kind": "panel", "panel_id": panel["panel_id"],
            "x": place["x_mm"], "y": place["y_mm"],
            "w": place["w_mm"], "h": place["h_mm"],
        }]
        drawn.extend(_panel_border(panel, place))
        drawn.extend(_panel_furniture(
            document, panel, letter_index[panel["panel_id"]], label_style, style))
        # A turned panel turns as one: picture, border and furniture about the
        # panel's centre, applied around the finished layout as for a turned
        # annotation (see _rotated).
        rotation = place.get("rotation") or 0.0
        if rotation:
            pivot = {"x": place["x_mm"] + place["w_mm"] / 2,
                     "y": place["y_mm"] + place["h_mm"] / 2}
            drawn = [{**item, "rotation": rotation, "pivot": pivot} for item in drawn]
        instructions.extend(drawn)

    return instructions


def stacking_order(document, panels, annotations):
    """One page's panels and annotations, back to front, as (kind, object).

    Mirrors `FigureSchema.stackOnPage`. A figure whose settings say
    `stack: "shared"` stacks both kinds by `z` together (a panel first on a
    tie); an older one ("split") draws every panel by its `z` and then every
    annotation by its own, which is how it always looked.
    """
    ordered = ([("panel", panel) for panel in sorted(panels, key=lambda p: p["placement"]["z"])]
               + [("annotation", a) for a in sorted(annotations, key=lambda a: a["z"])])
    if (document.get("settings") or {}).get("stack") != "shared":
        return ordered
    # sorted() is stable, so panels stay ahead of annotations on equal z.
    return sorted(ordered, key=lambda entry: (entry[1]["placement"]["z"] if entry[0] == "panel"
                                              else entry[1]["z"]))


#: How far apart a panel's shape and its frame's may be and still count as the
#: same shape. Rounding in millimetres and in pixels both produce ratios a hair
#: apart; treating those as a crop would move every picture by a fraction of a
#: pixel for nothing.
ASPECT_TOLERANCE = 0.002


def visible_frame(viewport, w_mm, h_mm):
    """(width, height), in image pixels, of the part of a panel's captured frame
    that a panel of w_mm x h_mm shows.

    ONE rule for a panel whose shape no longer matches the field it captured --
    it was resized from an edge, or put into a layout cell of another shape --
    and the canvas, both exporters and the scale bar all follow it: the panel
    shows the CENTRE of its frame, as much of it as fits at the frame's own
    proportions, and never stretches it. The canvas has always drawn it this way
    (`object-fit: cover`); the exporters used to stretch the whole frame into the
    box instead, which distorted the tissue in the file while the screen looked
    right, and measured the scale bar across the whole frame while only part of
    it was on the page.

    The bar is measured across THIS width (`scale_bar`), which is what keeps a
    physical scale bar true when a panel is cropped by its shape.
    """
    orientation = _turned(viewport) or {}
    frame_w = orientation.get("frame_w") or viewport.get("w") or 0
    frame_h = orientation.get("frame_h") or viewport.get("h") or 0
    if not w_mm or not h_mm or w_mm <= 0 or h_mm <= 0 or frame_w <= 0 or frame_h <= 0:
        return frame_w, frame_h
    aspect = w_mm / h_mm
    frame_aspect = frame_w / frame_h
    if abs(frame_aspect / aspect - 1) <= ASPECT_TOLERANCE:
        return frame_w, frame_h
    if frame_aspect > aspect:
        return frame_h * aspect, frame_h
    return frame_w, frame_w / aspect


def _turned(viewport):
    """The viewport's orientation when it turns or mirrors anything, else None
    -- `PlexoraViewerScene.orientationOf`'s rule, so both sides agree that a
    record of "0 degrees, no flips" is upright."""
    orientation = viewport.get("orientation")
    if not isinstance(orientation, dict):
        return None
    degrees = float(orientation.get("degrees") or 0) % 360
    if min(degrees, 360 - degrees) < 1e-9 and not orientation.get("flip_h") \
            and not orientation.get("flip_v"):
        return None
    return orientation


def visible_viewport(viewport, w_mm, h_mm):
    """The viewport cut down to `visible_frame`, about the same centre.

    An upright viewport is its own frame, so the box shrinks. A turned one
    keeps its box -- the renderer reads the box and cuts the frame out of the
    middle of the turned raster -- and only the frame's size changes.
    """
    frame_w, frame_h = visible_frame(viewport, w_mm, h_mm)
    orientation = _turned(viewport)
    if orientation:
        if frame_w == orientation.get("frame_w") and frame_h == orientation.get("frame_h"):
            return viewport
        return {**viewport, "orientation": {**orientation,
                                            "frame_w": frame_w, "frame_h": frame_h}}
    if frame_w == viewport["w"] and frame_h == viewport["h"]:
        return viewport
    # An orientation record that turns and mirrors nothing is upright, and is
    # dropped with the cut so the renderer takes the upright path for it.
    upright = {key: value for key, value in viewport.items() if key != "orientation"}
    return {**upright,
            "x": viewport["x"] + (viewport["w"] - frame_w) / 2,
            "y": viewport["y"] + (viewport["h"] - frame_h) / 2,
            "w": frame_w, "h": frame_h}


def _panel_border(panel, place):
    """A panel's border as four filled strips along the inside of its edge.

    Strips rather than a stroked rectangle, because the two backends disagree
    about where a stroke goes -- reportlab centres it on the path, Pillow
    draws it inward -- and a border has to be exactly as wide in the PDF as on
    the PNG, and inside the panel in both (`FigureCanvas.panelMarkup` draws it
    the same way, with `box-sizing: border-box`).
    """
    border = panel.get("border") or {}
    if not border.get("visible"):
        return []
    t = min(border.get("width_pt", 0.75) * textmetrics.MM_PER_PT,
            place["w_mm"] / 2, place["h_mm"] / 2)
    x, y, w, h = place["x_mm"], place["y_mm"], place["w_mm"], place["h_mm"]
    strip = lambda sx, sy, sw, sh: {"kind": "rect", "x": sx, "y": sy, "w": sw, "h": sh,  # noqa: E731
                                    "fill": border.get("color", "#ffffff"),
                                    "stroke": None, "line_width_pt": 0}
    return [strip(x, y, w, t), strip(x, y + h - t, w, t),
            strip(x, y + t, t, h - 2 * t), strip(x + w - t, y + t, t, h - 2 * t)]


def _reading_order(panel):
    place = panel["placement"]
    return (place["y_mm"], place["x_mm"], place["z"])


def reading_order(panels):
    """Panels in the order their letters run: rows top to bottom, each row left
    to right. Mirrors `FigureSchema.panelsOnPage`.

    A panel joins the current row when its top is above the middle of the
    row's first (topmost) panel. Exact tops are not enough: a layout that fits
    a wider picture into its cell centres it vertically, so its top sits a
    little lower than its neighbours', and sorting on the top alone lettered a
    row of three as A C B.
    """
    rows = []
    for panel in sorted(panels, key=_reading_order):
        place = panel["placement"]
        if rows:
            anchor = rows[-1][0]["placement"]
            if place["y_mm"] < anchor["y_mm"] + anchor["h_mm"] / 2:
                rows[-1].append(panel)
                continue
        rows.append([panel])
    return [panel for row in rows
            for panel in sorted(row, key=lambda p: (p["placement"]["x_mm"], p["placement"]["z"]))]


def _panel_furniture(document, panel, index, label_style, style):
    """Everything drawn ON a panel: its letter, its bars, its captions.

    Two things that used to be here are gone. The channel LEGEND -- a column of
    swatches and names -- and the panel TITLE, a centred line under the box.
    Both said what a caption says, and a panel with three overlapping ways to
    write a word on a picture was three places to look for the word. `labels`
    is the one that survived, because it is the one that carries its own corner,
    size and colour.
    """
    place = panel["placement"]
    family = style["font_family"]
    out = []

    label = panel["label"]["text"] if not panel["label"]["auto"] else label_for(index, label_style)
    if panel["label"]["visible"] and label:
        out.extend(_panel_letter(panel["label"], label, place, style, family))

    out.extend(_scalebar_instructions(document, panel, place, style, family))
    out.extend(_colorbar_instructions(panel, place, style, family))
    out.extend(_panel_label_instructions(panel, place, style, family))
    return out


#: The padding round a panel letter's ground, when it has one.
LETTER_GROUND_PAD_MM = 0.5


def _panel_letter(own, text, place, style, family):
    """The panel's letter, at its anchor, and the ground behind it if it has one.

    Anchored like a caption (`anchor_box`, `INSET_MM` from the edges) and sized
    from its own size or the figure's. A letter with no setting of its own is
    exactly what every letter was before these existed: the figure's size,
    white, bold, top left. `FigureCanvas.panelLetterMarkup` is the mirror.
    """
    size_pt = own.get("size_pt") or style["label_size_pt"]
    height = size_pt * textmetrics.MM_PER_PT
    width = _nominal_width_mm(text, size_pt)
    position = own.get("position") or "top_left"
    pad = LETTER_GROUND_PAD_MM if own.get("background") else 0.0
    x, y = anchor_box(place, position, width + 2 * pad, height + 2 * pad, INSET_MM)
    column = anchor_parts(position)[1]
    out = []
    if own.get("background"):
        out.append({"kind": "rect", "x": x, "y": y, "w": width + 2 * pad, "h": height + 2 * pad,
                    "fill": own["background"], "stroke": None, "line_width_pt": 0})
    out.append(_furniture(text, x + pad, y + pad, width, column, size_pt,
                          own.get("color") or "#ffffff", family, bold=own.get("bold", True)))
    return out


def anchor_box(place, anchor, w_mm, h_mm, margin_mm):
    """The top-left of a box of this size, placed at one of the nine anchors.

    Pure arithmetic on the panel's own rectangle, and the only place either
    renderer decides what "bottom left" means -- `FigureCanvas.anchorBox` is its
    mirror and `test_the_canvas_and_the_exporter_anchor_furniture_alike` holds
    the two together.

    The margin applies only on the sides the box is pushed against: a centred
    box is centred on the panel, not on the panel minus its margins, which is
    what makes a colour bar centred under one panel line up with the one under
    the next when the two are different widths.
    """
    row, column = anchor_parts(anchor)
    if column == "left":
        x = place["x_mm"] + margin_mm
    elif column == "right":
        x = place["x_mm"] + place["w_mm"] - margin_mm - w_mm
    else:
        x = place["x_mm"] + (place["w_mm"] - w_mm) / 2
    if row == "top":
        y = place["y_mm"] + margin_mm
    elif row == "bottom":
        y = place["y_mm"] + place["h_mm"] - margin_mm - h_mm
    else:
        y = place["y_mm"] + (place["h_mm"] - h_mm) / 2
    return x, y


def anchor_parts(anchor):
    """An anchor name as (row, column). "center" is the one without a seam."""
    if anchor == "center":
        return "middle", "center"
    row, _, column = str(anchor or "").partition("_")
    row = row if row in ("top", "middle", "bottom") else "bottom"
    column = column if column in ("left", "center", "right") else "right"
    return row, column


def _nominal_width_mm(text, size_pt):
    """Roughly how wide a string will be. See NOMINAL_CHAR_EM."""
    return len(str(text)) * NOMINAL_CHAR_EM * size_pt * textmetrics.MM_PER_PT


def _size_pt(own, style_size):
    """A furniture size in points: the piece's own, or the figure's.

    None is stored for "the figure's" rather than a copy of the number, so that
    changing the figure's body size still moves everything that never asked for
    a size of its own.
    """
    return style_size if own is None else own


def _scalebar_instructions(document, panel, place, style, family):
    """The bar, and its caption above it, anchored as one block.

    The caption sits above the bar whatever corner the block is in -- including
    the top ones, where it is therefore inside the panel and above the rule
    rather than hanging off the edge. One rule, so a bar moved from one corner
    to another keeps the same shape.
    """
    bar = scale_bar(document, panel)
    if not bar:
        return []
    settings = panel["scalebar"]
    size_pt = _size_pt(settings.get("label_size_pt"), style["font_size_pt"])
    thickness = settings["thickness_mm"]
    labelled = bool(settings.get("label", True)) and bool(bar["label"])
    caption_mm = _pt_to_mm(size_pt) + SCALEBAR_LABEL_GAP_MM if labelled else 0.0

    width = bar["fraction"] * place["w_mm"]
    _, column = anchor_parts(settings["position"])
    x, y = anchor_box(place, settings["position"], width,
                      caption_mm + thickness, settings["margin_mm"])

    out = []
    if labelled:
        out.append(_furniture(bar["label"], x, y, width, column,
                              size_pt, settings["color"], family))
    out.append({"kind": "rect", "x": x, "y": y + caption_mm,
                "w": width, "h": thickness,
                "fill": settings["color"], "stroke": None, "line_width_pt": 0})
    return out


def _colorbar_instructions(panel, place, style, family):
    """One intensity ramp per visible channel, with ticks in raw units.

    Each channel gets its own bar because each has its own window: a single
    shared axis would have to pick one channel's numbers and print them under
    all of them, which is a colour bar that is wrong for every channel but one.
    """
    settings = panel["colorbar"]
    rows = colorbar_rows(panel)
    if not settings["visible"] or not rows:
        return []

    size_pt = _size_pt(settings.get("label_size_pt"), style["font_size_pt"])
    ticked = settings["ticks"] > 0
    tick_mm = settings["tick_length_mm"] if ticked else 0.0
    label_mm = _pt_to_mm(size_pt) if ticked else 0.0
    thickness = settings["thickness_mm"]
    vertical = settings["orientation"] == "vertical"
    gap = settings["gap_mm"]

    if vertical:
        length = place["h_mm"] * COLORBAR_LENGTH_FRACTION
        widest = max(_nominal_width_mm(row["labels"][-1], size_pt) for row in rows)
        lane = thickness + (tick_mm + widest if ticked else 0.0)
        block_w = lane * len(rows) + gap * (len(rows) - 1)
        block_h = length
    else:
        length = place["w_mm"] * COLORBAR_LENGTH_FRACTION
        lane = thickness + tick_mm + label_mm
        block_w = length
        block_h = lane * len(rows) + gap * (len(rows) - 1)

    # The end labels are centred on the end ticks, so they overhang the bar
    # (horizontal: half the first and last label's width; vertical: half a
    # line above and below). The block is anchored WITH that overhang, so a
    # bar in a corner keeps its labels inside the panel.
    over_a = over_b = 0.0
    if ticked and vertical:
        over_a = over_b = _pt_to_mm(size_pt) / 2
    elif ticked:
        # 0.6 rather than half: the width is an estimate, and the extra
        # tenth keeps a slightly wide label off the panel's edge.
        over_a = max(_nominal_width_mm(row["labels"][0], size_pt) for row in rows) * 0.6
        over_b = max(_nominal_width_mm(row["labels"][-1], size_pt) for row in rows) * 0.6
    if vertical:
        x, y = anchor_box(place, settings["position"], block_w,
                          block_h + over_a + over_b, settings["margin_mm"])
        y += over_a
    else:
        x, y = anchor_box(place, settings["position"], block_w + over_a + over_b,
                          block_h, settings["margin_mm"])
        x += over_a

    out = []
    for index, row in enumerate(rows):
        if vertical:
            out.extend(_colorbar_lane_vertical(
                row, x + index * (lane + gap), y, length, settings,
                size_pt, tick_mm, family))
        else:
            out.extend(_colorbar_lane_horizontal(
                row, x, y + index * (lane + gap), length, settings,
                size_pt, tick_mm, family))
    return out


def _colorbar_lane_horizontal(row, x, y, length, settings, size_pt, tick_mm,
                              family):
    """One channel's bar running left to right, ticks and labels beneath it."""
    thickness = settings["thickness_mm"]
    out = [{"kind": "swatch", "x": x, "y": y, "w": length, "h": thickness,
            "color": row["ramp"][-1], "ramp": row["ramp"], "vertical": False}]
    if not settings["ticks"]:
        return out
    for label, position in zip(row["labels"], _tick_positions(settings["ticks"])):
        at = x + position * length
        if settings["tick_width_pt"] > 0 and tick_mm > 0:
            out.append({"kind": "line", "x": at, "y": y + thickness,
                        "w": 0.0, "h": tick_mm,
                        "stroke": settings["tick_color"],
                        "line_width_pt": settings["tick_width_pt"]})
        # Centred on the tick and given a box wide enough to hold it: the
        # outermost labels therefore overhang the ends of the bar, which is
        # where a reader looks for the value AT the end.
        width = max(_nominal_width_mm(label, size_pt), 1.0) * 2
        out.append(_furniture(label, at - width / 2, y + thickness + tick_mm,
                              width, "center", size_pt,
                              settings["tick_color"], family))
    return out


def _colorbar_lane_vertical(row, x, y, length, settings, size_pt, tick_mm, family):
    """One channel's bar running top to bottom, ticks and labels to its right.

    Low intensity at the BOTTOM, which is the way a reader expects a vertical
    scale to run and the opposite of how the ramp is stored -- so the stops are
    reversed here rather than in `colorbar_rows`, which stays the one order the
    horizontal bar and the canvas both read.
    """
    thickness = settings["thickness_mm"]
    out = [{"kind": "swatch", "x": x, "y": y, "w": thickness, "h": length,
            "color": row["ramp"][-1],
            "ramp": list(reversed(row["ramp"])), "vertical": True}]
    if not settings["ticks"]:
        return out
    ascent = _pt_to_mm(size_pt) * FURNITURE_ASCENT_EM
    for label, position in zip(row["labels"], _tick_positions(settings["ticks"])):
        at = y + (1 - position) * length
        if settings["tick_width_pt"] > 0 and tick_mm > 0:
            out.append({"kind": "line", "x": x + thickness, "y": at,
                        "w": tick_mm, "h": 0.0,
                        "stroke": settings["tick_color"],
                        "line_width_pt": settings["tick_width_pt"]})
        width = max(_nominal_width_mm(label, size_pt), 1.0) * 1.5
        # Nudged up by half an em so the label's middle, not its baseline,
        # lines up with the tick it belongs to.
        out.append(_furniture(label, x + thickness + tick_mm,
                              at - ascent / 2, width, "left", size_pt,
                              settings["tick_color"], family))
    return out


def _tick_positions(count):
    """Where the ticks fall along a bar, as fractions from its low end.

    One tick sits at the low end rather than in the middle: a single tick on a
    colour bar labels where the window STARTS, which is the number that is not
    guessable from the picture.
    """
    if count <= 1:
        return [0.0]
    return [index / (count - 1) for index in range(count)]


def colorbar_rows(panel):
    """One ramp and its tick labels per visible channel, low end first.

    The labels are the channel's own display window in RAW units -- the numbers
    the contrast was set against, and the numbers another lab would need to
    reproduce the picture. Anything else would be a quantity the figure does
    not encode.
    """
    rows = []
    for channel in panel["scene"]["channels"]:
        if not channel.get("visible", True):
            continue
        low, high = channel["window"][0], channel["window"][1]
        colour = channel["color"]
        rows.append({
            "label": channel["fullname_at_capture"] or channel["key"],
            "ramp": _ramp(colour, COLORBAR_STOPS),
            "labels": [format_intensity(low + (high - low) * position)
                       for position in _tick_positions(panel["colorbar"]["ticks"])],
        })
    return rows


def _ramp(colour, stops):
    """Black to the channel's colour, the way the renderer draws it.

    `t * colour * CHANNEL_ALPHA`, which is `render.render_panel`'s arithmetic
    and `frag.glsl`'s before it -- so the top of the bar is the brightest pixel
    this channel can actually produce, rather than a colour the panel never
    contains.
    """
    out = []
    for index in range(max(2, stops)):
        t = index / (max(2, stops) - 1) * CHANNEL_ALPHA
        out.append("#{:02x}{:02x}{:02x}".format(
            min(255, int(round(colour["r"] * t))),
            min(255, int(round(colour["g"] * t))),
            min(255, int(round(colour["b"] * t)))))
    return out


def _panel_label_instructions(panel, place, style, family):
    """The free captions a user has put on the image.

    Two sent to the same corner STACK, one line apart, in storage order. They
    used to sit exactly on top of each other on the theory that a visible
    collision is better than a silent offset -- which held while captions were
    typed one at a time, and stopped holding the moment one gesture could add a
    caption per channel. Three channel names in one corner have to be three
    lines or the feature produces an unreadable smudge.

    Bottom anchors stack UPWARD so the first caption stays exactly where a lone
    one would sit and the block grows into the panel rather than off its edge.
    `FigureCanvas.panelLabelsMarkup` is the mirror.
    """
    out = []
    used = {}
    for entry in panel.get("labels") or []:
        if not entry["text"]:
            continue
        size_pt = _size_pt(entry.get("size_pt"), style["label_size_pt"])
        row, column = anchor_parts(entry["position"])
        height = _pt_to_mm(size_pt)
        width = place["w_mm"] - 2 * INSET_MM
        x, y = anchor_box(place, entry["position"], width, height, INSET_MM)
        taken = used.get(entry["position"], 0.0)
        y += -taken if row == "bottom" else taken
        used[entry["position"]] = taken + height + PANEL_LABEL_GAP_MM
        out.append(_furniture(entry["text"], x, y, width, column, size_pt,
                              entry["color"], family,
                              bold=entry.get("bold", False),
                              italic=entry.get("italic", False)))
    return out


#: Where one line of furniture sits below the y it is anchored to, in ems.
#:
#: Both backends used to compute this for themselves, from this same 0.8, and it
#: is kept to the digit rather than replaced with the font's real ascent so that
#: re-exporting a figure made before this change does not move its panel labels.
#: A text annotation is laid out properly instead -- see `_text_layout` -- but
#: that is new behaviour rather than a silent shift under an existing figure.
FURNITURE_ASCENT_EM = 0.8


def _furniture(text, x, y_top, w, align, size_pt, colour, family, bold=False,
               italic=False):
    """One line of panel furniture, as a one-run text instruction.

    Furniture and annotation text share the instruction so that each backend has
    exactly one text branch. `y` is the BASELINE here as everywhere else, so the
    cap-height offset each backend used to apply is applied once, here.
    """
    return {
        "kind": "text", "x": x, "w": w, "align": align,
        "y": y_top + size_pt * textmetrics.MM_PER_PT * FURNITURE_ASCENT_EM,
        "runs": [_run(text, family, size_pt, colour, bold=bold, italic=italic)],
    }


def _run(text, family, size_pt, colour, bold=False, italic=False,
         underline=False, strike=False):
    """A run with every key present. Backends never see an absent one."""
    return {"text": text, "family": textmetrics.family(family), "size_pt": size_pt,
            "bold": bold, "italic": italic, "underline": underline,
            "strike": strike, "color": colour}


def _resolved_run(run, style):
    """One of an annotation's runs, with the box's style filled in.

    A run stores only what it OVERRIDES, so that raising the box's font size
    still reaches every run the user never touched. Nothing downstream is
    allowed to see that half-specified form: this is the only way a run leaves
    this module.
    """
    return _run(
        run["text"],
        run.get("family") or style["font_family"],
        style["font_size_pt"] if run.get("size_pt") is None else run["size_pt"],
        run.get("color") or style["color"],
        bold=bool(run.get("bold")), italic=bool(run.get("italic")),
        underline=bool(run.get("underline")), strike=bool(run.get("strike")))


def _text_layout(annotation):
    """Every line of one text annotation, positioned. Pure arithmetic.

    Where the lines BREAK is not decided here -- it cannot be, because measuring
    a string needs a font engine and `reportlab` is optional while this module
    is on the PNG path too. The browser breaks them and the break is stored, so
    all this does is stack lines that already exist.

    Mirrored by `FigureCanvas.textLayout`, and
    `test_the_canvas_and_the_exporter_put_the_baseline_in_the_same_place`
    compares the two. Returns block height and, per line, the baseline in
    millimetres from the page top.
    """
    style = annotation["style"]
    geometry = annotation["geometry"]
    lines = (annotation.get("rich") or {}).get("lines") or []

    measured = []
    for line in lines:
        runs = [_resolved_run(run, style) for run in line["runs"]]
        # An empty line still occupies one, at the box's own size: it is the
        # blank line between two paragraphs, and collapsing it would close a gap
        # the author put there on purpose.
        lead, ascent, descent = textmetrics.line_metrics(
            runs or [_run("", style["font_family"], style["font_size_pt"], style["color"])],
            style.get("line_height", textmetrics.LINE_HEIGHT))
        measured.append({"runs": runs, "lead": lead, "ascent": ascent,
                         "descent": descent, "hard": bool(line.get("hard", True))})

    block = sum(line["lead"] for line in measured)
    valign = style.get("valign", "top")
    if valign == "middle":
        top = geometry["y_mm"] + (geometry["h_mm"] - block) / 2
    elif valign == "bottom":
        top = geometry["y_mm"] + geometry["h_mm"] - block
    else:
        top = geometry["y_mm"]

    out = []
    cursor = top
    for index, line in enumerate(measured):
        # The line sits centred in its own box, half the leading above and half
        # below, so a line mixing an 8 pt caption with a 6 pt superscript lands
        # where a reader expects instead of riding the bottom of the box.
        half_lead = (line["lead"] - (line["ascent"] + line["descent"])) / 2
        following = measured[index + 1] if index + 1 < len(measured) else None
        out.append({
            "baseline_mm": cursor + half_lead + line["ascent"],
            "lead_mm": line["lead"],
            "runs": line["runs"],
            # The last line of a paragraph is never justified -- stretching it
            # to the box is what makes a two-word final line read as a mistake.
            "last_of_paragraph": following is None or following["hard"],
        })
        cursor += line["lead"]
    return {"block_h_mm": block, "lines": out}


def _annotation(annotation, style):
    geometry = annotation["geometry"]
    own = annotation["style"]
    common = {"x": geometry["x_mm"], "y": geometry["y_mm"],
              "w": geometry["w_mm"], "h": geometry["h_mm"]}
    if annotation["type"] == "text":
        align = own["align"]
        out = []
        for line in _text_layout(annotation)["lines"]:
            if not line["runs"]:
                continue          # a blank line takes height and draws nothing
            out.append({
                "kind": "text", "x": geometry["x_mm"], "y": line["baseline_mm"],
                "w": geometry["w_mm"], "runs": line["runs"],
                "align": "left" if (align == "justify" and line["last_of_paragraph"])
                         else align,
            })
        return _rotated(out, annotation)
    if annotation["type"] in ("line", "arrow"):
        return _stroke(annotation, geometry, own, common)
    if annotation["type"] == "shape":
        shape = annotation["shape"]
        return _rotated([{
            "kind": "path", **common,
            # Absolute millimetres, like every other instruction in this file.
            # x/y/w/h still ride along, and only for `_rotated` -- the pivot is
            # computed from the box, which is why the box has to be the ink's
            # tight bounds and not merely a box the ink fits inside.
            "segments": shapegeom.segments_mm(shape, geometry),
            "closed": shape["closed"],
            # A fill on an open path is a guess about where the missing edge
            # runs, and every backend guesses differently. The stored colour is
            # kept -- closing the path brings it back -- and the decision not to
            # draw it is made once, here, rather than three times.
            "fill": (own["fill"] or None) if shape["closed"] else None,
            # There is no `stroke: none` key. A width of nothing already says
            # it, and a second way to say the same thing is a second thing to
            # keep in step.
            "stroke": own["color"] if own["line_width_pt"] > 0 else None,
            "line_width_pt": own["line_width_pt"],
            "opacity": own["opacity"],
        }], annotation)
    return _rotated([{"kind": annotation["type"], **common,
                      "fill": own["fill"] or None, "stroke": own["color"],
                      "line_width_pt": own["line_width_pt"]}], annotation)


def _stroke(annotation, geometry, own, common):
    """A line or an arrow, taken apart into things every backend already draws.

    A stroke used to be one instruction, because a stroke used to be one
    straight solid line. It now has a dash, a head at either end out of five
    styles, and an edge that may be a taper or a fade -- and rather than teach
    the PDF writer and the raster writer each of those, it is decomposed here
    into at most three kinds of thing:

      * the SHAFT, which is either an enriched `line` (carrying `dash_pt`,
        `fade` and `opacity` beside the colour and width it always had) or, when
        the edge tapers, a closed `path` -- because a taper has no constant
        width and no backend in this tree has a variable-width pen;

      * each HEAD, as a `path`: stroked and open for the barbs and the bar,
        closed and filled for the triangle and the diamond.

    Which means the two solid head styles and the whole taper need no new export
    code at all, and -- not incidentally -- the raster writer draws arrowheads
    for the first time. It never had any: PNG and TIFF exported every arrow in
    every figure as a plain line, and nothing said so.

    Endpoints are TRIMMED before the shaft is emitted, so a fat round cap does
    not poke out either side of a solid head. The heads themselves are placed
    against the untrimmed endpoints, which is where the user put them.

    Not `_rotated`, and deliberately: `geometry.rotation` has never done
    anything to a line in any renderer, and a vector that already carries its
    own direction in the SIGNS of `w_mm`/`h_mm` has no obvious pivot to turn
    about. Pinned by a test rather than left to be discovered.
    """
    p1 = (geometry["x_mm"], geometry["y_mm"])
    p2 = (p1[0] + geometry["w_mm"], p1[1] + geometry["h_mm"])
    width_mm = own["line_width_pt"] * textmetrics.MM_PER_PT
    size_mm = strokegeom.head_size(
        own["head_size_pt"], own["line_width_pt"]) * textmetrics.MM_PER_PT
    heads = [strokegeom.head_geometry(own["start_head"], size_mm, width_mm),
             strokegeom.head_geometry(own["end_head"], size_mm, width_mm)]
    start, end = strokegeom.trimmed_shaft(p1, p2, heads[0]["trim"], heads[1]["trim"])

    out = []
    if own["edge"] in strokegeom.TAPER_EDGES:
        # `line_style` is stored and not drawn here: dashing a ribbon whose
        # width varies along it is a fourth renderer path for a look nobody
        # asked for, and Edge is presented as one control.
        outline = strokegeom.taper_outline(p1, p2, width_mm, own["edge"],
                                           heads[0]["trim"], heads[1]["trim"])
        out.append(_filled_path(outline, own["color"], own["opacity"], common))
    elif start != end:
        out.append({
            "kind": "line", **common,
            # The trimmed span, not the stored one. x/y/w/h is what both writers
            # draw a line between, so the trim has to be in the numbers.
            "x": start[0], "y": start[1],
            "w": end[0] - start[0], "h": end[1] - start[1],
            "stroke": own["color"],
            "line_width_pt": own["line_width_pt"],
            # In points, like `line_width_pt` and for the same reason: it scales
            # with the pen, and the pen is in points. Derived from the enum here
            # so a document can never hand reportlab a pattern it refuses.
            "dash_pt": strokegeom.dash_pattern(own["line_style"], own["line_width_pt"]),
            "fade": own["edge"] if own["edge"] in strokegeom.FADE_EDGES else None,
            "opacity": own["opacity"],
        })

    # Heads at the annotation's own opacity and never at a fade's alpha: a head
    # on the faded end would disappear, which is not what fading a line means.
    for tip, other, geom in ((p1, p2, heads[0]), (p2, p1, heads[1])):
        placed = strokegeom.place_head(tip, other, geom)
        for line_start, line_end in placed["lines"]:
            out.append({
                "kind": "path", **common,
                "segments": [("move",) + line_start, ("line",) + line_end],
                "closed": False, "fill": None, "stroke": own["color"],
                "line_width_pt": own["line_width_pt"],
                "opacity": own["opacity"],
            })
        if placed["polygon"]:
            out.append(_filled_path(placed["polygon"], own["color"],
                                    own["opacity"], common))
    return out


def _filled_path(polygon, colour, opacity, common):
    """A closed polygon as a `path` instruction -- solid ink, no outline.

    No stroke at all rather than a zero-width one: `_pdf_path` reads a missing
    stroke as "do not stroke", and a hairline round the edge of a taper would be
    a second, differently-shaped drawing of the same shape.
    """
    return {
        "kind": "path", **common,
        "segments": ([("move",) + polygon[0]]
                     + [("line",) + point for point in polygon[1:]]
                     + [("close",)]),
        "closed": True, "fill": colour, "stroke": None,
        "line_width_pt": 0.0, "opacity": opacity,
    }


def _rotated(instructions, annotation):
    """Stamp a rotation onto instructions that need one.

    Rotation is applied AROUND a finished layout and is never an input to it:
    the lines are stacked square, and each backend then turns the whole block
    about the box's centre. One rule, applied three times, that the arithmetic
    above never has to know about -- and the reason a rotated caption cannot
    break its lines differently from an upright one.
    """
    rotation = annotation["geometry"].get("rotation") or 0.0
    if not rotation:
        return instructions
    geometry = annotation["geometry"]
    pivot = {"x": geometry["x_mm"] + geometry["w_mm"] / 2,
             "y": geometry["y_mm"] + geometry["h_mm"] / 2}
    return [{**item, "rotation": rotation, "pivot": pivot} for item in instructions]


def scale_bar(document, panel):
    """The bar's length as a fraction of the panel, and its label -- or None.

    Two kinds of bar, and which one is drawn is decided by what the picture can
    actually support rather than by what the panel asked for. A PHYSICAL bar
    needs the source's calibration; nothing here invents one, because a scale
    bar drawn from an assumed pixel size is wrong and looks exactly like one
    that is right, which is the single worst thing a figure tool can produce.

    A PIXEL bar needs nothing at all -- the viewport is in image pixels by
    construction -- and says "500 px", which is a true statement about the
    picture. So it is the fallback whenever there is no calibration, not only
    when the panel explicitly asked for `unit == "px"`. An uncalibrated import
    used to get no bar and no explanation, and a user who switched the bar on,
    typed a length and saw nothing appear had no way to find out why.

    `FigureCanvas.scaleBarLength` is the mirror of this arithmetic.
    """
    if not panel["scalebar"]["visible"]:
        return None

    # Across the PANEL: a turned capture's frame, not the box around it
    # (`schema.frame_size`, read inline -- this module stays free of the
    # schema's machinery).
    # The width the panel SHOWS, which is its frame's width unless the panel's
    # shape crops it (`visible_frame`).
    viewport = panel["scene"]["viewport"]
    place = panel.get("placement") or {}
    span_px, _ = visible_frame(viewport, place.get("w_mm") or 0, place.get("h_mm") or 0)
    source = document["sources"].get(panel["source_id"])
    pixel_size = source and source.get("pixel_size")
    calibrated = bool(pixel_size and pixel_size.get("value"))

    if panel["scalebar"].get("unit") == "px" or not calibrated:
        length = panel["scalebar"].get("target_px") or round_length(span_px)
        if not length or length > span_px:
            return None
        return {"fraction": length / span_px,
                "label": format_pixels(length),
                "length_px": length}

    span_um = span_px * pixel_size["value"]
    length = panel["scalebar"]["target_um"] or round_length(span_um)
    if not length or length > span_um:
        return None
    return {"fraction": length / span_um,
            "label": format_microns(length, panel["scalebar"].get("unit")),
            "length_um": length}


def round_length(span_um):
    """A round number of microns that sits comfortably inside the field.

    A quarter of the width, snapped down to something a reader recognises. The
    same sequence the canvas uses, so the preview and the export agree.
    """
    if span_um <= 0:
        return None
    target = span_um / 4
    best = None
    for value in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500,
                  1000, 2000, 5000, 10000):
        if value <= target:
            best = value
    return best


#: What one of each PHYSICAL scale-bar unit is worth in microns. "px" is not
#: here on purpose: it is not a length in microns at all, and putting it in this
#: table with some conversion factor is exactly the invented calibration the
#: whole scale-bar path exists to refuse. See `format_pixels`.
SCALEBAR_UNIT_UM = {"nm": 0.001, "um": 1.0, "mm": 1000.0, "cm": 10000.0}
SCALEBAR_UNIT_TEXT = {"nm": "nm", "um": "µm", "mm": "mm", "cm": "cm"}


def format_pixels(value):
    """A length in IMAGE PIXELS, written for an uncalibrated bar.

    Whole pixels: a fractional count of them is a number the picture cannot
    support, and the bar is drawn to whatever the arithmetic says either way.

    Mirrored by `FigureSchema.formatPixels`.
    """
    if not value or value <= 0:
        return ""
    return f"{round(value)} px"


def format_microns(value, unit="auto"):
    """A length in microns, written the way the panel asks for.

    "auto" is the original behaviour and the default: microns up to a
    millimetre, then millimetres. Naming a unit instead is what makes a row of
    panels comparable at a glance -- "500 µm" beside "1000 µm" rather than
    beside "1 mm", which a reader has to convert before they can compare.

    Mirrored by `FigureSchema.formatMicrons`, so the canvas and the export
    print the same caption.
    """
    if unit in SCALEBAR_UNIT_UM:
        scaled = value / SCALEBAR_UNIT_UM[unit]
        text = f"{scaled:.2f}".rstrip("0").rstrip(".") if scaled < 100 \
            else f"{round(scaled)}"
        return f"{text or '0'} {SCALEBAR_UNIT_TEXT[unit]}"
    if value >= 1000:
        text = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text} mm"
    if value >= 10:
        return f"{round(value)} µm"
    return f"{value:.1f} µm"


def label_for(index, style="A"):
    """A..Z, then AA, AB -- the spreadsheet-column sequence, not plain base 26.

    Position 26 is "AA" and not "BA", which is what a reader expects and what
    plain base conversion gets wrong.
    """
    if style == "A1":
        return "A" + str(index + 1)
    number = max(0, int(index))
    out = ""
    while True:
        out = chr(65 + (number % 26)) + out
        number = number // 26 - 1
        if number < 0:
            break
    return out.lower() if style == "a" else out


def panel_pixels(width_mm, height_mm, dpi):
    """How many pixels a panel of this size needs at this DPI."""
    return (max(1, int(round(width_mm / MM_PER_INCH * dpi))),
            max(1, int(round(height_mm / MM_PER_INCH * dpi))))


def _pt_to_mm(points):
    return points * MM_PER_INCH / 72.0


def format_intensity(value):
    """A raw intensity, written for a tick label.

    Plain integers all the way up, because these are 16-bit camera counts and
    "20000" is what a reader recognises -- the general-purpose formatter turned
    it into "2.0e+04", which is a number nobody would type into an acquisition
    setting. Only genuinely fractional values keep a decimal.

    Mirrored by `FigureSchema.formatIntensity`.
    """
    magnitude = abs(value)
    if magnitude >= 10 or value == int(value):
        return f"{int(round(value))}"
    if magnitude >= 0.01:
        return f"{round(value, 2):g}"
    return f"{value:.1e}"
