"""Writing a figure out: PDF, PNG, TIFF.

One layout (`compose.page_instructions`) rendered by two backends, so the two
formats cannot disagree about where a label sits. The split between them is the
one that matters for a publication:

    PDF     microscopy panels are rasters; text, labels, legends, scale bars,
            arrows and shapes are real vector objects. That is what makes the
            file editable in Illustrator without being a multi-gigabyte attempt
            to turn half a million cells into polygons (spec §51).
    PNG     everything flattened. For circulating a draft.
    TIFF    everything flattened, at the requested DPI, for submission.

Every panel is re-rendered from the source image at the size the page asks for.
That is the whole point: a panel captured at 300 screen pixels comes out at
however many the DPI demands, and the quality of the export has nothing to do
with the monitor it was laid out on.

`reportlab` is a hard dependency, but still imported lazily: an environment that
somehow lost it can still export PNG and TIFF, and asking for a PDF says what to
reinstall rather than failing with an ImportError from three frames down.
"""

from __future__ import annotations

import json
from pathlib import Path

from plexora.plugins.figure_builder.server import (
    compose, provenance, render, repository, schema, shapegeom, strokegeom,
    textmetrics)

#: Formats this can write. Ordered as the dialog offers them.
FORMATS = ("pdf", "png", "tiff")

#: The DPI a page's own furniture is rasterised at for PNG/TIFF. Panels are
#: rendered at the requested DPI regardless; this is the resolution text and
#: rules land at.
RASTER_MIN_DPI = 72


class ExportUnavailable(Exception):
    """A format this build cannot write, with the install line to fix it."""


def export(document, out_dir, options, progress=None, cancelled=None):
    """Write a figure and return a report.

    `progress(done, total, message)` is called between panels and `cancelled()`
    is checked at the same points -- between panels rather than inside one,
    because a panel is the smallest unit that leaves a coherent file behind and
    tearing out of the middle of one buys milliseconds.
    """
    fmt = options.get("format", "pdf")
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    dpi = int(options.get("dpi") or document["settings"]["dpi_default"])

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(document["title"]) or document["figure_id"]

    pages = _pages(document)
    total = sum(len(page["panels"]) for page in pages) or 1
    done = 0
    results = []

    # One handle per datasource, held across the whole export: a figure is
    # routinely eight panels from one slide, and reopening a pyramidal TIFF
    # eight times is eight directory walks for the same answer.
    sources = {}
    try:
        rendered_pages = []
        for page in pages:
            drawn = {}
            for panel in page["panels"]:
                if cancelled and cancelled():
                    return {"cancelled": True}
                if progress:
                    progress(done, total, f"Rendering panel {done + 1} of {total}")
                image, result = _render_one(document, panel, sources, dpi)
                drawn[panel["panel_id"]] = image
                results.append((panel, result))
                done += 1
            rendered_pages.append({**page, "images": drawn})

        if progress:
            progress(done, total, "Building provenance")
        manifest = provenance.manifest(document, results, {**options, "dpi": dpi})
        report_lines = provenance.lines(document, results, {**options, "dpi": dpi})

        if progress:
            progress(done, total, f"Writing {fmt.upper()}")
        if fmt == "pdf":
            path = out_dir / f"{stem}.pdf"
            fonts = _write_pdf(document, rendered_pages, path, dpi, report_lines,
                               include_provenance=options.get("provenance", True))
            written = [path]
        else:
            written, fonts = _write_rasters(
                document, rendered_pages, out_dir, stem, fmt, dpi)

        manifest_path = out_dir / f"{stem}-provenance.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    finally:
        # `None` is cached for a source that could not be opened, so that one
        # missing image is not re-attempted once per panel. Skipped here rather
        # than not cached, because the retry is the expensive half.
        for source in sources.values():
            if source is not None:
                source.close()

    return {
        "cancelled": False,
        "files": [str(path) for path in written] + [str(manifest_path)],
        "download": str(written[0]),
        "panels": len(results),
        "warnings": _warnings(results, fonts),
    }


def preflight(document, options):
    """What the user should know before committing to an export.

    Opens each source and asks it, so "this panel is 96 DPI at that size" and
    "this channel is gone" are answered before a single pixel is rendered
    rather than in a warning after the file is written.
    """
    dpi = int(options.get("dpi") or document["settings"]["dpi_default"])
    sources = {}
    reports = []
    try:
        for page in _pages(document):
            for panel in page["panels"]:
                origin = document["sources"].get(panel["source_id"]) or {}
                if origin.get("kind") == "imported_asset":
                    # Nothing to open and nothing to reconstruct: the file IS
                    # the panel. Its effective resolution is still worth
                    # reporting, because enlarging a 400-pixel schematic onto a
                    # third of a page is the same soft result as enlarging a
                    # crop.
                    reports.append((panel, _imported_report(origin, panel, dpi)))
                    continue
                source = _source_for(document, panel, sources)
                if source is None:
                    reports.append((panel, {"missing_source": True,
                                            "missing_channels": [], "missing_overlays": []}))
                    continue
                reports.append((panel, render.panel_report(
                    source, panel["scene"], panel["placement"]["w_mm"], dpi)))
    finally:
        for source in sources.values():
            if source is not None:
                source.close()

    return {
        "dpi": dpi,
        "panels": len(reports),
        "warnings": _warnings(reports),
        "per_panel": [
            {"panel_id": panel["panel_id"], **report} for panel, report in reports
        ],
    }


# -- rendering -----------------------------------------------------------


def _pages(document):
    from plexora.plugins.figure_builder.server import schema  # noqa: F401  (shape only)

    pages = []
    for page in document["pages"]:
        panels = [panel for panel in document["panels"].values()
                  if panel["placement"] and panel["placement"]["page_id"] == page["page_id"]]
        annotations = [a for a in document["annotations"].values()
                       if a["page_id"] == page["page_id"]]
        pages.append({"page": page, "panels": panels, "annotations": annotations})
    return pages


def _source_for(document, panel, cache):
    source = document["sources"].get(panel["source_id"])
    if not source or source["kind"] != "plexora_project" or not source["datasource"]:
        return None
    name = source["datasource"]
    if name not in cache:
        try:
            cache[name] = render.SourceImage(name)
        except (KeyError, render.RenderError, OSError):
            cache[name] = None
    return cache[name]


def _imported_report(source, panel, dpi):
    """What is worth saying about an imported panel.

    Same shape as `render.panel_report`, so the warning machinery does not have
    to know which kind of source a panel came from. There are no channels and no
    overlays; what there is is a resolution, and enlarging a 400-pixel schematic
    onto a third of a page is exactly as soft as enlarging a crop.
    """
    width_px = (source.get("image") or {}).get("width") or 0
    return {
        "missing_channels": [], "missing_overlays": [],
        "effective_dpi": round(render.effective_dpi(width_px, panel["placement"]["w_mm"]), 1),
        "requested_dpi": dpi,
    }


def _render_imported(document, panel, figure_id, width_px, height_px):
    """An imported PNG, JPEG or TIFF, resampled to the page.

    Read from the figure's own directory rather than re-rendered: for a
    schematic or a supporting RGB image the file IS the panel, and there is
    nothing to reconstruct it from at a higher resolution than it already has.
    It is enlarged if the page asks for more, and the preflight says so.
    """
    from PIL import Image

    source = document["sources"].get(panel["source_id"]) or {}
    path = repository.asset_path(figure_id, source.get("asset_id") or "")
    if path is None:
        return None
    with Image.open(path) as opened:
        return opened.convert("RGB").resize((width_px, height_px), Image.LANCZOS)


def _render_one(document, panel, sources, dpi):
    from PIL import Image

    width_px, height_px = compose.panel_pixels(
        panel["placement"]["w_mm"], panel["placement"]["h_mm"], dpi)

    origin = document["sources"].get(panel["source_id"]) or {}
    if origin.get("kind") == "imported_asset":
        image = _render_imported(document, panel, document["figure_id"], width_px, height_px)
        if image is not None:
            return image, {**_imported_report(origin, panel, dpi), "imported": True}
        return (Image.new("RGB", (width_px, height_px), "black"),
                {"missing_source": True, "missing_channels": [], "missing_overlays": [],
                 "effective_dpi": 0, "requested_dpi": dpi})

    source = _source_for(document, panel, sources)
    if source is None:
        # The source is gone. A black panel rather than a hole, and the
        # provenance says which panel and why -- an export that silently
        # dropped it would be a figure with a missing field nobody noticed.
        return (Image.new("RGB", (width_px, height_px), "black"),
                {"missing_source": True, "missing_channels": [], "missing_overlays": [],
                 "effective_dpi": 0, "requested_dpi": dpi})

    report = render.panel_report(source, panel["scene"], panel["placement"]["w_mm"], dpi)
    image, detail = render.render_panel(source, panel["scene"], width_px, height_px)
    return image, {**report, **detail}


def _warnings(results, fonts=()):
    """The whole export's caveats, deduplicated and phrased for a person."""
    out = []
    if fonts:
        # Named rather than omitted, like every other limitation this exporter
        # reports. The layout is still right on the raster path -- Pillow's
        # fallback measures and places a baseline correctly -- so the honest
        # statement is about the typeface and not about the positions.
        out.append({
            "kind": "substituted_font", "fonts": sorted(fonts),
            "message": "Some text was drawn in a substitute typeface ("
                       + ", ".join(sorted(fonts))
                       + " was not available). The PDF is the typographic master.",
        })
    low = [panel["panel_id"] for panel, report in results
           if report.get("effective_dpi") and report.get("requested_dpi")
           and report["effective_dpi"] < report["requested_dpi"] * 0.75]
    if low:
        out.append({
            "kind": "low_resolution", "panels": low,
            "message": "Some panels are enlarged past what their source holds. They "
                       "will export, but they cannot become sharper than the image is.",
        })
    missing_source = [panel["panel_id"] for panel, report in results
                      if report.get("missing_source")]
    if missing_source:
        out.append({"kind": "missing_source", "panels": missing_source,
                    "message": "Some panels' images are no longer available and export black."})
    overlays = sorted({name for _, report in results
                       for name in report.get("missing_overlays") or []})
    if overlays:
        out.append({
            "kind": "overlays_not_rendered", "panels": overlays,
            "message": "Overlays drawn by a tool (" + ", ".join(overlays) + ") are not "
                       "re-rendered at export resolution. The provenance page says so.",
        })
    return out


# -- PDF -----------------------------------------------------------------


def _write_pdf(document, pages, path, dpi, report_lines, include_provenance=True):
    try:
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as pdf_canvas
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExportUnavailable(
            "PDF export needs reportlab. Install it with:  pip install reportlab"
        ) from exc

    pdf = pdf_canvas.Canvas(str(path))
    pdf.setTitle(document["title"])
    pdf.setSubject(f"Plexora figure {document['figure_id']} revision {document['revision']}")
    # Fonts this export could not honour, collected as it goes and stated on the
    # provenance page rather than silently substituted.
    notes = set()

    for entry in pages:
        page = entry["page"]
        width_mm, height_mm = page["size_mm"]["w"], page["size_mm"]["h"]
        pdf.setPageSize((width_mm * mm, height_mm * mm))
        _fill(pdf, page["background"], 0, 0, width_mm, height_mm, height_mm, mm)

        for item in compose.page_instructions(document, page, entry["panels"], entry["annotations"]):
            _draw_pdf(pdf, item, entry["images"], height_mm, notes, mm, ImageReader)
        pdf.showPage()

    if include_provenance:
        _provenance_page(pdf, report_lines + _font_lines(notes), document, mm)
    pdf.save()
    return notes


def _font_lines(notes):
    """What the PDF could not set, for the provenance page.

    Unreachable unless `textmetrics._POSTSCRIPT` has a hole in it, since the
    families are whitelisted on the way into the document -- which is the point:
    if it ever does fire, it says so on the artefact instead of shipping the
    wrong typeface quietly.
    """
    if not notes:
        return []
    return ["Fonts substituted: " + ", ".join(sorted(notes))]


def _fill(pdf, colour, x, y, width, height, page_height, mm):
    # A transparent page has no fill to draw. PDF supports transparency, but a
    # figure with no page at all under it composites onto whatever the reader's
    # viewer paints -- usually a grey app chrome -- so it is rendered white and
    # the export dialog says which formats can carry the alpha.
    if not colour or colour == schema.TRANSPARENT:
        return
    pdf.setFillColor(colour)
    pdf.rect(x * mm, (page_height - y - height) * mm, width * mm, height * mm,
             stroke=0, fill=1)


def _pdf_font(run, notes):
    """The PostScript font for one run, or the default with a note saying so.

    `postscript_name` raises rather than substituting quietly, because by the
    time a run reaches here its family has been through the schema's whitelist:
    a miss is a typo in the table or a family added to the UI and not to it.
    The fallback is still right -- a missing font is a style problem and not a
    reason to lose an export -- but it is recorded, and the provenance page
    names it. Silently shipping the wrong typeface is the one option that is not
    on the table.
    """
    try:
        return textmetrics.postscript_name(run["family"], run["bold"], run["italic"])
    except KeyError:
        notes.add(run["family"])
        return textmetrics.postscript_name(
            textmetrics.DEFAULT_FAMILY, run["bold"], run["italic"])


def _pdf_decorations(pdf, run, pen, baseline, width, mm):
    """Underline and strike-through, drawn rather than delegated.

    A PDF has no underline of its own -- the mark is always a drawn rule -- and
    the canvas draws its own for the same reason SVG's would be wrong: the
    browser takes the position from whatever font file it actually resolved,
    which is Arial where the PDF holds Helvetica. Both sides use the same
    fraction of the em instead, so the rule lands in the same place.
    """
    # reportlab's user space IS points, so one em is the size in points and
    # needs no conversion. `mm` is only the multiplier for lengths arriving in
    # millimetres.
    em = run["size_pt"]
    for on, offset in ((run["underline"], -textmetrics.UNDERLINE_OFFSET_EM),
                       (run["strike"], textmetrics.STRIKE_OFFSET_EM)):
        if not on:
            continue
        thickness = em * textmetrics.UNDERLINE_THICKNESS_EM
        pdf.setFillColor(run["color"] or "#000000")
        pdf.rect(pen, baseline + em * offset, width, thickness, stroke=0, fill=1)


def _draw_pdf(pdf, item, images, page_height, notes, mm, ImageReader):
    # PDF's origin is bottom-left and the document's is top-left. Converted
    # here, once, so nothing upstream has to hold two coordinate systems.
    def top(y, height=0.0):
        return (page_height - y - height) * mm

    # A rotation is applied AROUND the finished layout, never inside it: the
    # instruction's coordinates are square and this turns the page under them,
    # about the box's centre. Same rule in the canvas and the raster writer, so
    # a rotated caption cannot lay out differently from an upright one.
    rotation = item.get("rotation")
    if rotation:
        pivot = item["pivot"]
        pdf.saveState()
        pdf.translate(pivot["x"] * mm, top(pivot["y"]))
        pdf.rotate(-rotation)
        pdf.translate(-pivot["x"] * mm, -top(pivot["y"]))
        try:
            _draw_pdf(pdf, {k: v for k, v in item.items() if k != "rotation"},
                      images, page_height, notes, mm, ImageReader)
        finally:
            pdf.restoreState()
        return

    kind = item["kind"]
    if kind == "panel":
        image = images.get(item["panel_id"])
        if image is not None:
            pdf.drawImage(ImageReader(image), item["x"] * mm, top(item["y"], item["h"]),
                          item["w"] * mm, item["h"] * mm, mask=None)
        return

    if kind == "text":
        from reportlab.pdfbase import pdfmetrics

        # Every position bar one is already decided: the instruction carries the
        # baseline and the box to align in, and this walks a pen along the line.
        # Advancing by the width of what it just drew is not a decision -- it is
        # what drawing a string means -- so the two backends cannot disagree.
        widths = [pdfmetrics.stringWidth(run["text"], _pdf_font(run, notes),
                                         run["size_pt"]) for run in item["runs"]]
        line_width = sum(widths)
        # Trailing whitespace is excluded from the width so that a wrapped line
        # centres on its words: the space the break landed on is kept in the run
        # (it is what makes the break reversible) and would otherwise pull every
        # centred line a little to the left.
        if item["runs"] and item["runs"][-1]["text"] != item["runs"][-1]["text"].rstrip():
            stripped = item["runs"][-1]["text"].rstrip()
            line_width -= widths[-1] - pdfmetrics.stringWidth(
                stripped, _pdf_font(item["runs"][-1], notes), item["runs"][-1]["size_pt"])

        box_width = item.get("w", 0) * mm
        align = item.get("align", "left")
        if align == "center":
            pen = item["x"] * mm + (box_width - line_width) / 2
        elif align == "right":
            pen = item["x"] * mm + box_width - line_width
        else:
            pen = item["x"] * mm
        # Justify spreads the slack across the gaps between words rather than
        # scaling anything, and never runs on the last line of a paragraph --
        # `compose` has already turned that one into "left".
        extra = 0.0
        if align == "justify":
            gaps = sum(run["text"].count(" ") for run in item["runs"])
            extra = (box_width - line_width) / gaps if gaps and box_width > line_width else 0.0

        baseline = top(item["y"])
        for run, width in zip(item["runs"], widths):
            font = _pdf_font(run, notes)
            pdf.setFont(font, run["size_pt"])
            pdf.setFillColor(run["color"] or "#000000")
            spread = extra * run["text"].count(" ")
            if extra:
                pdf.drawString(pen, baseline, run["text"], wordSpace=extra)
            else:
                pdf.drawString(pen, baseline, run["text"])
            _pdf_decorations(pdf, run, pen, baseline, width + spread, mm)
            pen += width + spread
        return

    if kind == "swatch":
        ramp = item.get("ramp")
        if ramp and item.get("vertical"):
            # Stops run DOWN the box, first at the top. A colour bar reverses
            # its own list before it gets here (see `_colorbar_lane_vertical`)
            # so this stays one rule rather than two.
            step = item["h"] / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                pdf.setFillColor(colour)
                # A hair of overlap, because adjacent fills at fractional
                # coordinates leave a white seam the ramp is meant not to have.
                pdf.rect(item["x"] * mm, top(item["y"] + index * step, step * 1.02),
                         item["w"] * mm, step * 1.02 * mm, stroke=0, fill=1)
        elif ramp:
            step = item["w"] / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                pdf.setFillColor(colour)
                pdf.rect((item["x"] + index * step) * mm, top(item["y"], item["h"]),
                         step * 1.02 * mm, item["h"] * mm, stroke=0, fill=1)
        else:
            pdf.setFillColor(item["color"])
            pdf.rect(item["x"] * mm, top(item["y"], item["h"]),
                     item["w"] * mm, item["h"] * mm, stroke=0, fill=1)
        return

    if kind == "path":
        _pdf_path(pdf, item, top, mm)
        return

    pdf.setLineWidth(item.get("line_width_pt", 0.75))
    if item.get("stroke"):
        pdf.setStrokeColor(item["stroke"])
    if item.get("fill"):
        pdf.setFillColor(item["fill"])

    if kind == "rect":
        pdf.rect(item["x"] * mm, top(item["y"], item["h"]), item["w"] * mm, item["h"] * mm,
                 stroke=1 if item.get("stroke") else 0, fill=1 if item.get("fill") else 0)
    elif kind == "ellipse":
        pdf.ellipse(item["x"] * mm, top(item["y"], item["h"]),
                    (item["x"] + item["w"]) * mm, top(item["y"]),
                    stroke=1 if item.get("stroke") else 0, fill=1 if item.get("fill") else 0)
    elif kind == "line":
        _pdf_line(pdf, item, top, mm)


def _pdf_path(pdf, item, top, mm):
    """A shape, as one reportlab path.

    Real vector output, which is the whole reason the PDF branch exists: the
    curves come through as curves and stay editable in Illustrator, where the
    raster branch has to flatten them.

    Opacity is set on the graphics state rather than mixed into the colour, so
    a translucent shape over a panel lets the panel through instead of merely
    being a paler colour. Guarded by `hasattr`, because a build old enough to
    lack it should draw the shape opaque -- a loss the user can see and reason
    about -- rather than fail the export.
    """
    segments = item.get("segments") or []
    stroke = item.get("stroke")
    fill = item.get("fill")
    if not segments or (not stroke and not fill):
        return

    pdf.saveState()
    try:
        opacity = item.get("opacity", 1.0)
        if opacity < 1.0 and hasattr(pdf, "setFillAlpha"):
            pdf.setFillAlpha(opacity)
            pdf.setStrokeAlpha(opacity)
        path = pdf.beginPath()
        for segment in segments:
            if segment[0] == "move":
                path.moveTo(segment[1] * mm, top(segment[2]))
            elif segment[0] == "line":
                path.lineTo(segment[1] * mm, top(segment[2]))
            elif segment[0] == "curve":
                path.curveTo(segment[1] * mm, top(segment[2]),
                             segment[3] * mm, top(segment[4]),
                             segment[5] * mm, top(segment[6]))
            elif segment[0] == "close":
                path.close()
        pdf.setLineWidth(item.get("line_width_pt", 0.75))
        # Round joins, matching the canvas's `stroke-linejoin="round"`. A star
        # at 0.75pt is all corners, and mitred spikes where the screen showed
        # round ones is a difference between the preview and the paper.
        pdf.setLineJoin(1)
        if stroke:
            pdf.setStrokeColor(stroke)
        if fill:
            pdf.setFillColor(fill)
        pdf.drawPath(path, stroke=1 if stroke else 0, fill=1 if fill else 0)
    finally:
        pdf.restoreState()


def _pdf_line(pdf, item, top, mm):
    """A line's shaft, with whatever dash and fade it carries.

    There is no arrowhead code here any more. A head arrives as a `path`
    instruction from `compose._stroke` and is drawn by `_pdf_path`, which means
    the PDF and the canvas cannot draw one differently -- neither of them knows
    what an arrowhead is.

    Everything is inside saveState/restoreState. A dash pattern, a line cap and
    an alpha are graphics STATE in a PDF: set one and fail to unset it and every
    later instruction on the page inherits it, which surfaces as a dashed panel
    border three annotations further down and no clue where it came from.

    A gradient cannot be applied to a PDF stroke at all, so a fade is drawn as a
    run of short segments each at its own alpha. `shaft_render_plan` decides
    where they fall, and the raster writer walks the identical plan -- which is
    what keeps a dashed fade's dashes in the same places in both files.
    """
    stroke = item.get("stroke")
    if not stroke:
        return
    x1, y1 = item["x"] * mm, top(item["y"])
    x2, y2 = (item["x"] + item["w"]) * mm, top(item["y"] + item["h"])
    dash = item.get("dash_pt")
    fade = item.get("fade")
    opacity = item.get("opacity", 1.0)
    # Guarded the way `_pdf_path` guards it: a build too old to have alpha
    # should draw the line opaque, which the user can see and reason about,
    # rather than fail the export.
    alpha_ok = hasattr(pdf, "setStrokeAlpha")

    pdf.saveState()
    try:
        pdf.setStrokeColor(stroke)
        pdf.setLineWidth(item.get("line_width_pt", 0.75))
        # Round caps, matching the canvas -- and also what turns a zero-length
        # dash into a dot, which is the whole of "dotted".
        pdf.setLineCap(1)
        if opacity < 1.0 and alpha_ok:
            pdf.setStrokeAlpha(opacity)
        if not fade:
            # The native pattern, because one dashed line is one operator and a
            # plan would be a hundred. `dash_pt` comes out of the enum in
            # `strokegeom.dash_pattern`, never out of a document, because
            # `setDash` RAISES on a negative entry or a cycle summing to zero.
            if dash:
                pdf.setDash(dash)
            pdf.line(x1, y1, x2, y2)
            return
        for start, end, alpha, _is_dot in strokegeom.shaft_render_plan(
                (x1, y1), (x2, y2), dash, fade):
            if alpha_ok:
                pdf.setStrokeAlpha(alpha * opacity)
            pdf.line(start[0], start[1], end[0], end[1])
    finally:
        pdf.restoreState()


def _provenance_page(pdf, lines, document, mm):
    """Appended by default, because a figure that cannot say where it came from
    is a figure a reviewer has to take on trust."""
    from reportlab.lib.pagesizes import A4

    pdf.setPageSize(A4)
    width, height = A4
    cursor = height - 20 * mm
    pdf.setFillColor("#000000")
    for line in lines:
        if cursor < 20 * mm:
            pdf.showPage()
            pdf.setPageSize(A4)
            pdf.setFillColor("#000000")
            cursor = height - 20 * mm
        bold = line and not line.startswith(" ") and line.isupper()
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 8.5)
        pdf.drawString(18 * mm, cursor, line[:160])
        cursor -= 11
    pdf.showPage()


# -- PNG and TIFF --------------------------------------------------------


def _write_rasters(document, pages, out_dir, stem, fmt, dpi):
    """One file per page, flattened.

    Text is drawn with Pillow's own font because these formats have no vector
    concept -- which is exactly why the PDF exists and why this is not described
    as the editable master anywhere in the interface.
    """
    from PIL import Image, ImageDraw

    # Typefaces this build could not find, collected as it goes. Named in the
    # report rather than silently substituted -- the standing rule for this
    # exporter is that what it could not reproduce is stated.
    notes = set()
    written = []
    for index, entry in enumerate(pages):
        page = entry["page"]
        scale = dpi / compose.MM_PER_INCH
        width = max(1, int(round(page["size_mm"]["w"] * scale)))
        height = max(1, int(round(page["size_mm"]["h"] * scale)))
        # PNG is the one format here that can carry an alpha channel, so it is
        # the one that honours a transparent page. TIFF gets white: a
        # submission-format file that composited onto whatever the journal's
        # tooling paints is a figure ruined at the last step, and silently.
        transparent = page["background"] == schema.TRANSPARENT
        if transparent and fmt == "png":
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        else:
            canvas = Image.new(
                "RGB", (width, height),
                "#ffffff" if transparent else page["background"])
        draw = ImageDraw.Draw(canvas)

        for item in compose.page_instructions(document, page, entry["panels"], entry["annotations"]):
            _draw_raster(canvas, draw, item, entry["images"], scale, dpi, notes)

        suffix = "tif" if fmt == "tiff" else fmt
        name = f"{stem}.{suffix}" if len(pages) == 1 else f"{stem}-{index + 1}.{suffix}"
        path = out_dir / name
        canvas.save(path, dpi=(dpi, dpi))
        written.append(path)
    return written, notes


#: Where to look for each family and weight, best match first.
#:
#: Nothing is bundled. Shipping the Liberation faces would make PNG and TIFF
#: typographically faithful and would also add a megabyte and a half, a
#: MANIFEST.in entry, a licence to carry and a font-loading path that can fail
#: for reasons unrelated to the figure -- so it is a change of its own, with its
#: own packaging test, rather than a rider on this one. Until then the raster
#: formats say what they substituted and the PDF stays the typographic master.
_RASTER_CANDIDATES = {
    ("Helvetica", False, False): ("Arial.ttf", "Helvetica.ttc",
                                  "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
    ("Helvetica", True, False): ("Arial Bold.ttf", "Arial-Bold.ttf",
                                 "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"),
    ("Helvetica", False, True): ("Arial Italic.ttf", "LiberationSans-Italic.ttf",
                                 "DejaVuSans-Oblique.ttf"),
    ("Helvetica", True, True): ("Arial Bold Italic.ttf", "LiberationSans-BoldItalic.ttf",
                                "DejaVuSans-BoldOblique.ttf"),
    ("Times-Roman", False, False): ("Times New Roman.ttf", "Times.ttc",
                                    "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"),
    ("Times-Roman", True, False): ("Times New Roman Bold.ttf",
                                   "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"),
    ("Times-Roman", False, True): ("Times New Roman Italic.ttf",
                                   "LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf"),
    ("Times-Roman", True, True): ("Times New Roman Bold Italic.ttf",
                                  "LiberationSerif-BoldItalic.ttf",
                                  "DejaVuSerif-BoldItalic.ttf"),
    ("Courier", False, False): ("Courier New.ttf", "Courier.ttc",
                                "LiberationMono-Regular.ttf", "DejaVuSansMono.ttf"),
    ("Courier", True, False): ("Courier New Bold.ttf", "LiberationMono-Bold.ttf",
                               "DejaVuSansMono-Bold.ttf"),
    ("Courier", False, True): ("Courier New Italic.ttf", "LiberationMono-Italic.ttf",
                               "DejaVuSansMono-Oblique.ttf"),
    ("Courier", True, True): ("Courier New Bold Italic.ttf",
                              "LiberationMono-BoldItalic.ttf",
                              "DejaVuSansMono-BoldOblique.ttf"),
}

#: Resolved fonts, keyed by family, weight and pixel size. A caption is one
#: `truetype` call per run otherwise, and a page of legends is hundreds.
_RASTER_FONTS = {}


def _raster_font(run, dpi, notes):
    """A Pillow font for one run, and a note when it is not the one asked for.

    `ImageFont.load_default(size=...)` returns a real FreeTypeFont on Pillow
    10.1 and up -- and `pyproject.toml` pins 10.4 -- so the fallback still
    measures and still places a baseline correctly. What it cannot do is bold,
    italic, or any family at all: there is one face. So the LAYOUT is right on
    this path and only the typeface is wrong, which is worth stating precisely
    rather than as "raster text is approximate".
    """
    from PIL import ImageFont

    size = max(6, int(round(run["size_pt"] / 72.0 * dpi)))
    key = (run["family"], run["bold"], run["italic"], size)
    if key not in _RASTER_FONTS:
        resolved = None
        for name in _RASTER_CANDIDATES.get(key[:3], ()):
            try:
                resolved = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _RASTER_FONTS[key] = (resolved or ImageFont.load_default(size=size),
                              None if resolved else textmetrics.postscript_name(
                                  run["family"], run["bold"], run["italic"]))
    font, missing = _RASTER_FONTS[key]
    if missing:
        notes.add(missing)
    return font


def _raster_decorations(draw, run, x, baseline, width, scale):
    """Underline and strike, drawn from the same em fractions as everywhere else.

    Note the signs are the opposite of the PDF's: a raster canvas counts y
    downwards, so an underline sits at a POSITIVE offset from the baseline here
    and a negative one there.
    """
    em = run["size_pt"] * textmetrics.MM_PER_PT * scale
    for on, offset in ((run["underline"], textmetrics.UNDERLINE_OFFSET_EM),
                       (run["strike"], -textmetrics.STRIKE_OFFSET_EM)):
        if not on:
            continue
        thickness = max(1.0, em * textmetrics.UNDERLINE_THICKNESS_EM)
        y = baseline + em * offset
        draw.rectangle([x, y, x + width, y + thickness], fill=run["color"] or "#000000")


def _draw_raster(canvas, draw, item, images, scale, dpi, notes):
    from PIL import Image, ImageDraw, ImageFont

    # Pillow cannot rotate a drawing operation, so a rotated instruction is
    # drawn square onto its own transparent layer and the layer is turned about
    # the same pivot the other two renderers use. `rotate` counts anticlockwise
    # and this canvas counts y downwards, so the angle is negated to turn the
    # same way the screen does.
    rotation = item.get("rotation")
    if rotation:
        pivot = item["pivot"]
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        _draw_raster(layer, ImageDraw.Draw(layer),
                     {k: v for k, v in item.items() if k != "rotation"},
                     images, scale, dpi, notes)
        layer = layer.rotate(-rotation, resample=Image.BICUBIC,
                             center=(pivot["x"] * scale, pivot["y"] * scale))
        canvas.paste(layer, (0, 0), layer)
        return

    def box(entry):
        return [entry["x"] * scale, entry["y"] * scale,
                (entry["x"] + entry.get("w", 0)) * scale,
                (entry["y"] + entry.get("h", 0)) * scale]

    kind = item["kind"]
    if kind == "panel":
        image = images.get(item["panel_id"])
        if image is not None:
            canvas.paste(image, (int(item["x"] * scale), int(item["y"] * scale)))
        return
    if kind == "text":
        # The instruction carries the baseline, so this anchors on it ("ls")
        # rather than on the top of the line -- which is what the "la"/"ma"/"ra"
        # anchors used to do, using Pillow's idea of the ascent where the PDF
        # used its own. That was the same decision made twice with two different
        # constants, and it is now made once, in `compose`.
        fonts = [_raster_font(run, dpi, notes) for run in item["runs"]]
        widths = [draw.textlength(run["text"], font=font)
                  for run, font in zip(item["runs"], fonts)]
        line_width = sum(widths)
        if item["runs"] and item["runs"][-1]["text"] != item["runs"][-1]["text"].rstrip():
            line_width -= widths[-1] - draw.textlength(
                item["runs"][-1]["text"].rstrip(), font=fonts[-1])

        box_width = item.get("w", 0) * scale
        align = item.get("align", "left")
        if align == "center":
            pen = item["x"] * scale + (box_width - line_width) / 2
        elif align == "right":
            pen = item["x"] * scale + box_width - line_width
        else:
            pen = item["x"] * scale
        gaps = sum(run["text"].count(" ") for run in item["runs"]) if align == "justify" else 0
        extra = (box_width - line_width) / gaps if gaps and box_width > line_width else 0.0

        baseline = item["y"] * scale
        for run, font, width in zip(item["runs"], fonts, widths):
            spread = extra * run["text"].count(" ")
            if extra:
                # Pillow has no word-spacing, so a justified line is drawn word
                # by word at computed pen positions.
                for index, word in enumerate(run["text"].split(" ")):
                    if index:
                        pen += draw.textlength(" ", font=font) + extra
                    draw.text((pen, baseline), word, fill=run["color"] or "#000000",
                              font=font, anchor="ls")
                    pen += draw.textlength(word, font=font)
            else:
                draw.text((pen, baseline), run["text"], fill=run["color"] or "#000000",
                          font=font, anchor="ls")
                pen += width
            _raster_decorations(draw, run, pen - width - spread, baseline,
                                width + spread, scale)
        return
    if kind == "swatch":
        ramp = item.get("ramp")
        if ramp and item.get("vertical"):
            step = item["h"] * scale / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                near = item["y"] * scale + index * step
                draw.rectangle([item["x"] * scale, near,
                                (item["x"] + item["w"]) * scale, near + step + 1],
                               fill=colour)
        elif ramp:
            step = item["w"] * scale / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                left = item["x"] * scale + index * step
                # One pixel of overlap: abutting fills at fractional
                # coordinates leave seams the ramp is meant not to have.
                draw.rectangle([left, item["y"] * scale, left + step + 1,
                                (item["y"] + item["h"]) * scale], fill=colour)
        else:
            draw.rectangle(box(item), fill=item["color"])
        return
    if kind == "path":
        _raster_path(canvas, item, scale, dpi)
        return
    if kind == "line":
        _raster_line(canvas, item, scale, dpi)
        return
    width = max(1, int(round(item.get("line_width_pt", 0.75) / 72.0 * dpi)))
    if kind == "rect":
        draw.rectangle(box(item), fill=item.get("fill"), outline=item.get("stroke"), width=width)
    elif kind == "ellipse":
        draw.ellipse(box(item), fill=item.get("fill"), outline=item.get("stroke"), width=width)


def _raster_path(canvas, item, scale, dpi):
    """A shape, flattened to a polyline and drawn.

    Pillow has no curve primitive, so the curve is subdivided here rather than
    approximated with a fixed number of steps -- `shapegeom.flatten` spends
    points where the path bends and none where it does not. The tolerance is a
    quarter of a DEVICE pixel, converted out of millimetres, so a 600 dpi page
    gets a finer polyline than a 150 dpi one without anybody choosing a number.

    The outline is `draw.line(joint="curve")` rather than `polygon(width=)`:
    the width argument on `polygon` behaves differently across Pillow versions,
    and an outline that is a pixel out in one install and not another is the
    kind of difference nobody attributes to Pillow.
    """
    from PIL import Image, ImageDraw

    segments = item.get("segments") or []
    stroke = item.get("stroke")
    fill = item.get("fill")
    if not segments or (not stroke and not fill):
        return
    points = [(x * scale, y * scale)
              for x, y in shapegeom.flatten(segments, 0.25 / max(scale, 1e-6))]
    if len(points) < 2:
        return
    closed = bool(item.get("closed"))
    if closed and points[0] != points[-1]:
        points.append(points[0])

    # Opacity is composited, never blended into the colour: a half-transparent
    # shape over a panel has to let the panel through, and a lightened colour
    # over an image is simply the wrong colour.
    opacity = item.get("opacity", 1.0)
    layer = None if opacity >= 0.999 else Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    target = canvas if layer is None else layer
    draw = ImageDraw.Draw(target)

    if fill and closed:
        draw.polygon(points, fill=fill)
    if stroke:
        width = max(1, int(round(item.get("line_width_pt", 0.75) / 72.0 * dpi)))
        draw.line(points, fill=stroke, width=width, joint="curve")

    if layer is not None:
        layer.putalpha(layer.split()[3].point(lambda value: int(value * opacity)))
        canvas.paste(layer, (0, 0), layer)


def _raster_line(canvas, item, scale, dpi):
    """A line's shaft, dashes and fade included.

    Pillow has none of the three things this needs: no dash array, no gradient,
    no alpha on a draw call. So it walks `strokegeom.shaft_render_plan`, which
    is the same list `_pdf_line` walks when it has a fade -- one plan, two
    backends, and the dashes land in the same places in both files.

    ONE RGBA layer per annotation, never one per piece. A faded 200 mm line at
    600 dpi is a few hundred sub-segments, and a full-page layer for each of
    them would be a gigabyte of allocation for one arrow. Each piece writes its
    own alpha straight into that layer and the layer is composited once, which
    also means abutting pieces replace rather than blend -- a fade should not be
    twice as dark where two of its steps meet.

    There is no arrowhead code here either, and there never was: PNG and TIFF
    exported every arrow in every figure as a plain headless line. Heads now
    arrive as `path` instructions like everything else, which is what fixes it.
    """
    from PIL import Image, ImageColor, ImageDraw

    stroke = item.get("stroke")
    if not stroke:
        return
    p1 = (item["x"] * scale, item["y"] * scale)
    p2 = ((item["x"] + item["w"]) * scale, (item["y"] + item["h"]) * scale)
    width = max(1, int(round(item.get("line_width_pt", 0.75) / 72.0 * dpi)))
    dash = item.get("dash_pt")
    # The pattern is in points because the pen is; the plan works in whatever
    # units its endpoints are in, which here is device pixels.
    dash_px = [value / 72.0 * dpi for value in dash] if dash else None
    fade = item.get("fade")
    opacity = item.get("opacity", 1.0)

    plan = strokegeom.shaft_render_plan(p1, p2, dash_px, fade)
    if not plan:
        return

    translucent = bool(fade) or opacity < 0.999
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0)) if translucent else None
    draw = ImageDraw.Draw(canvas if layer is None else layer)
    rgb = ImageColor.getrgb(stroke)[:3] if translucent else None

    for start, end, alpha, is_dot in plan:
        colour = stroke if layer is None else (
            rgb + (max(0, min(255, int(round(alpha * opacity * 255)))),))
        if is_dot:
            # Pillow has no line caps, so a dot has to be drawn as one. A
            # zero-length line is a single pixel however wide the pen is.
            radius = width / 2.0
            draw.ellipse([start[0] - radius, start[1] - radius,
                          start[0] + radius, start[1] + radius], fill=colour)
        else:
            draw.line([start, end], fill=colour, width=width)

    if layer is not None:
        canvas.paste(layer, (0, 0), layer)


def _safe_stem(title):
    """A filename from a figure's title, or "" if nothing survives.

    Whitelisted rather than escaped: this becomes a path, and a title is
    whatever the user typed.
    """
    kept = [c if (c.isalnum() or c in " -_") else " " for c in (title or "")]
    return "_".join("".join(kept).split())[:60]
