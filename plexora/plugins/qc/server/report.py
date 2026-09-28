"""The QC report: what was checked, what was found, and what it cost the data.

Built from the plugin store (so it works long after the session folder is
swept, and after a licence lapses), with every percentage stated against its
denominator:

- the whole tissue with every QC region overlaid (excluded filled, warned
  dashed, one colour per class);
- the numbers: image area, tissue area and how it was estimated, the union of
  the excluded regions on tissue (overlaps counted once), cells excluded of
  the cells of this image, warnings kept apart;
- per channel: clean channels as one line each, flagged ones with their
  regions drawn on the channel;
- every region: class, action, scope, severity, confidence, area, who made it
  (the user's edits included);
- the cell modules: cutoffs, how many cells each reason fails;
- the provenance: detectors and versions, the agent, the strictness, and
  what the detectors found but the session did not pursue.

HTML (images embedded) and an A4 PDF, under `<agent_root>/reports/`.
"""

from __future__ import annotations

import base64
import html
import io
import json

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import results, roi_link, schemas

MAX_EMBEDDED = 40
OVERVIEW_PX = 900
CHANNEL_PX = 420


def _root():
    from plexora import paths

    folder = paths.agent_root() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _scan_for(result):
    from plexora.plugins.qc.server import scan as scanmod

    fp = result.get("scan_fingerprint")
    if not fp:
        return None
    return scanmod._MEMORY.get((result["project"], fp)) or scanmod.load(result["project"], fp)


def affected_area(scan, regions, image_size):
    """{union_px, on_tissue_px, tissue_px, tissue_fraction, method} of the
    regions' union, rasterised on the scan's overview tissue mask."""
    import cv2
    from shapely.geometry import MultiPolygon, shape
    from shapely.ops import unary_union

    width, height = image_size
    shapes = [shape(r["geometry"]).buffer(0) for r in regions if r.get("geometry")]
    union = unary_union(shapes) if shapes else None
    union_px = float(union.area) if union is not None else 0.0
    out = {"union_px": union_px, "image_px": float(width * height),
           "image_fraction": union_px / max(1.0, float(width * height))}
    tissue = scan.shared("tissue_overview") if scan is not None else None
    if tissue is None or union is None:
        out.update(on_tissue_px=None, tissue_px=None, tissue_fraction=None,
                   method=(scan.meta.get("tissue") or {}).get("method") if scan else None)
        return out
    h, w = tissue.shape
    sx, sy = w / max(1, width), h / max(1, height)
    canvas = np.zeros((h, w), dtype=np.uint8)
    polygons = union.geoms if isinstance(union, MultiPolygon) else [union]
    for polygon in polygons:
        if polygon.is_empty or not hasattr(polygon, "exterior"):
            continue
        ring = np.asarray(polygon.exterior.coords) * [sx, sy]
        cv2.fillPoly(canvas, [np.round(ring).astype(np.int32).reshape(-1, 1, 2)], 1)
        for hole in polygon.interiors:
            ring = np.asarray(hole.coords) * [sx, sy]
            cv2.fillPoly(canvas, [np.round(ring).astype(np.int32).reshape(-1, 1, 2)], 0)
    on = tissue.astype(bool)
    per_px = (width * height) / max(1, h * w)
    tissue_px = float(on.sum()) * per_px
    on_tissue = float((on & canvas.astype(bool)).sum()) * per_px
    out.update(on_tissue_px=on_tissue, tissue_px=tissue_px,
               tissue_fraction=on_tissue / tissue_px if tissue_px else None,
               method=(scan.meta.get("tissue") or {}).get("method"),
               overview_level=scan.meta.get("overview_level"))
    return out


def build(call, project, result) -> dict:
    from plexora.server.utils import pixel_scale

    session = call.session
    record = session.project(project)
    ds = session.image_data(project)
    document = results.load(project)
    roi_link.sync(ds, document, save=False)
    result = results.get_result(project, document, result["result_id"]) or result
    width, height = record.image.width or 0, record.image.height or 0
    pixel = pixel_scale.pixel_size(record)
    regions = roi_link.live_regions(ds, result)
    candidates = result.get("candidates") or {}
    rows = []
    for index, region in enumerate(regions, start=1):
        candidate = candidates.get(region["candidate_id"]) or {}
        decision = candidate.get("ai_decision") or {}
        from shapely.geometry import shape

        area = float(shape(region["geometry"]).area)
        rows.append({"label": f"r{index}", "roi_id": region["roi_id"], "class": region["class"],
                     "action": region["action"], "channels": region["channels"],
                     "scope": candidate.get("scope"), "severity": decision.get("severity"),
                     "confidence": decision.get("confidence"), "area_px": area,
                     "area_um2": area * pixel["value"] ** 2 if pixel else None,
                     "created_by": candidate.get("created_by") or "agent",
                     "user": {k: v for k, v in (candidate.get("user_state") or {}).items()
                              if v},
                     "detector": candidate.get("detector"), "geometry": region["geometry"]})
    scan = _scan_for(result)
    excluded = [r for r in rows if r["action"] == "exclude"]
    warned = [r for r in rows if r["action"] == "warn"]
    area = affected_area(scan, excluded, (width, height))
    warn_area = affected_area(scan, warned, (width, height))
    cells = result.get("cells") or {}
    tissue_meta = result.get("tissue") or (scan.meta.get("tissue") if scan else {}) or {}
    denominators = {
        "image_px": width * height, "image_um2": width * height * pixel["value"] ** 2
        if pixel else None,
        "tissue_px": area.get("tissue_px") or tissue_meta.get("area_px"),
        "tissue_um2": tissue_meta.get("area_um2"),
        "tissue_method": tissue_meta.get("method"), "tissue_channel": tissue_meta.get("channel"),
        "pixel": pixel,
        "excluded_on_tissue_px": area.get("on_tissue_px"),
        "excluded_tissue_fraction": area.get("tissue_fraction"),
        "excluded_image_fraction": area.get("image_fraction"),
        "warned_tissue_fraction": warn_area.get("tissue_fraction"),
        "cells": cells.get("n"), "cells_excluded": cells.get("n_fail"),
        "cells_warned": cells.get("n_warn"),
        "cells_excluded_fraction": (cells.get("n_fail") or 0) / cells["n"]
        if cells.get("n") else None,
        "sentence": _denominator_sentence(tissue_meta, area, pixel, cells, scan)}
    return {"project": project, "result": {k: result.get(k) for k in (
        "result_id", "session_id", "created_at", "finished_at", "strictness",
        "detector_versions", "scan_version", "scan_fingerprint", "cycles_method", "agent",
        "software_version", "mode", "origin", "rolled_back")},
        "channels": result.get("channels") or [], "regions": rows,
        "denominators": denominators, "cells": cells,
        "modules": (cells.get("modules") or {}), "residual": result.get("residual") or [],
        "dismissed": result.get("dismissed") or [], "warnings": result.get("warnings") or [],
        "final_review": result.get("final_review"), "summary": results.summary(result),
        "_scan": scan, "_pixel": pixel}


def _denominator_sentence(tissue, area, pixel, cells, scan):
    method = {"nuclear_otsu": f"the {tissue.get('channel')} channel's overview (smoothed, "
                              "Otsu, small holes filled)",
              "max_projection_otsu": "the maximum of every channel's overview (Otsu)",
              "brightfield_otsu": "the brightfield image's darkness (Otsu)",
              "all_pixels": "the whole image (no tissue could be told from glass)"}.get(
        tissue.get("method"), "an estimate")
    level = scan.meta.get("overview_level") if scan else None
    parts = [f"Tissue is estimated from {method}" + (f" at 1/{2 ** level} resolution"
                                                     if level else "") + "."]
    if area.get("tissue_fraction") is not None:
        parts.append(f"Excluded tissue = area of the union of excluded regions on tissue / "
                     f"tissue area = {area['tissue_fraction'] * 100:.2f} %; overlapping "
                     "regions are counted once; warned regions are not included.")
    if pixel:
        parts.append(f"Areas in µm² use {pixel['value']:.4g} µm/px ({pixel.get('source')}).")
    else:
        parts.append("The image states no pixel size: areas are in pixels.")
    if cells.get("n"):
        parts.append(f"Excluded cells = cells failing any excluding reason / all "
                     f"{cells['n']} cells of this image = "
                     f"{(cells.get('n_fail') or 0) / cells['n'] * 100:.2f} %.")
    return " ".join(parts)


# -- pictures ----------------------------------------------------------------------------


def _png(picture):
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _overview(call, project, report, channel=None, regions=None, size=OVERVIEW_PX):
    from plexora.agent.evidence import calibration
    from plexora.plugins.qc.server import sheets

    scan = report["_scan"]
    if scan is not None:
        bounds = sheets.tissue_box(scan)
        nuclear = scan.nuclear()
        names = [c["name"] for c in scan.channels]
    else:
        record = call.session.project(project)
        bounds = {"x": 0, "y": 0, "width": record.image.width, "height": record.image.height}
        names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
        from plexora.agent import presets

        nuclear = presets.nuclear_channel(names)
    try:
        record = calibration.current(call.session, project, names)
    except AgentError:
        record = None
    name = channel or nuclear or (names[0] if names else None)
    if name is None:
        return None
    shapes = []
    for region in regions if regions is not None else report["regions"]:
        color = schemas.CLASS_COLORS.get(region["class"], "#ff3df2")
        exclude = region["action"] == "exclude"
        shapes.append(sheets._shape(region["label"], region["geometry"], color=color, width=2,
                                    dash=not exclude, fill_alpha=0.3 if exclude else 0.0,
                                    label=region["label"]))
    picture, _manifest = sheets._render(call.session, project, bounds,
                                        [sheets._channel(name, sheets.CHANNEL_COLOR, record)],
                                        size, pixel=report["_pixel"], shapes=shapes)
    return _png(picture)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# -- HTML --------------------------------------------------------------------------------


def _b64(png):
    return base64.b64encode(png).decode("ascii") if png else ""


def _n(value, digits=4):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _pct(value):
    return "-" if value is None else f"{value * 100:.2f} %"


def to_html(call, report) -> str:
    esc = html.escape
    r = report["result"]
    d = report["denominators"]
    embedded = 0

    def image(png, alt):
        nonlocal embedded
        if not png or embedded >= MAX_EMBEDDED:
            return ""
        embedded += 1
        return f'<img alt="{esc(alt)}" src="data:image/png;base64,{_b64(png)}">'

    overview = _safe(_overview, call, report["project"], report)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>QC report - {esc(report['project'])}</title>",
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1d2126}"
        "h1{font-size:22px}h2{font-size:17px;margin-top:28px}table{border-collapse:collapse;"
        "margin:8px 0}td,th{border:1px solid #d6dadf;padding:3px 7px;text-align:left;"
        "vertical-align:top;font-size:13px}th{background:#f1f3f5}.muted{color:#5b636d}"
        ".sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}"
        "img{max-width:100%;border:1px solid #d6dadf}</style></head><body>",
        f"<h1>Quality control: {esc(report['project'])}</h1>",
        f"<p class='muted'>result {esc(str(r.get('result_id')))} · session "
        f"{esc(str(r.get('session_id') or 'manual'))} · strictness "
        f"{esc(str((r.get('strictness') or {}).get('preset') or 'standard'))} · finished "
        f"{esc(str(r.get('finished_at') or '-'))}</p>",
        "<h2>Summary</h2><table>",
        f"<tr><th>Regions excluded</th><td>{sum(1 for x in report['regions'] if x['action'] == 'exclude')}</td></tr>",
        f"<tr><th>Regions warned</th><td>{sum(1 for x in report['regions'] if x['action'] == 'warn')}</td></tr>",
        f"<tr><th>Tissue excluded</th><td>{_pct(d['excluded_tissue_fraction'])}</td></tr>",
        f"<tr><th>Tissue warned (not excluded)</th><td>{_pct(d['warned_tissue_fraction'])}</td></tr>",
        f"<tr><th>Cells excluded</th><td>{_n(d['cells_excluded'])} of {_n(d['cells'])} "
        f"({_pct(d['cells_excluded_fraction'])})</td></tr>",
        f"<tr><th>Cells warned</th><td>{_n(d['cells_warned'])}</td></tr>",
        "</table>", f"<p class='muted'>{esc(d['sentence'])}</p>",
    ]
    if overview:
        parts.append("<h2>Every QC region</h2>" + image(overview, "QC regions on the tissue"))
        legend = sorted({x["class"] for x in report["regions"]})
        parts.append("<p>" + " ".join(
            f"<span class='sw' style='background:{schemas.CLASS_COLORS.get(k, '#999')}'></span>"
            f"{esc(schemas.CLASS_WORDS.get(k, k))}" for k in legend) +
            "</p><p class='muted'>filled: excluded · dashed: warned</p>")
    parts.append("<h2>Regions</h2><table><tr><th>#</th><th>class</th><th>action</th>"
                 "<th>channels</th><th>scope</th><th>severity</th><th>confidence</th>"
                 "<th>area</th><th>made by</th></tr>")
    for x in report["regions"]:
        area = f"{x['area_um2'] / 1e6:.4g} mm²" if x.get("area_um2") else \
            f"{x['area_px']:.0f} px²"
        made = x["created_by"] + (" (edited)" if x["user"].get("edited") else "") + \
            (" (approved)" if x["user"].get("approved") else "")
        parts.append(f"<tr><td>{x['label']}</td><td>{esc(schemas.CLASS_WORDS.get(x['class'], x['class']))}"
                     f"</td><td>{esc(x['action'])}</td><td>{esc(', '.join(x['channels'][:6]))}</td>"
                     f"<td>{esc(str(x.get('scope') or '-'))}</td><td>{esc(str(x.get('severity') or '-'))}</td>"
                     f"<td>{esc(str(x.get('confidence') or '-'))}</td><td>{area}</td>"
                     f"<td>{esc(made)}</td></tr>")
    parts.append("</table>")
    parts.append("<h2>Channels</h2><table><tr><th>channel</th><th>cycle</th><th>status</th>"
                 "<th>flags</th><th>regions</th></tr>")
    by_channel = {}
    for x in report["regions"]:
        for name in x["channels"]:
            by_channel.setdefault(name, []).append(x)
    flagged = []
    for channel in report["channels"]:
        mine = by_channel.get(channel["name"], [])
        if mine:
            flagged.append((channel, mine))
        parts.append(f"<tr><td>{esc(channel['name'])}</td><td>{_n(channel.get('cycle'))}</td>"
                     f"<td>{esc(str(channel.get('status')))}</td>"
                     f"<td>{esc(', '.join(channel.get('flags') or []))}</td>"
                     f"<td>{', '.join(x['label'] for x in mine) or '-'}</td></tr>")
    parts.append("</table>")
    for channel, mine in flagged[:12]:
        png = _safe(_overview, call, report["project"], report, channel=channel["name"],
                    regions=mine, size=CHANNEL_PX)
        parts.append(f"<h3>{esc(channel['name'])}</h3>" + image(png, channel["name"]))
    cells = report["cells"]
    if cells.get("n"):
        parts.append("<h2>Cells</h2><table><tr><th>reason</th><th>cells excluded</th>"
                     "<th>cells warned</th><th>meaning</th></tr>")
        reasons = sorted(set(cells.get("by_reason") or {}) | set(cells.get("warn_by_reason")
                                                                 or {}))
        for reason in reasons:
            parts.append(f"<tr><td>{esc(reason)}</td><td>{_n((cells.get('by_reason') or {}).get(reason))}"
                         f"</td><td>{_n((cells.get('warn_by_reason') or {}).get(reason))}</td>"
                         f"<td>{esc(schemas.REASON_DEFINITIONS.get(reason, ''))}</td></tr>")
        parts.append("</table><table><tr><th>module</th><th>state</th><th>cutoffs</th>"
                     "<th>agent</th></tr>")
        for name, entry in report["modules"].items():
            cut = entry.get("cutoffs") or {}
            decision = entry.get("decision") or {}
            said = ", ".join(f"{side}: {(decision.get(side) or {}).get('verdict')}"
                             for side in ("low", "high") if decision.get(side))
            parts.append(f"<tr><td>{esc(name)}</td><td>{esc(str(entry.get('state')))}</td>"
                         f"<td>{_n(cut.get('low'))} .. {_n(cut.get('high'))} "
                         f"({esc(str(cut.get('space') or ''))})</td><td>{esc(said or '-')}</td></tr>")
        parts.append("</table>")
    if report["residual"]:
        parts.append("<h2>Found but not pursued</h2><ul>" + "".join(
            f"<li>{row['n']} further {esc(schemas.CLASS_WORDS.get(row['class'], row['class']))} "
            f"candidate(s) in {esc(row['channel'])}"
            + (f" ({row['area_mm2']} mm²)" if row.get("area_mm2") else "") + "</li>"
            for row in report["residual"]) + "</ul>")
    parts.append("<h2>Provenance</h2><pre>" + esc(json.dumps(
        {k: v for k, v in report["result"].items()}, indent=2, default=str)) + "</pre>")
    parts.append("<p class='muted'>Plexora quality control. QC regions are ROIs in the ROI "
                 "panel; every agent write is receipted and undoable.</p></body></html>")
    return "".join(parts)


# -- PDF ----------------------------------------------------------------------------------


def to_pdf(call, report, path):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as exc:  # pragma: no cover
        raise AgentError("capability_unavailable", "the PDF report needs reportlab") from exc
    esc = html.escape
    r = report["result"]
    d = report["denominators"]
    margin = 12 * mm
    page_w, page_h = landscape(A4)
    frame_w = page_w - 2 * margin
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=8.5, leading=11)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5)
    muted = ParagraphStyle("muted", parent=small, textColor=colors.HexColor("#555b63"))
    title = ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=15,
                           leading=19, spaceAfter=4)
    heading = ParagraphStyle("heading", parent=title, fontSize=12, leading=15)

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColorRGB(0.45, 0.45, 0.45)
        canvas.drawString(margin, 8 * mm, f"Plexora quality control · result "
                                          f"{r.get('result_id')} · page {canvas.getPageNumber()}")
        canvas.restoreState()

    def picture(png, max_w, max_h):
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(png)) as probe:
            iw, ih = probe.size
        scale = min(max_w / iw, max_h / ih)
        return Image(io.BytesIO(png), width=iw * scale, height=ih * scale)

    def grid(rows, widths):
        table = Table([[Paragraph(esc(str(v)), small) for v in row] for row in rows],
                      colWidths=[w * mm for w in widths], repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d4da")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f3f5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return table

    story = [Paragraph(esc(f"Quality control: {report['project']}"), title),
             Paragraph(esc(f"result {r.get('result_id')} · session {r.get('session_id') or 'manual'}"
                           f" · strictness {(r.get('strictness') or {}).get('preset')}"), muted),
             Spacer(1, 3 * mm),
             grid([["measure", "value"],
                   ["tissue excluded", _pct(d["excluded_tissue_fraction"])],
                   ["tissue warned", _pct(d["warned_tissue_fraction"])],
                   ["cells excluded", f"{_n(d['cells_excluded'])} of {_n(d['cells'])} "
                                      f"({_pct(d['cells_excluded_fraction'])})"],
                   ["cells warned", _n(d["cells_warned"])]], [60, 80]),
             Spacer(1, 2 * mm), Paragraph(esc(d["sentence"]), muted), Spacer(1, 3 * mm)]
    overview = _safe(_overview, call, report["project"], report)
    if overview:
        story.append(picture(overview, frame_w * 0.6, (page_h - 2 * margin) * 0.55))
    story.append(PageBreak())
    story.append(Paragraph("Regions", heading))
    rows = [["#", "class", "action", "channels", "severity", "confidence", "made by"]]
    for x in report["regions"]:
        rows.append([x["label"], schemas.CLASS_WORDS.get(x["class"], x["class"]), x["action"],
                     ", ".join(x["channels"][:6]), x.get("severity") or "-",
                     x.get("confidence") or "-", x["created_by"]])
    story.append(grid(rows, [12, 50, 20, 80, 22, 24, 30]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Channels", heading))
    rows = [["channel", "cycle", "status", "flags"]]
    for channel in report["channels"]:
        rows.append([channel["name"], _n(channel.get("cycle")), channel.get("status"),
                     ", ".join(channel.get("flags") or [])])
    story.append(grid(rows, [50, 16, 40, 100]))
    cells = report["cells"]
    if cells.get("n"):
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Cells", heading))
        rows = [["reason", "excluded", "warned"]]
        for reason in sorted(set(cells.get("by_reason") or {})
                             | set(cells.get("warn_by_reason") or {})):
            rows.append([reason, _n((cells.get("by_reason") or {}).get(reason)),
                         _n((cells.get("warn_by_reason") or {}).get(reason))])
        story.append(grid(rows, [80, 30, 30]))
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=margin,
                            rightMargin=margin, topMargin=margin, bottomMargin=margin + 4 * mm)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return str(path)


def write_report(call, *, session_id=None, project=None, fmt="both") -> dict:
    if session_id:
        from plexora.plugins.qc.server.engine import store

        record = store().load(session_id)
        project = record["images"][0]
        document = results.load(project)
        result = results.get_result(project, document, record["result_id"])
    elif project:
        document = results.load(project)
        result = results.active(document)
    else:
        raise AgentError("invalid_input", "give session_id or project")
    if result is None:
        raise AgentError("precondition_missing", "no QC result to report on")
    report = build(call, project, result)
    out = {"project": project, "result_id": result["result_id"],
           "summary": report["summary"], "denominators": report["denominators"]}
    base = _root() / f"qc_{result['result_id']}"
    if fmt in ("html", "both"):
        path = base.with_suffix(".html")
        path.write_text(to_html(call, report), encoding="utf-8")
        out["html"] = str(path)
    if fmt in ("pdf", "both"):
        out["pdf"] = to_pdf(call, report, base.with_suffix(".pdf"))
    with results.lock(project):
        document = results.load(project)
        stored = results.get_result(project, document, result["result_id"])
        if stored is not None and (document.get("results") or {}).get(result["result_id"]):
            stored["report_paths"] = {k: out[k] for k in ("html", "pdf") if k in out}
            results.put_result(document, stored)
            results.save(project, document)
    return out
