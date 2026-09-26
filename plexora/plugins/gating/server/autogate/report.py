"""What a gating session did, for the person who has to trust it.

One report per session, HTML (self-contained, images embedded) and PDF (A4
landscape, reportlab -- Figure Builder's own dependency), built from the same
data: a summary table of every marker, then one section per marker with the
final gate beside the GMM proposal, the confidence and why, the flags, the
distribution with both gates, the cells either side of the final gate, and
the decisions that led there. For a dataset, the spread of each marker's gate
across images and the strategy the drift classes imply. Visual first; words
only where a number needs saying.

`export_gates` writes the gates themselves (and their provenance) as CSV for
other tools.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import time

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate.engine import store, unit_key

STATE_LABELS = {
    "accepted": "accepted", "accepted_low_confidence": "accepted (low confidence)",
    "manual_review_recommended": "manual review", "technically_failed": "technically failed",
    "not_binary": "not binary", "insufficient_information": "needs information",
    "skipped_locked": "locked / approved", "skipped_excluded": "excluded",
    "skipped_manual": "user's gate kept", "skipped_no_marker": "not in this image",
}
CONFIDENCE_COLOURS = {"high": "#2f9e44", "moderate": "#e0a800", "low": "#e8590c",
                      "manual_review": "#c92a2a", "failed_qc": "#868e96"}


def _root():
    from plexora import paths

    return paths.agent_root()


def _histogram_png(call, unit):
    from plexora.agent import plots
    from plexora.server.utils import fast_png

    ds = call.session.data(unit["project"])
    values = np.asarray(ds.table.columns([unit["marker"]])[unit["marker"]], dtype=np.float64)
    gate = unit.get("final") if unit.get("final") is not None else unit.get("gmm")
    if gate is None:
        return None
    fit = model.gmm_for(ds, unit["marker"])
    image = plots.draw_histogram(values, gate=gate,
                                 curves={"background": fit.get("gmm_1") or [],
                                         "positive": fit.get("gmm_2") or []},
                                 width=420, height=240,
                                 title=f"{unit['marker']}: final {_n(gate)}  GMM "
                                       f"{_n(unit.get('gmm'))}")
    # One gate line (the final one); the GMM gate is named in the title, which
    # keeps a 420-pixel plot legible.
    return fast_png.encode_rgb8_png(np.asarray(image))


def _cells_png(call, unit):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import tableops, views

    ds = call.session.data(unit["project"])
    channel = views.image_channel(ds, unit["marker"])
    low = unit.get("final") if unit.get("final") is not None else unit.get("gmm")
    if channel is None or low is None:
        return None
    sample = tableops.local_or_node(ds, "gating.autogate.sample", {
        "marker": unit["marker"], "low": low, "high": unit.get("high")})
    near = [c for n in ("just_below", "borderline", "just_above", "low_background",
                        "moderate_positive") for c in sample["strata"].get(n) or []]
    near = sorted(sorted(near, key=lambda c: abs(c["value"] - low))[:6],
                  key=lambda c: c["value"])
    if not near:
        return None
    rows = [{"label": "near the final gate", "cells": [
        dict(c, call=views.call_of(c["value"], low, unit.get("high") or float("inf")))
        for c in near]}]
    rendered = collage.render_collage(call.session, ds, layout="strip", rows=rows,
                                      marker=channel, gate=low, fmt="png", store=False,
                                      tile_px=56, title=f"{unit['marker']} around "
                                                        f"{collage.compact_number(low)}")
    return rendered["png"]


def _n(value):
    if value is None:
        return "-"
    from plexora.agent.evidence.collage import compact_number

    return compact_number(value)


def build(call, session_id) -> dict:
    st = store()
    record = st.load(session_id)
    decisions = st.decisions(session_id)
    units = []
    for project in record["images"]:
        for marker in record["order"]:
            unit = record["units"].get(unit_key(project, marker))
            if unit is None:
                continue
            trail = [d for d in decisions if any(
                u.get("project") == project and u.get("marker") == marker
                for u in (d.get("units") or [])) or d.get("unit") == unit_key(project, marker)]
            units.append({**unit, "trail": trail[-12:]})
    counts = {}
    for unit in units:
        counts[unit["state"]] = counts.get(unit["state"], 0) + 1
    out = {"session": {k: record.get(k) for k in ("session_id", "created_at", "finished_at",
                                                   "state", "scope", "dataset", "images",
                                                   "reference_image", "principal")},
           "options": record["options"], "used": record.get("used"),
           "vision_tokens": -(-int((record.get("used") or {}).get("pixels", 0)) // 750),
           "counts": counts, "units": units, "questions": record.get("questions") or [],
           "receipts": record.get("receipts") or []}
    if record.get("scope") == "dataset":
        spread = {}
        for marker in record["order"]:
            finals = [u.get("final") for u in units if u["marker"] == marker
                      and u.get("final") is not None]
            if finals:
                spread[marker] = {"n_images": len(finals), "min": min(finals),
                                  "median": float(np.median(finals)), "max": max(finals)}
        out["dataset"] = {"spread": spread, "experimental_unit": "image"}
        from plexora.plugins.gating.server.autogate import engine as engines
        from plexora.plugins.gating.server.autogate import transfer

        with engines.engine_for(call, session_id, save=False) as engine:
            out["dataset"]["strategy"] = transfer.dataset_summary(engine)["markers"]
    return out


def _b64(png):
    return base64.b64encode(png).decode("ascii") if png else None


def to_html(call, report) -> str:
    esc = html.escape
    s = report["session"]
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Gating report {esc(s['session_id'])}</title>",
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1b1e23;"
        "background:#fff}table{border-collapse:collapse;margin:12px 0}"
        "td,th{border:1px solid #d0d4da;padding:4px 8px;text-align:left;font-size:13px}"
        "th{background:#f1f3f5}section{border-top:1px solid #d0d4da;margin-top:20px;"
        "padding-top:12px}.pill{display:inline-block;padding:1px 8px;border-radius:9px;"
        "color:#fff;font-size:12px}.muted{color:#6b7280}img{max-width:100%;"
        "border:1px solid #e5e7eb;margin:4px 8px 4px 0;vertical-align:top}"
        "@media (prefers-color-scheme: dark){body{background:#15171b;color:#e6e6e6}"
        "th{background:#23262d}td,th{border-color:#3a3f48}}</style></head><body>",
        f"<h1>Automatic gating: {esc(', '.join(s['images'][:4]))}"
        f"{' …' if len(s['images']) > 4 else ''}</h1>",
        f"<p class='muted'>Session {esc(s['session_id'])} · {esc(s['scope'])} · mode "
        f"{esc(report['options']['mode'])} · started {esc(str(s['created_at']))} · state "
        f"{esc(s['state'])} · {len(report['receipts'])} gate writes (each undoable) · "
        f"~{report['vision_tokens']} vision tokens</p>",
        "<table><tr><th>outcome</th><th>markers</th></tr>" + "".join(
            f"<tr><td>{esc(STATE_LABELS.get(k, k))}</td><td>{v}</td></tr>"
            for k, v in sorted(report["counts"].items())) + "</table>",
        "<table><tr><th>image</th><th>marker</th><th>outcome</th><th>confidence</th>"
        "<th>final gate</th><th>GMM</th><th>tier</th><th>flags</th><th>why</th></tr>",
    ]
    for unit in report["units"]:
        colour = CONFIDENCE_COLOURS.get(unit.get("confidence"), "#868e96")
        parts.append(
            f"<tr><td>{esc(unit['project'])}</td><td><a href='#{esc(unit_key(unit['project'], unit['marker']))}'>"
            f"{esc(unit['marker'])}</a></td><td>{esc(STATE_LABELS.get(unit['state'], unit['state']))}</td>"
            f"<td><span class='pill' style='background:{colour}'>"
            f"{esc(str(unit.get('confidence') or '-'))}</span></td>"
            f"<td>{_n(unit.get('final'))}</td><td>{_n(unit.get('gmm'))}</td>"
            f"<td>{esc(str(unit.get('tier') or '-'))}</td>"
            f"<td>{esc(', '.join((unit.get('flags') or [])[:4]))}</td>"
            f"<td class='muted'>{esc(str(unit.get('reason') or ''))[:160]}</td></tr>")
    parts.append("</table>")
    if report.get("dataset"):
        parts.append("<h2>Across images</h2><table><tr><th>marker</th><th>images</th>"
                     "<th>min</th><th>median</th><th>max</th><th>strategy</th></tr>")
        strategy = report["dataset"].get("strategy") or {}
        for marker, row in report["dataset"]["spread"].items():
            parts.append(f"<tr><td>{esc(marker)}</td><td>{row['n_images']}</td>"
                         f"<td>{_n(row['min'])}</td><td>{_n(row['median'])}</td>"
                         f"<td>{_n(row['max'])}</td><td>"
                         f"{esc(str((strategy.get(marker) or {}).get('strategy') or '-'))}"
                         "</td></tr>")
        parts.append("</table><p class='muted'>The image is the experimental unit.</p>")
    if report["questions"]:
        parts.append("<h2>Questions for you</h2><ul>" + "".join(
            f"<li><b>{esc(q['unit'])}</b>: {esc(q['question'])}"
            f"{' (' + esc(', '.join(q.get('options') or [])) + ')' if q.get('options') else ''}"
            "</li>" for q in report["questions"]) + "</ul>")
    embedded = 0
    for unit in report["units"]:
        if unit["state"] in ("skipped_no_marker",):
            continue
        parts.append(f"<section id='{esc(unit_key(unit['project'], unit['marker']))}'>"
                     f"<h3>{esc(unit['marker'])} <span class='muted'>· "
                     f"{esc(unit['project'])}</span></h3>")
        parts.append(f"<p>{esc(STATE_LABELS.get(unit['state'], unit['state']))}, confidence "
                     f"<b>{esc(str(unit.get('confidence') or '-'))}</b>. Final "
                     f"{_n(unit.get('final'))}, GMM {_n(unit.get('gmm'))}"
                     f"{', class ' + esc(str(unit.get('class'))) if unit.get('class') else ''}"
                     f". {esc(str(unit.get('reason') or ''))}</p>")
        if embedded < 40:
            for png in (_safe_render(_histogram_png, call, unit),
                        _safe_render(_cells_png, call, unit)):
                if png:
                    parts.append(f"<img alt='' src='data:image/png;base64,{_b64(png)}'>")
                    embedded += 1
        if unit.get("trail"):
            parts.append("<details><summary>decisions</summary><ul>" + "".join(
                f"<li class='muted'>{esc(str(d.get('event')))} {esc(str(d.get('kind') or d.get('state') or ''))}"
                f"{' — ' + esc(str((d.get('outcome') or {}).get('state') or d.get('reason') or '')) if d.get('outcome') or d.get('reason') else ''}"
                "</li>" for d in unit["trail"]) + "</ul></details>")
        parts.append("</section>")
    parts.append("<p class='muted'>Gates are lower bounds on each table's own scale "
                 "(low &lt; value &le; high, compared in float32). Every write is in the "
                 "audit log with an undo hint.</p></body></html>")
    return "".join(parts)


def _safe_render(fn, call, unit):
    try:
        return fn(call, unit)
    except Exception:
        return None


def to_pdf(call, report, path):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as pdf_canvas
    except ImportError as exc:  # pragma: no cover
        raise AgentError("capability_unavailable", "the PDF report needs reportlab") from exc
    width, height = landscape(A4)
    pdf = pdf_canvas.Canvas(str(path), pagesize=(width, height))
    s = report["session"]
    pdf.setTitle(f"Plexora gating report {s['session_id']}")

    def footer():
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.45, 0.45, 0.45)
        pdf.drawString(12 * mm, 8 * mm, f"Plexora automatic gating · session "
                                        f"{s['session_id']} · every gate write is audited "
                                        "and undoable")
        pdf.setFillColorRGB(0, 0, 0)

    y = height - 18 * mm
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(12 * mm, y, f"Automatic gating: {', '.join(s['images'][:3])}")
    y -= 7 * mm
    pdf.setFont("Helvetica", 9)
    pdf.drawString(12 * mm, y, f"{s['scope']} · mode {report['options']['mode']} · "
                               f"{len(report['receipts'])} writes · ~{report['vision_tokens']} "
                               "vision tokens")
    y -= 9 * mm
    columns = (("image", 0), ("marker", 48), ("outcome", 88), ("confidence", 138),
               ("final", 168), ("GMM", 188), ("tier", 208), ("why", 220))
    pdf.setFont("Helvetica-Bold", 8)
    for name, x in columns:
        pdf.drawString((12 + x) * mm, y, name)
    pdf.setFont("Helvetica", 8)
    for unit in report["units"]:
        y -= 4.6 * mm
        if y < 16 * mm:
            footer()
            pdf.showPage()
            y = height - 18 * mm
            pdf.setFont("Helvetica", 8)
        values = (unit["project"][:22], unit["marker"][:18],
                  STATE_LABELS.get(unit["state"], unit["state"])[:24],
                  str(unit.get("confidence") or "-"), _n(unit.get("final")),
                  _n(unit.get("gmm")), str(unit.get("tier") or "-"),
                  str(unit.get("reason") or "")[:70])
        for (name, x), value in zip(columns, values):
            pdf.drawString((12 + x) * mm, y, value)
    footer()
    pdf.showPage()
    drawn = 0
    for unit in report["units"]:
        if unit["state"] in ("skipped_no_marker",) or drawn >= 60:
            continue
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(12 * mm, height - 16 * mm, f"{unit['marker']}  ·  {unit['project']}")
        pdf.setFont("Helvetica", 9)
        pdf.drawString(12 * mm, height - 23 * mm,
                       f"{STATE_LABELS.get(unit['state'], unit['state'])}, confidence "
                       f"{unit.get('confidence') or '-'} · final {_n(unit.get('final'))} · GMM "
                       f"{_n(unit.get('gmm'))} · tier {unit.get('tier') or '-'} · class "
                       f"{unit.get('class') or '-'}")
        pdf.drawString(12 * mm, height - 29 * mm, str(unit.get("reason") or "")[:150])
        if unit.get("flags"):
            pdf.drawString(12 * mm, height - 35 * mm, "flags: " + ", ".join(unit["flags"][:8]))
        x = 12 * mm
        for png in (_safe_render(_histogram_png, call, unit),
                    _safe_render(_cells_png, call, unit)):
            if not png:
                continue
            image = ImageReader(io.BytesIO(png))
            iw, ih = image.getSize()
            scale = min(130 * mm / iw, 110 * mm / ih)
            pdf.drawImage(image, x, height - 40 * mm - ih * scale, iw * scale, ih * scale)
            x += iw * scale + 8 * mm
        footer()
        pdf.showPage()
        drawn += 1
    pdf.save()


def write_report(call, session_id, *, fmt="both") -> dict:
    report = build(call, session_id)
    folder = _root() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    session_folder = store().folder(session_id)
    out = {"session_id": session_id, "counts": report["counts"], "paths": {}}
    if fmt in ("html", "both"):
        path = folder / f"gating_{session_id}.html"
        path.write_text(to_html(call, report), encoding="utf-8")
        (session_folder / "report.html").write_text(path.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
        out["paths"]["html"] = str(path)
    if fmt in ("pdf", "both"):
        path = folder / f"gating_{session_id}.pdf"
        to_pdf(call, report, path)
        out["paths"]["pdf"] = str(path)
    out["questions"] = report["questions"]
    return out


# -- export -------------------------------------------------------------------


def _gate_rows(call, project):
    from plexora.plugins.gating.server.autogate import provenance

    ds = call.session.data(project)
    rows = provenance.read(ds.name)
    out = []
    for gate in model.all_gates(ds):
        prov = rows.get(gate["marker"]) or {}
        out.append({"image": project, "channel": gate["marker"], "gate_start": gate["low"],
                    "gate_end": gate["high"], "thresholded": gate["thresholded"],
                    "method": prov.get("method"), "status": prov.get("status"),
                    "confidence": prov.get("confidence"), "state": prov.get("state"),
                    "gmm_proposal": prov.get("gmm_proposal"),
                    "session_id": prov.get("session_id"),
                    "operation_id": prov.get("operation_id"),
                    "timestamp": prov.get("timestamp")})
    return out


def _write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_gates(call, *, project=None, dataset=None, include_provenance=True) -> dict:
    import re

    if dataset:
        from plexora import datasets

        try:
            cohort = datasets.dataset(dataset)
        except KeyError as exc:
            raise AgentError("invalid_input", str(exc.args[0])) from None
        projects = list(cohort.projects)
        label = cohort.name
    else:
        call.session.project(project)
        projects = [project]
        label = project
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    folder = _root() / "exports" / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    long_rows = []
    for name in projects:
        rows = _gate_rows(call, name)
        long_rows.extend(rows)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        path = folder / f"{safe}_gates.csv"
        _write_csv(path, rows, ["channel", "gate_start", "gate_end", "thresholded"])
        written.append(str(path))
        if include_provenance:
            path = folder / f"{safe}_gates_provenance.csv"
            _write_csv(path, rows, ["channel", "gate_start", "gate_end", "method", "status",
                                    "confidence", "state", "gmm_proposal", "session_id",
                                    "operation_id", "timestamp"])
            written.append(str(path))
    if dataset:
        path = folder / "dataset_gates_long.csv"
        _write_csv(path, long_rows, ["image", "channel", "gate_start", "gate_end",
                                     "thresholded", "method", "status", "confidence"])
        written.append(str(path))
    return {"folder": str(folder), "files": written, "images": len(projects),
            "note": "gate_start is the lower bound: cells with gate_start < value <= "
                    "gate_end are positive"}


def provenance_long(call, projects) -> list:
    """Rows for `uns['gates_provenance']`: marker, image_id, value, method, ..."""
    rows = []
    for project in projects:
        for row in _gate_rows(call, project):
            if row["thresholded"]:
                rows.append({"marker": row["channel"], "image_id": project,
                             "value": row["gate_start"], "method": row["method"] or "manual",
                             "status": row["status"] or "", "confidence":
                                 row["confidence"] or "", "session_id": row["session_id"] or "",
                             "timestamp": row["timestamp"] or ""})
    return rows

