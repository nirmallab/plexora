"""The QC report: what was checked, what was found, and what it cost the data.

Built from the plugin store (so it works long after the session folder is
swept, and after a licence lapses), with every percentage stated against its
denominator:

- the whole tissue with every QC region overlaid (excluded filled, warned
  dashed, one colour per class);
- the numbers: image area, tissue area and how it was estimated, the union of
  the excluded regions on tissue (overlaps counted once), cells excluded of
  the cells of this image, warnings kept apart;
- per channel: the channel audit's verdict, the class it named and why --
  a staining or signal problem is judged per channel and never outlined, so
  this table IS the staining output -- and flagged channels with their
  regions drawn on the channel;
- every region: class, action, scope, severity, confidence, area, who made it
  (the user's edits included);
- the cells: how many each reason fails, warns or only notes (the cells
  outside the tissue, annotated and kept; labels with no nucleus); how many
  cells have each marker flagged (counts only); and the channel-scoped
  regions the cells' own values did not bear out;
- DNA retention by cycle: until which cycle the nuclei are still there
  ("reliable through cycle N"), and the markers of each cycle it flags;
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
from plexora.plugins.qc.server import checks_result, provenance, results, roi_link, schemas

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


def _envelope_area(candidate):
    from plexora.plugins.qc.server import polygons

    envelope = candidate.get("envelope_geometry")
    return polygons.area_of(envelope) if envelope else None


def _area_cell(region, pixel_um):
    """A region's area, and how its outline was made: "0.04 mm² (traced, 33 %
    of a 0.12 mm² envelope)" or "0.12 mm² (envelope: <why>)"."""
    def words(px):
        if px is None:
            return "-"
        return f"{px * pixel_um * pixel_um / 1e6:.4g} mm²" if pixel_um else f"{px:.0f} px²"

    text = words(region["area_px"])
    refinement = region.get("refinement") or {}
    status = refinement.get("status")
    if status == "refined" and region.get("envelope_px"):
        share = region["area_px"] / region["envelope_px"]
        return f"{text} (traced, {100 * share:.0f} % of a {words(region['envelope_px'])} " \
               "envelope)"
    if status:
        return f"{text} (envelope: {refinement.get('reason') or status})"
    return text


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
                     "category": region.get("category")
                     or schemas.category_of_class(region["class"]),
                     "threshold": (candidate.get("metrics") or {}).get("threshold"),
                     "threshold_source": (candidate.get("metrics") or {}).get(
                         "threshold_source"),
                     "action": region["action"], "channels": region["channels"],
                     "scope": candidate.get("scope"), "severity": decision.get("severity"),
                     "confidence": decision.get("confidence"), "area_px": area,
                     "area_um2": area * pixel["value"] ** 2 if pixel else None,
                     "created_by": candidate.get("created_by") or "agent",
                     "user": {k: v for k, v in (candidate.get("user_state") or {}).items()
                              if v},
                     "detector": candidate.get("detector"), "geometry": region["geometry"],
                     "refinement": candidate.get("refinement"),
                     "envelope_px": _envelope_area(candidate),
                     "origin": candidate.get("origin"),
                     "method": candidate.get("method"),
                     "notes": provenance.notes_text(candidate.get("notes")),
                     "evidence_artifacts": list(candidate.get("evidence_artifacts") or []),
                     "overlaps": candidate.get("overlaps") or []})
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
        "cells_marker_flagged": cells.get("n_marker_flagged"),
        "cells_noted": cells.get("n_noted"),
        "cells_background": cells.get("n_background"),
        "cells_no_nucleus": cells.get("n_no_nucleus"),
        "cells_excluded_fraction": (cells.get("n_fail") or 0) / cells["n"]
        if cells.get("n") else None,
        "sentence": _denominator_sentence(tissue_meta, area, pixel, cells, scan)}
    return {"project": project, "result": {k: result.get(k) for k in (
        "result_id", "session_id", "created_at", "finished_at", "strictness",
        "detector_versions", "scan_version", "scan_fingerprint", "cycles_method", "agent",
        "software_version", "mode", "origin", "rolled_back")},
        "channels": result.get("channels") or [], "regions": rows,
        "checks": result.get("checks") or {},
        "planning_notes": result.get("planning_notes") or [],
        "registration": checks_result.registration_table(result),
        "dna_retention": _retention_for(project, result),
        "denominators": denominators, "cells": cells,
        "residual": result.get("residual") or [],
        "dismissed": result.get("dismissed") or [], "warnings": result.get("warnings") or [],
        "final_review": result.get("final_review"), "summary": results.summary(result),
        "_scan": scan, "_pixel": pixel}


def _retention_for(project, result):
    """The DNA retention summary the result's cells were derived with: the
    session's own (`result["dna_retention"]`), else the stored one the cell
    calls name (`cells["dna_flags"]`), else None."""
    found = result.get("dna_retention")
    if found:
        return found
    used = (result.get("cells") or {}).get("dna_flags") or {}
    if used.get("state") != "current" or not used.get("fingerprint"):
        return None
    from plexora.plugins.qc.server import dna_retention

    try:
        return dna_retention.load_summary(project, used["fingerprint"])
    except Exception:  # an unreadable store: no section
        return None


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
        color = schemas.category_color(region.get("category")
                                       or schemas.category_of_class(region["class"]))
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
        f"<tr><th>Cells with a marker flagged</th><td>{_n(d.get('cells_marker_flagged'))} "
        "(the cell is kept; that marker's value is not to be read)</td></tr>",
        f"<tr><th>Cells outside the tissue (annotated, kept)</th>"
        f"<td>{_n(d.get('cells_background'))}</td></tr>",
        "</table>", f"<p class='muted'>{esc(d['sentence'])}</p>",
    ]
    if overview:
        parts.append("<h2>Every QC region</h2>" + image(overview, "QC regions on the tissue"))
        order = list(schemas.category_order())
        present = {x["category"] for x in report["regions"]}
        legend = [k for k in order if k in present] + sorted(present - set(order))
        parts.append("<p>" + " ".join(
            f"<span class='sw' style='background:{schemas.category_color(k)}'></span>"
            f"{esc(schemas.category_words(k))}" for k in legend) +
            "</p><p class='muted'>filled: excluded · dashed: warned</p>")
    parts.append("<h2>Regions</h2><table><tr><th>#</th><th>category</th><th>subtype</th>"
                 "<th>action</th><th>channels</th><th>scope</th><th>severity</th>"
                 "<th>confidence</th><th>threshold</th><th>area</th><th>made by</th></tr>")
    for x in report["regions"]:
        area = esc(_area_cell(x, (x["area_um2"] / x["area_px"]) ** 0.5
                              if x.get("area_um2") and x.get("area_px") else None))
        made = x["created_by"] + (" (edited)" if x["user"].get("edited") else "") + \
            (" (approved)" if x["user"].get("approved") else "")
        if x.get("origin") == "agent":
            made = esc(made.replace("agent", "agent, drawn with magic select", 1))
            if x.get("notes"):
                made += f"<br><span class='muted'>{esc(x['notes'])}</span>"
        else:
            made = esc(made)
        bar = f"{_n(x.get('threshold'))} ({x.get('threshold_source') or 'auto'})" \
            if x.get("threshold") is not None else "-"
        parts.append(f"<tr><td>{x['label']}</td><td>{esc(schemas.category_words(x['category']))}"
                     f"</td><td>{esc(schemas.CLASS_WORDS.get(x['class'], x['class']))}"
                     f"</td><td>{esc(x['action'])}</td><td>{esc(', '.join(x['channels'][:6]))}</td>"
                     f"<td>{esc(str(x.get('scope') or '-'))}</td><td>{esc(str(x.get('severity') or '-'))}</td>"
                     f"<td>{esc(str(x.get('confidence') or '-'))}</td><td>{esc(bar)}</td>"
                     f"<td>{area}</td><td>{made}</td></tr>")
    parts.append("</table>")
    parts.extend(_checks_html(report, esc))
    parts.extend(_registration_html(report, esc))
    parts.append("<h2>Channels</h2><p class='muted'>Staining and signal problems are judged "
                 "per channel by the channel audit and never outlined: its verdict, the "
                 "class it named and why are this table's.</p><table><tr><th>channel</th>"
                 "<th>cycle</th><th>status</th><th>audit verdict</th><th>class</th>"
                 "<th>reason</th><th>flags</th><th>regions</th></tr>")
    by_channel = {}
    for x in report["regions"]:
        for name in x["channels"]:
            by_channel.setdefault(name, []).append(x)
    flagged = []
    for channel in report["channels"]:
        mine = by_channel.get(channel["name"], [])
        if mine:
            flagged.append((channel, mine))
        verdict, klass, why = _audit_words(channel)
        parts.append(f"<tr><td>{esc(channel['name'])}</td><td>{_n(channel.get('cycle'))}</td>"
                     f"<td>{esc(str(channel.get('status')))}</td><td>{esc(verdict)}</td>"
                     f"<td>{esc(klass)}</td><td>{esc(why)}</td>"
                     f"<td>{esc(', '.join(channel.get('flags') or []))}</td>"
                     f"<td>{', '.join(x['label'] for x in mine) or '-'}</td></tr>")
    parts.append("</table>")
    for channel, mine in flagged[:12]:
        png = _safe(_overview, call, report["project"], report, channel=channel["name"],
                    regions=mine, size=CHANNEL_PX)
        parts.append(f"<h3>{esc(channel['name'])}</h3>" + image(png, channel["name"]))
    cells = report["cells"]
    if cells.get("n"):
        parts.append("<h2>Cells</h2><p class='muted'>Every cell is kept in every export; a "
                     "noted reason records the cell and changes nothing.</p><table><tr>"
                     "<th>reason</th><th>cells excluded</th><th>cells warned</th>"
                     "<th>cells noted</th><th>meaning</th></tr>")
        for reason in _cell_reasons(cells):
            parts.append(f"<tr><td>{esc(_reason_words(reason))}</td>"
                         f"<td>{_n((cells.get('by_reason') or {}).get(reason))}</td>"
                         f"<td>{_n((cells.get('warn_by_reason') or {}).get(reason))}</td>"
                         f"<td>{_n((cells.get('note_by_reason') or {}).get(reason))}</td>"
                         f"<td>{esc(schemas.REASON_DEFINITIONS.get(reason, ''))}</td></tr>")
        parts.append("</table>")
        if cells.get("n_no_nucleus_in_background"):
            parts.append(f"<p class='muted'>{cells['n_no_nucleus_in_background']} cells with no "
                         "nucleus are also outside the tissue; both reasons are listed.</p>")
        parts.extend(_marker_counts_html(cells, esc))
    parts.extend(_retention_html(report, esc))
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


def _audit_words(channel):
    """(verdict, class words, reason) of a channel's audit, "-" where it
    said nothing."""
    audit = channel.get("audit") or {}
    note = channel.get("audit_note") or {}
    klass = note.get("class") or audit.get("class_hint")
    return (str(audit.get("verdict") or "-"),
            schemas.CLASS_WORDS.get(klass, klass) if klass else "-",
            str(note.get("reason") or channel.get("reason") or "-"))


def _cell_reasons(cells):
    """Every reason that excluded, warned or noted a cell, in the order a
    cell's primary reason is chosen."""
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    found = set(cells.get("by_reason") or {}) | set(cells.get("warn_by_reason") or {}) \
        | set(cells.get("note_by_reason") or {})
    return sorted(found, key=lambda r: (order.get(r, 999), r))


def _reason_words(reason):
    if reason.startswith("region:"):
        klass = reason.split(":", 1)[1]
        return f"In {schemas.CLASS_WORDS.get(klass, klass)}"
    return schemas.REASON_WORDS.get(reason, reason)


def _retention_rows(report):
    """[(cycle, DNA channel, % retained, % retained through, markers, cells
    flagged)] of the DNA retention summary, and its digest line."""
    from plexora.plugins.qc.server import dna_retention

    summary = report.get("dna_retention")
    if not summary or not summary.get("cycles"):
        return [], None
    flagged = {}
    for entry in (report.get("cells") or {}).get("marker_evidence") or []:
        if entry.get("test") == "dna_retention":
            cycle = entry.get("cycle")
            flagged[cycle] = max(flagged.get(cycle, 0), int(entry.get("n_flagged") or 0))
    rows = []
    for cycle in summary["cycles"]:
        index = cycle.get("index")
        rows.append((index, cycle.get("channel") or "-",
                     _pct(cycle.get("fraction_retained")),
                     _pct(cycle.get("cumulative_retained")),
                     ", ".join(cycle.get("markers") or []) or "-",
                     _n(flagged.get(index, 0))))
    return rows, dna_retention.digest_line(summary)


def _retention_html(report, esc):
    rows, digest = _retention_rows(report)
    if not rows:
        return []
    summary = report["dna_retention"]
    out = ["<h2>DNA retention by cycle</h2>",
           f"<p><b>{esc(digest or '')}</b></p>",
           f"<p class='muted'>Each cell's DNA in each cycle against the reference "
           f"({esc(str(summary.get('reference') or '-'))}): retained while at least "
           f"{(summary.get('params') or {}).get('retained_ratio', 0.3):.0%} of it. A cell "
           "that lost its nucleus has every marker of that cycle and later flagged "
           "(dna_loss).</p>",
           "<table><tr><th>cycle</th><th>DNA channel</th><th>% retained</th>"
           "<th>% retained through</th><th>markers in cycle</th><th>cells flagged</th></tr>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{esc(str(v))}</td>" for v in row) + "</tr>")
    out.append("</table>")
    return out


def _registration_html(report, esc):
    """The registration table: one row per DNA channel against the reference."""
    rows = []
    words = {"aligned": "aligned", "shifted": "shifted as a whole (re-registration "
             "can fix it)", "local": "out of register in places", "skipped": "not compared"}
    for row in report.get("registration") or []:
        shift = row.get("global_shift_um")
        nuclei = row.get("nuclei") or {}
        rows.append(
            f"<tr><td>{esc(row['channel'])}</td><td>{esc(str(row.get('reference') or '-'))}</td>"
            f"<td>{esc(words.get(row['status'], row['status']))}"
            f"{' -- ' + esc(row['reason']) if row.get('reason') else ''}</td>"
            f"<td>{_n(shift) + ' µm' if shift is not None else '-'}</td>"
            f"<td>{row['local_regions']} ({nuclei.get('displaced', 0)} nuclei)</td>"
            f"<td>{row['tissue_loss_regions']} ({nuclei.get('lost', 0)} nuclei)</td>"
            f"<td>{esc(', '.join(row.get('markers') or []) or '-')}</td>"
            f"<td>{row.get('cells_affected') or 0}</td></tr>")
    if not rows:
        return []
    return ["<h2>Registration</h2><table><tr><th>DNA channel</th><th>against</th>"
            "<th>verdict</th><th>whole shift</th><th>local places</th><th>tissue lost</th>"
            "<th>markers unreliable</th><th>cells</th></tr>", *rows, "</table>"]


#: How a planning note's status reads in the report.
PLANNING_WORDS = {"not_run": "not run", "widened": "widened",
                  "proposed": "proposed, not written"}


def _checks_html(report, esc):
    """The image checks: what each scored, the bar it was judged at and
    where that bar came from, what its look said, what became of it."""
    rows = []
    for check, per in (report.get("checks") or {}).items():
        for key, entry in per.items():
            if check == "visual":
                found = entry.get("regions") or {}
                fate = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in found.items() if v)
                left = "; ".join(f"{p.get('where')}: {p.get('why')}" for p in
                                 (entry.get("left") or [])[:4] if p.get("where"))
                rows.append(
                    f"<tr><td>{esc(schemas.CHECK_WORDS.get(check, check))}</td>"
                    f"<td>the whole tissue</td><td>-</td><td>-</td>"
                    f"<td>{esc(str(entry.get('status') or entry.get('state') or '-'))}"
                    f"{(': ' + esc(left)) if left else ''}</td>"
                    f"<td>{esc(fate or str(entry.get('reason') or '-'))}</td></tr>")
                continue
            verdicts = (entry.get("strata_verdicts") or [])[-1:] or [{}]
            said = ", ".join(f"{k}: {v}" for k, v in (verdicts[0].get("strata") or {}).items())
            regions = entry.get("regions") or {}
            fate = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in regions.items() if v)
            share = entry.get("flagged_pct")
            if share is None:
                share = (entry.get("at_auto") or {}).get("flagged_pct")
            rows.append(
                f"<tr><td>{esc(schemas.CHECK_WORDS.get(check, check))}</td><td>{esc(key)}</td>"
                f"<td>{_n(entry.get('threshold'))} ({esc(str(entry.get('threshold_source') or 'auto'))}"
                f"{', ' + str(entry.get('offset_steps')) + ' steps' if entry.get('offset_steps') else ''})"
                f"</td><td>{_n(share)} % of {esc(str(entry.get('denominator') or (entry.get('at_auto') or {}).get('denominator') or '-'))}"
                f"</td><td>{esc(said or '-')}</td><td>{esc(fate or str(entry.get('reason') or '-'))}</td></tr>")
    # What was not checked, or checked more widely than asked, and why:
    # silence on a check must not read as a clean result.
    notes = [f"<li><b>{esc(schemas.CHECK_WORDS.get(n.get('check'), n.get('check')))}</b>"
             f"{' (' + esc(n['unit']) + ')' if n.get('unit') else ''}: "
             f"{esc(PLANNING_WORDS.get(n.get('status'), str(n.get('status'))))} -- "
             f"{esc(str(n.get('reason') or ''))}</li>"
             for n in report.get("planning_notes") or ()]
    if not rows and not notes:
        return []
    out = ["<h2>Image checks</h2>"]
    if rows:
        out += ["<table><tr><th>check</th><th>on</th><th>bar (source)</th>"
                "<th>flagged at the bar</th><th>rows judged</th><th>regions</th></tr>", *rows,
                "</table>"]
    if notes:
        out += ["<p>Not checked, or checked differently from the default:</p><ul>", *notes,
                "</ul>"]
    return out


def _marker_counts_html(cells, esc):
    """How many cells have each marker flagged, by reason (counts only: the
    per-cell evidence is in the exports), and the regions the cells' own
    values did not bear out."""
    parts = []
    by_marker = cells.get("marker_flags") or {}
    if by_marker:
        parts.append("<h2>Markers flagged in cells</h2><p class='muted'>The cell is kept; "
                     "the marker's value should not be read in it.</p><table><tr>"
                     "<th>marker</th><th>reason</th><th>cells (unreliable)</th>"
                     "<th>cells (flagged)</th></tr>")
        for marker, reasons in sorted(by_marker.items()):
            for reason, counts in sorted(reasons.items()):
                parts.append(f"<tr><td>{esc(marker)}</td><td>{esc(_reason_words(reason))}</td>"
                             f"<td>{_n(counts.get('exclude'))}</td><td>{_n(counts.get('warn'))}"
                             "</td></tr>")
        parts.append("</table>")
    missed = cells.get("not_borne_out") or []
    if missed:
        parts.append("<h2>Regions the cells do not bear out</h2><p class='muted'>Seen in the "
                     "image, but the cells' own values give no reason to flag them.</p><ul>"
                     + "".join(f"<li>{esc(m.get('roi_id') or '')} "
                               f"({esc(schemas.CLASS_WORDS.get(m.get('class'), ''))}, "
                               f"{esc(m.get('channel') or '')}): {esc(m.get('why') or '')}</li>"
                               for m in missed) + "</ul>")
    return parts


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
                   ["cells warned", _n(d["cells_warned"])],
                   ["cells with a marker flagged", _n(d.get("cells_marker_flagged"))],
                   ["cells outside the tissue (annotated, kept)",
                    _n(d.get("cells_background"))]],
                  [60, 80]),
             Spacer(1, 2 * mm), Paragraph(esc(d["sentence"]), muted), Spacer(1, 3 * mm)]
    overview = _safe(_overview, call, report["project"], report)
    if overview:
        story.append(picture(overview, frame_w * 0.6, (page_h - 2 * margin) * 0.55))
    story.append(PageBreak())
    story.append(Paragraph("Regions", heading))
    rows = [["#", "category", "subtype", "action", "channels", "severity", "confidence",
             "made by"]]
    for x in report["regions"]:
        rows.append([x["label"], schemas.category_words(x["category"]),
                     schemas.CLASS_WORDS.get(x["class"], x["class"]), x["action"],
                     ", ".join(x["channels"][:6]), x.get("severity") or "-",
                     x.get("confidence") or "-", x["created_by"]])
    story.append(grid(rows, [12, 40, 44, 18, 66, 20, 22, 26]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Channels", heading))
    rows = [["channel", "cycle", "status", "audit verdict", "class", "reason", "flags"]]
    for channel in report["channels"]:
        verdict, klass, why = _audit_words(channel)
        rows.append([channel["name"], _n(channel.get("cycle")), channel.get("status"),
                     verdict, klass, why, ", ".join(channel.get("flags") or [])])
    story.append(grid(rows, [36, 12, 30, 22, 36, 86, 40]))
    cells = report["cells"]
    if cells.get("n"):
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Cells", heading))
        rows = [["reason", "excluded", "warned", "noted"]]
        for reason in _cell_reasons(cells):
            rows.append([_reason_words(reason), _n((cells.get("by_reason") or {}).get(reason)),
                         _n((cells.get("warn_by_reason") or {}).get(reason)),
                         _n((cells.get("note_by_reason") or {}).get(reason))])
        story.append(grid(rows, [80, 30, 30, 30]))
        marker_rows = [["marker", "reason", "unreliable", "flagged"]]
        for marker, reasons in sorted((cells.get("marker_flags") or {}).items()):
            for reason, counts in sorted(reasons.items()):
                marker_rows.append([marker, reason, _n(counts.get("exclude")),
                                    _n(counts.get("warn"))])
        if len(marker_rows) > 1:
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph("Markers flagged in cells (the cells are kept)", heading))
            story.append(grid(marker_rows, [40, 70, 25, 25]))
    retention, digest = _retention_rows(report)
    if retention:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("DNA retention by cycle", heading))
        story.append(Paragraph(esc(digest or ""), body))
        story.append(grid([["cycle", "DNA channel", "% retained", "% retained through",
                            "markers in cycle", "cells flagged"],
                           *[list(map(str, row)) for row in retention]],
                          [14, 36, 24, 30, 110, 24]))
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
