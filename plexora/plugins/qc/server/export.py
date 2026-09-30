"""QC results as files other tools read: cells CSV, regions GeoJSON, summary.

    <agent_root>/exports/qc_<project>_<result_id>_<stamp>/
        cells.csv            one row per cell: pass, action, reasons (the whole
                             cell's), unreliable_markers and marker_flags
                             ("marker|reason|status": one channel's value),
                             ROI ids
        qc_regions.geojson   every QC region (geometry as the ROI plugin holds
                             it, the user's edits included) with its metadata
        summary.json         the counts, with their denominators
        result.json          the whole result document

Segmentation QC rides along when it has a result: cells.csv gains
seg_qc_status / seg_qc_under_score / seg_qc_over_score / seg_qc_reason /
seg_qc_partner_id (joined on cell_id), and summary.json its summary under
`segmentation_qc`. With only a Segmentation QC result the export is those two
files alone.

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
                      "refinement_status": (candidate.get("refinement") or {}).get("status"),
                      "refinement_method": (candidate.get("refinement") or {}).get("method"),
                      "kept_fraction": (candidate.get("refinement") or {}).get(
                          "kept_fraction"),
                      "approved": bool(row.get("approved")),
                      "session_id": result.get("session_id"),
                      "result_id": result.get("result_id")}
        features.append({"type": "Feature", "geometry": region["geometry"],
                         "properties": properties})
    return {"type": "FeatureCollection", "features": features,
            "properties": {"coordinate_space": "full-resolution image pixels",
                           "project": ds.name}}


SEG_COLUMNS = {"status_word": "seg_qc_status", "under_score": "seg_qc_under_score",
               "over_score": "seg_qc_over_score", "reason": "seg_qc_reason",
               "partner_id": "seg_qc_partner_id"}


def _seg_columns(seg):
    import polars as pl

    frame = seg.select(["cell_id", *SEG_COLUMNS]).rename(SEG_COLUMNS)
    return frame.with_columns(pl.col("cell_id").cast(pl.Int64),
                              pl.when(pl.col("seg_qc_partner_id") > 0)
                              .then(pl.col("seg_qc_partner_id")).otherwise(None)
                              .alias("seg_qc_partner_id"))


def cells_csv(cells, path, seg=None):
    import polars as pl

    frame = None
    if cells is not None:
        frame = cells.select([c for c in ("cell_id", "pass", "action", "primary_reason",
                                          "reasons", "reason_count", "unreliable_markers",
                                          "marker_flags", "roi_ids", "roi_method")
                              if c in cells.columns])
        frame = frame.with_columns([pl.col(c).list.join(";") for c in (
            "reasons", "unreliable_markers", "marker_flags", "roi_ids") if c in frame.columns])
    if seg is not None:
        seg = _seg_columns(seg)
        frame = seg if frame is None else frame.with_columns(
            pl.col("cell_id").cast(pl.Int64)).join(seg, on="cell_id", how="left")
    frame.write_csv(path)


def export(call, project, *, what="both", result_id=None):
    session = call.session
    ds = session.image_data(project)
    document = results.load(project)
    result = results.get_result(project, document, result_id) if result_id \
        else results.active(document)
    from plexora.plugins.qc.server.segqc import run as segqc

    seg_summary = segqc.current(project) if result_id is None else None
    seg = segqc.frame(project) if seg_summary is not None else None
    if result is None and seg is None:
        from plexora.agent.errors import AgentError

        raise AgentError("precondition_missing", "this project has no QC result to export",
                         detail={"hint": "finish a QC session, draw QC regions, or run "
                                         "Segmentation QC"})
    if result is None:
        folder = _root(project, "segmentation")
        path = folder / "cells.csv"
        cells_csv(None, path, seg)
        summary = {"segmentation_qc": {k: v for k, v in seg_summary.items() if k != "mask"}}
        (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                             encoding="utf-8")
        return {"folder": str(folder), "files": {"cells": str(path),
                                                 "summary": str(folder / "summary.json")},
                "summary": summary}
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
            cells_csv(cells, path, seg)
            files["cells"] = str(path)
        elif seg is not None:
            path = folder / "cells.csv"
            cells_csv(None, path, seg)
            files["cells"] = str(path)
    summary = results.summary(result)
    if seg_summary is not None:
        summary = {**summary, "segmentation_qc": {k: v for k, v in seg_summary.items()
                                                  if k != "mask"}}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                         encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(result, default=str), encoding="utf-8")
    files["summary"] = str(folder / "summary.json")
    files["result"] = str(folder / "result.json")
    return {"folder": str(folder), "files": files, "summary": summary}
