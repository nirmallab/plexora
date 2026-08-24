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

from plexora.plugins.figure_builder.server import textmetrics

MM_PER_INCH = 25.4

#: Padding between a panel's edge and the furniture drawn inside it.
INSET_MM = 1.2

#: Height of a legend row and of a scale bar, in millimetres.
LEGEND_ROW_MM = 3.0
LEGEND_SWATCH_MM = 2.0
SCALEBAR_HEIGHT_MM = 0.8


def page_instructions(document, page, panels, annotations):
    """Everything drawn on one page, back to front.

    Order is the order it is drawn in: panels first, then their furniture, then
    annotations on top. Within panels, `placement.z` -- which is what the canvas
    z-orders by, so a figure exports stacked the way it looked.
    """
    style = document["settings"]["style"]
    label_style = document["settings"]["label_style"]
    instructions = []

    for index, panel in enumerate(sorted(panels, key=_reading_order)):
        place = panel["placement"]
        instructions.append({
            "kind": "panel", "panel_id": panel["panel_id"],
            "x": place["x_mm"], "y": place["y_mm"],
            "w": place["w_mm"], "h": place["h_mm"],
        })
        instructions.extend(_panel_furniture(document, panel, index, label_style, style))

    for annotation in sorted(annotations, key=lambda a: a["z"]):
        instructions.extend(_annotation(annotation, style))

    return instructions


def _reading_order(panel):
    place = panel["placement"]
    # Rows before columns, matching the canvas: a reader numbers a grid
    # left-to-right and top-to-bottom, not by when each field was captured.
    return (place["y_mm"], place["x_mm"], place["z"])


def _panel_furniture(document, panel, index, label_style, style):
    place = panel["placement"]
    family = style["font_family"]
    out = []
    cursor = place["y_mm"] + INSET_MM

    label = panel["label"]["text"] if not panel["label"]["auto"] else label_for(index, label_style)
    if panel["label"]["visible"] and label:
        out.append(_furniture(label, place["x_mm"] + INSET_MM, cursor,
                              place["w_mm"] - 2 * INSET_MM, "left",
                              style["label_size_pt"], "#ffffff", family, bold=True))
        cursor += _pt_to_mm(style["label_size_pt"]) + 0.6

    for row in legend_rows(panel):
        out.append({
            "kind": "swatch", "x": place["x_mm"] + INSET_MM, "y": cursor,
            "w": LEGEND_SWATCH_MM, "h": LEGEND_SWATCH_MM,
            "color": row.get("color", "#ffffff"), "ramp": row.get("ramp"),
        })
        out.append(_furniture(
            row["label"], place["x_mm"] + INSET_MM + LEGEND_SWATCH_MM + 0.8, cursor,
            place["w_mm"], "left", style["font_size_pt"], "#ffffff", family))
        cursor += LEGEND_ROW_MM

    bar = scale_bar(document, panel)
    if bar:
        width = bar["fraction"] * place["w_mm"]
        x = place["x_mm"] + place["w_mm"] - INSET_MM - width
        y = place["y_mm"] + place["h_mm"] - INSET_MM - SCALEBAR_HEIGHT_MM
        out.append({"kind": "rect", "x": x, "y": y, "w": width, "h": SCALEBAR_HEIGHT_MM,
                    "fill": "#ffffff", "stroke": None, "line_width_pt": 0})
        out.append(_furniture(
            bar["label"], x, y - _pt_to_mm(style["font_size_pt"]) - 0.4, width, "right",
            style["font_size_pt"], "#ffffff", family))

    if panel["title"]:
        out.append(_furniture(
            panel["title"], place["x_mm"], place["y_mm"] + place["h_mm"] + 0.8,
            place["w_mm"], "center", style["title_size_pt"], style["text_color"], family))
    return out


#: Where one line of furniture sits below the y it is anchored to, in ems.
#:
#: Both backends used to compute this for themselves, from this same 0.8, and it
#: is kept to the digit rather than replaced with the font's real ascent so that
#: re-exporting a figure made before this change does not move its panel labels.
#: A text annotation is laid out properly instead -- see `_text_layout` -- but
#: that is new behaviour rather than a silent shift under an existing figure.
FURNITURE_ASCENT_EM = 0.8


def _furniture(text, x, y_top, w, align, size_pt, colour, family, bold=False):
    """One line of panel furniture, as a one-run text instruction.

    Furniture and annotation text share the instruction so that each backend has
    exactly one text branch. `y` is the BASELINE here as everywhere else, so the
    cap-height offset each backend used to apply is applied once, here.
    """
    return {
        "kind": "text", "x": x, "w": w, "align": align,
        "y": y_top + size_pt * textmetrics.MM_PER_PT * FURNITURE_ASCENT_EM,
        "runs": [_run(text, family, size_pt, colour, bold=bold)],
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
        return [{"kind": annotation["type"], **common,
                 "stroke": own["color"], "line_width_pt": own["line_width_pt"]}]
    return _rotated([{"kind": annotation["type"], **common,
                      "fill": own["fill"] or None, "stroke": own["color"],
                      "line_width_pt": own["line_width_pt"]}], annotation)


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


def legend_rows(panel):
    """The legend a panel asks for, from what was recorded at capture time.

    Never recomputed from a live plugin: the plugin may be a version on, a
    palette on, or not installed at all, and a legend that disagrees with the
    panel above it is worse than no legend.
    """
    rows = []
    if panel["legend"]["channels"]:
        for channel in panel["scene"]["channels"]:
            colour = channel["color"]
            rows.append({
                "label": channel["fullname_at_capture"] or channel["key"],
                "color": "#{:02x}{:02x}{:02x}".format(colour["r"], colour["g"], colour["b"]),
            })
    if panel["legend"]["plugins"]:
        for contribution in panel["scene"]["plugins"].values():
            for entry in contribution["legend"]:
                if entry["kind"] == "continuous":
                    low, high = (entry.get("domain") or [0, 1])[:2]
                    rows.append({"label": f"{_number(low)}–{_number(high)}",
                                 "ramp": entry.get("ramp") or [], "color": "#ffffff"})
                else:
                    rows.append({"label": entry["label"], "color": entry.get("color", "#ffffff")})
    return rows


def scale_bar(document, panel):
    """The bar's length as a fraction of the panel, and its label -- or None.

    None whenever the source has no physical calibration. Nothing here invents
    one: a scale bar drawn from an assumed pixel size is wrong and looks exactly
    like one that is right, which is the single worst thing a figure tool can
    produce.
    """
    if not panel["scalebar"]["visible"]:
        return None
    source = document["sources"].get(panel["source_id"])
    pixel_size = source and source.get("pixel_size")
    if not pixel_size or not pixel_size.get("value"):
        return None

    span_um = panel["scene"]["viewport"]["w"] * pixel_size["value"]
    length = panel["scalebar"]["target_um"] or round_length(span_um)
    if not length or length > span_um:
        return None
    return {"fraction": length / span_um, "label": format_microns(length), "length_um": length}


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


def format_microns(value):
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


def _number(value):
    magnitude = abs(value)
    if magnitude >= 1000 or (0 < magnitude < 0.01):
        return f"{value:.1e}"
    return f"{round(value, 2):g}"
