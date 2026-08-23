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

`reportlab` is imported lazily and is an optional dependency. A build without it
can still export PNG and TIFF, and asking for a PDF says what to install rather
than failing with an ImportError from three frames down.
"""

from __future__ import annotations

import json
from pathlib import Path

from plexora.plugins.figure_builder.server import compose, provenance, render, repository

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
            _write_pdf(document, rendered_pages, path, dpi, report_lines,
                       include_provenance=options.get("provenance", True))
            written = [path]
        else:
            written = _write_rasters(document, rendered_pages, out_dir, stem, fmt, dpi)

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
        "warnings": _warnings(results),
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


def _warnings(results):
    """The whole export's caveats, deduplicated and phrased for a person."""
    out = []
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
            "PDF export needs reportlab. Install it with:  pip install 'plexora[figures]'"
        ) from exc

    style = document["settings"]["style"]
    pdf = pdf_canvas.Canvas(str(path))
    pdf.setTitle(document["title"])
    pdf.setSubject(f"Plexora figure {document['figure_id']} revision {document['revision']}")

    for entry in pages:
        page = entry["page"]
        width_mm, height_mm = page["size_mm"]["w"], page["size_mm"]["h"]
        pdf.setPageSize((width_mm * mm, height_mm * mm))
        _fill(pdf, page["background"], 0, 0, width_mm, height_mm, height_mm, mm)

        for item in compose.page_instructions(document, page, entry["panels"], entry["annotations"]):
            _draw_pdf(pdf, item, entry["images"], height_mm, style, mm, ImageReader)
        pdf.showPage()

    if include_provenance:
        _provenance_page(pdf, report_lines, document, mm)
    pdf.save()


def _fill(pdf, colour, x, y, width, height, page_height, mm):
    if not colour:
        return
    pdf.setFillColor(colour)
    pdf.rect(x * mm, (page_height - y - height) * mm, width * mm, height * mm,
             stroke=0, fill=1)


def _draw_pdf(pdf, item, images, page_height, style, mm, ImageReader):
    # PDF's origin is bottom-left and the document's is top-left. Converted
    # here, once, so nothing upstream has to hold two coordinate systems.
    def top(y, height=0.0):
        return (page_height - y - height) * mm

    kind = item["kind"]
    if kind == "panel":
        image = images.get(item["panel_id"])
        if image is not None:
            pdf.drawImage(ImageReader(image), item["x"] * mm, top(item["y"], item["h"]),
                          item["w"] * mm, item["h"] * mm, mask=None)
        return

    if kind == "text":
        pdf.setFillColor(item.get("color") or "#000000")
        font = style["font_family"] if item.get("weight") != "bold" else "Helvetica-Bold"
        try:
            pdf.setFont(font, item["size_pt"])
        except Exception:
            # A font the PDF library does not have is a style problem, not a
            # reason to lose the export.
            pdf.setFont("Helvetica", item["size_pt"])
        # The instruction's y is the TOP of the text; PDF places from the
        # baseline, so it moves down by roughly the cap height.
        baseline = top(item["y"] + item["size_pt"] * 25.4 / 72.0 * 0.8)
        align = item.get("align", "left")
        if align == "center":
            pdf.drawCentredString(item["x"] * mm, baseline, item["text"])
        elif align == "right":
            pdf.drawRightString(item["x"] * mm, baseline, item["text"])
        else:
            pdf.drawString(item["x"] * mm, baseline, item["text"])
        return

    if kind == "swatch":
        ramp = item.get("ramp")
        if ramp:
            step = item["w"] / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                pdf.setFillColor(colour)
                pdf.rect((item["x"] + index * step) * mm, top(item["y"], item["h"]),
                         step * mm, item["h"] * mm, stroke=0, fill=1)
        else:
            pdf.setFillColor(item["color"])
            pdf.rect(item["x"] * mm, top(item["y"], item["h"]),
                     item["w"] * mm, item["h"] * mm, stroke=0, fill=1)
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
    elif kind in ("line", "arrow"):
        x1, y1 = item["x"] * mm, top(item["y"])
        x2, y2 = (item["x"] + item["w"]) * mm, top(item["y"] + item["h"])
        pdf.line(x1, y1, x2, y2)
        if kind == "arrow":
            _arrow_head(pdf, x1, y1, x2, y2, item.get("line_width_pt", 0.75))


def _arrow_head(pdf, x1, y1, x2, y2, line_width):
    import math

    angle = math.atan2(y2 - y1, x2 - x1)
    size = max(3.0, line_width * 4)
    for direction in (-1, 1):
        spread = angle + direction * math.radians(160)
        pdf.line(x2, y2, x2 + size * math.cos(spread), y2 + size * math.sin(spread))


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

    written = []
    for index, entry in enumerate(pages):
        page = entry["page"]
        scale = dpi / compose.MM_PER_INCH
        width = max(1, int(round(page["size_mm"]["w"] * scale)))
        height = max(1, int(round(page["size_mm"]["h"] * scale)))
        canvas = Image.new("RGB", (width, height), page["background"])
        draw = ImageDraw.Draw(canvas)

        for item in compose.page_instructions(document, page, entry["panels"], entry["annotations"]):
            _draw_raster(canvas, draw, item, entry["images"], scale, dpi)

        suffix = "tif" if fmt == "tiff" else fmt
        name = f"{stem}.{suffix}" if len(pages) == 1 else f"{stem}-{index + 1}.{suffix}"
        path = out_dir / name
        canvas.save(path, dpi=(dpi, dpi))
        written.append(path)
    return written


def _draw_raster(canvas, draw, item, images, scale, dpi):
    from PIL import ImageFont

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
        size = max(6, int(round(item["size_pt"] / 72.0 * dpi)))
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - very old Pillow
            font = ImageFont.load_default()
        anchor = {"left": "la", "center": "ma", "right": "ra"}[item.get("align", "left")]
        draw.text((item["x"] * scale, item["y"] * scale), item["text"],
                  fill=item.get("color") or "#000000", font=font, anchor=anchor)
        return
    if kind == "swatch":
        ramp = item.get("ramp")
        if ramp:
            step = item["w"] * scale / max(1, len(ramp))
            for index, colour in enumerate(ramp):
                left = item["x"] * scale + index * step
                draw.rectangle([left, item["y"] * scale, left + step,
                                (item["y"] + item["h"]) * scale], fill=colour)
        else:
            draw.rectangle(box(item), fill=item["color"])
        return
    width = max(1, int(round(item.get("line_width_pt", 0.75) / 72.0 * dpi)))
    if kind == "rect":
        draw.rectangle(box(item), fill=item.get("fill"), outline=item.get("stroke"), width=width)
    elif kind == "ellipse":
        draw.ellipse(box(item), fill=item.get("fill"), outline=item.get("stroke"), width=width)
    elif kind in ("line", "arrow"):
        draw.line(box(item), fill=item.get("stroke"), width=width)


def _safe_stem(title):
    """A filename from a figure's title, or "" if nothing survives.

    Whitelisted rather than escaped: this becomes a path, and a title is
    whatever the user typed.
    """
    kept = [c if (c.isalnum() or c in " -_") else " " for c in (title or "")]
    return "_".join("".join(kept).split())[:60]
