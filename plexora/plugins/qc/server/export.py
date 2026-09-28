"""QC results as files other tools read: cells CSV, regions GeoJSON, summary.

    <agent_root>/exports/qc_<project>_<result_id>_<stamp>/
        cells.csv            one row per cell: pass, action, reasons, ROI ids
        qc_regions.geojson   every QC region (geometry as the ROI plugin holds
                             it, the user's edits included) with its metadata
        summary.json         the counts, with their denominators
        result.json          the whole result document

Free, and readable after a licence lapses: what a session produced belongs
to the user.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from plexora.plugins.qc.server import results, roi_link


def _root(project, result_id):
    from plexora import paths

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(project))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder = paths.agent_root() / "exports" / f"qc_{safe}_{result_id}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def regions_geojson(ds, result):
    meta = results.roi_meta(ds.name)
    rows = {r["roi_id"]: r for r in meta.to_dicts()} if meta.height else {}
    features = []
    for region in roi_link.live_regions(ds, result):
        row = rows.get(region["roi_id"]) or {}
        candidate = (result.get("candidates") or {}).get(region["candidate_id"]) or {}
        properties = {"roi_id": region["roi_id"], "candidate_id": region["candidate_id"],
                      "class": region["class"], "action": region["action"],
                      "channels": region["channels"],
                      "scope": candidate.get("scope") or row.get("scope"),
                      "severity": (candidate.get("ai_decision") or {}).get("severity"),
                      "confidence": (candidate.get("ai_decision") or {}).get("confidence"),
                      "detector": candidate.get("detector"),
                      "created_by": candidate.get("created_by") or row.get("created_by"),
                      "user_edited": bool(row.get("user_edited")),
                      "approved": bool(row.get("approved")),
                      "session_id": result.get("session_id"),
                      "result_id": result.get("result_id")}
        features.append({"type": "Feature", "geometry": region["geometry"],
                         "properties": properties})
    return {"type": "FeatureCollection", "features": features,
            "properties": {"coordinate_space": "full-resolution image pixels",
                           "project": ds.name}}


def cells_csv(cells, path):
    import polars as pl

    frame = cells.select([c for c in ("cell_id", "pass", "action", "primary_reason",
                                      "reasons", "reason_count", "roi_ids", "roi_method")
                          if c in cells.columns])
    frame = frame.with_columns([pl.col("reasons").list.join(";"),
                                pl.col("roi_ids").list.join(";")])
    frame.write_csv(path)


def export(call, project, *, what="both", result_id=None):
    session = call.session
    ds = session.image_data(project)
    document = results.load(project)
    result = results.get_result(project, document, result_id) if result_id \
        else results.active(document)
    if result is None:
        from plexora.agent.errors import AgentError

        raise AgentError("precondition_missing", "this project has no QC result to export",
                         detail={"hint": "finish a QC session, or draw QC regions"})
    folder = _root(project, result["result_id"])
    files = {}
    if what in ("rois", "both"):
        path = folder / "qc_regions.geojson"
        path.write_text(json.dumps(regions_geojson(ds, result)), encoding="utf-8")
        files["regions"] = str(path)
    if what in ("cells", "both"):
        cells = results.cells(project)
        if cells is not None and cells.height and \
                (result_id is None or result_id == document.get("active_result_id")):
            path = folder / "cells.csv"
            cells_csv(cells, path)
            files["cells"] = str(path)
    summary = results.summary(result)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                         encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(result, default=str), encoding="utf-8")
    files["summary"] = str(folder / "summary.json")
    files["result"] = str(folder / "result.json")
    return {"folder": str(folder), "files": files, "summary": summary}
