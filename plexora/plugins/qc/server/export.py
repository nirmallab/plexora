"""QC results as files other tools read: cells CSV, regions GeoJSON, summary.

    <agent_root>/exports/qc_<project>_<result_id>_<stamp>/
        cells.csv            one row per cell: pass, action, reasons (the whole
                             cell's), qc_category (its primary reason's) and
                             categories, flag_source (direct: read on the cell;
                             roi: inherited from a region; both),
                             unreliable_markers and marker_flags
                             ("marker|reason|status": one channel's value),
                             ROI ids
        qc_regions.geojson   every QC region (geometry as the ROI plugin holds
                             it, the user's edits included) with its category,
                             subtype, score, threshold and its source, the
                             agent's verdict and the cells it removes; with the
                             ROI export's `plexora` member (producer "qc",
                             image_id, coordinate_space, categories, result_id)
                             and a `category_id` per feature, so it imports
                             into the ROI tool and SCIMAP Pro like an ROI file
        qc_provenance.json   every region, cell reason and marker reason with
                             the steps behind it (`provenance.document`)
        qc_findings.csv      the same, one row per finding
        summary.json         the counts, with their denominators
        result.json          the whole result document

`what` picks: "rois" (the GeoJSON), "cells" (cells.csv), "provenance" (the
two provenance files), "both" (all of them).

Segmentation QC rides along when it has a result: cells.csv gains
seg_qc_status / seg_qc_under_score / seg_qc_over_score / seg_qc_reason /
seg_qc_partner_id and seg_qc_large / seg_qc_small / seg_qc_irregular (the
mask's shape outliers at the default 3 robust SDs; joined on cell_id), and
summary.json its summary under
`segmentation_qc`. With only a Segmentation QC result the export is those two
files alone.

Free, and readable after a licence lapses: what a session produced belongs
to the user.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from plexora.plugins.qc.server import provenance, results, roi_link, schemas

WHATS = ("rois", "cells", "provenance", "both")
#: The columns cells.csv adds to each cell's call: its category, every
#: category its reasons fall in, and whether they were read on the cell
#: (`direct`), inherited from a region (`roi`) or both.
CELL_CATEGORY_COLUMNS = ("qc_category", "categories", "flag_source")


def _root(project, result_id):
    from plexora import paths

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(project))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder = paths.agent_root() / "exports" / f"qc_{safe}_{result_id}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


#: The prefix of a QC region's category id in the exported document, so a QC
#: category never merges into a user's ROI category of the same word when the
#: file is imported into the ROI tool or read by SCIMAP Pro.
CATEGORY_PREFIX = "qc_"


def _member(ds, result, categories):
    """The `plexora` foreign member, in the ROI export's shape
    (plugins/roi/server/geojson.export_document) with `producer: "qc"` and the
    result it came from. What `roi.geojson.validate_document` reads to know
    what the coordinates mean, and what SCIMAP Pro's `hl.addExternalROI` reads
    to know which image the regions belong to -- so QC regions import like ROI
    regions instead of being refused as "not exported by Plexora"."""
    from plexora.plugins.qc import VERSION
    from plexora.plugins.roi.server import schema as roi_schema
    from plexora.server.models.project import Project

    record = Project.find(ds.name)
    image_id = None
    width = height = None
    if record is not None:
        subset = (record.dataset.subset or {}) if record.dataset is not None else {}
        image_id = None if subset.get("value") is None else str(subset["value"])
        width, height = record.image.width, record.image.height
    member = {
        "schema_version": roi_schema.SCHEMA_VERSION,
        "plugin_version": VERSION,
        "producer": "qc",
        "datasource": ds.name,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coordinate_space": roi_schema.coordinate_space(width, height),
        "categories": categories,
        "result_id": result.get("result_id"),
    }
    if image_id is not None:
        member["image_id"] = image_id
    return member


def regions_geojson(ds, result):
    counts = provenance.cells_per_region(ds.name, result)
    meta = results.roi_meta(ds.name)
    rows = {r["roi_id"]: r for r in meta.to_dicts()} if meta.height else {}
    features = []
    for region in roi_link.live_regions(ds, result):
        row = rows.get(region["roi_id"]) or {}
        candidate = (result.get("candidates") or {}).get(region["candidate_id"]) or {}
        record = provenance.region_summary(candidate, checks=result.get("checks"))
        category = region.get("category") or schemas.category_of_class(region["class"])
        properties = {"roi_id": region["roi_id"], "candidate_id": region["candidate_id"],
                      "category": category,
                      "category_words": schemas.category_words(category),
                      "class": region["class"],
                      "class_words": schemas.CLASS_WORDS.get(region["class"], region["class"]),
                      "action": region["action"],
                      "channels": region["channels"],
                      "scope": candidate.get("scope") or row.get("scope"),
                      "severity": (candidate.get("ai_decision") or {}).get("severity"),
                      "confidence": (candidate.get("ai_decision") or {}).get("confidence"),
                      "detector": candidate.get("detector"),
                      "detector_version": candidate.get("detector_version"),
                      "score": record["score"], "score_kind": record["score_kind"],
                      "threshold": record["threshold"],
                      "threshold_source": record["threshold_source"],
                      "offset_steps": record["offset_steps"],
                      "ai_verdict": (record["ai"] or {}).get("verdict"),
                      "ai_confidence": (record["ai"] or {}).get("confidence"),
                      "ai_notes": (record["ai"] or {}).get("notes"),
                      "n_cells": (counts or {}).get(region["roi_id"]),
                      "created_by": candidate.get("created_by") or row.get("created_by"),
                      "user_edited": bool(row.get("user_edited")),
                      "refinement_status": (candidate.get("refinement") or {}).get("status"),
                      "refinement_method": (candidate.get("refinement") or {}).get("method"),
                      "kept_fraction": (candidate.get("refinement") or {}).get(
                          "kept_fraction"),
                      "approved": bool(row.get("approved")),
                      "session_id": result.get("session_id"),
                      "result_id": result.get("result_id")}
        properties["category_id"] = f"{CATEGORY_PREFIX}{category}"
        features.append({"type": "Feature", "geometry": region["geometry"],
                         "properties": properties})
    seen = sorted({f["properties"]["category"] for f in features})
    categories = [{"id": f"{CATEGORY_PREFIX}{c}", "label": f"QC: {schemas.category_words(c)}",
                   "color": schemas.category_color(c), "sort_order": i}
                  for i, c in enumerate(seen)]
    return {"type": "FeatureCollection", "plexora": _member(ds, result, categories),
            "features": features,
            "properties": {"coordinate_space": "full-resolution image pixels",
                           "project": ds.name}}


SEG_COLUMNS = {"status_word": "seg_qc_status", "under_score": "seg_qc_under_score",
               "over_score": "seg_qc_over_score", "reason": "seg_qc_reason",
               "partner_id": "seg_qc_partner_id"}


def _seg_columns(seg):
    import polars as pl

    frame = seg.select(["cell_id", *SEG_COLUMNS]).rename(SEG_COLUMNS)
    frame = frame.with_columns(pl.col("cell_id").cast(pl.Int64),
                               pl.when(pl.col("seg_qc_partner_id") > 0)
                               .then(pl.col("seg_qc_partner_id")).otherwise(None)
                               .alias("seg_qc_partner_id"))
    return frame


def _sizes(project):
    """cell_id and seg_qc_large / _small / _irregular at the default
    thresholds, or None (a result stored before cells had shapes)."""
    from plexora.plugins.qc.server.segqc import run as segqc

    found = segqc.calls(project)
    names = [n for n in segqc.SIZE_CATEGORIES if found is not None and n in found[1].columns]
    if not names:
        return None
    return found[1].select("cell_id", *[found[1][n].alias(f"seg_qc_{n}") for n in names])


def _categories(cells):
    """qc_category (the primary reason's, else the first reason's),
    categories (every reason's, in category order) and flag_source (direct:
    a reason read on the cell; roi: one from a region it sits in; both)."""
    import polars as pl

    order = {k: i for i, k in enumerate((*schemas.CATEGORY_IDS, schemas.REVIEW["id"]))}

    def of(reasons):
        return sorted({schemas.category_of_reason(r) for r in reasons or []},
                      key=lambda c: order.get(c, 99))

    def source(reasons):
        region = any(str(r).startswith("region:") for r in reasons or [])
        direct = any(not str(r).startswith("region:") for r in reasons or [])
        return "both" if region and direct else "roi" if region else \
            "direct" if direct else None

    reasons = cells["reasons"].to_list() if "reasons" in cells.columns else [[]] * cells.height
    primary = cells["primary_reason"].to_list() if "primary_reason" in cells.columns \
        else [""] * cells.height
    categories = [of(r) for r in reasons]
    first = [schemas.category_of_reason(p) if p else (c[0] if c else None)
             for p, c in zip(primary, categories)]
    return (pl.Series("qc_category", first, dtype=pl.Utf8),
            pl.Series("categories", [";".join(c) for c in categories], dtype=pl.Utf8),
            pl.Series("flag_source", [source(r) for r in reasons], dtype=pl.Utf8))


def cells_csv(cells, path, seg=None, sizes=None):
    import polars as pl

    frame = None
    if cells is not None:
        frame = cells.select([c for c in ("cell_id", "pass", "action", "primary_reason",
                                          "reasons", "reason_count", "unreliable_markers",
                                          "marker_flags", "roi_ids", "roi_method")
                              if c in cells.columns])
        frame = frame.with_columns(*_categories(cells))
        frame = frame.with_columns([pl.col(c).list.join(";") for c in (
            "reasons", "unreliable_markers", "marker_flags", "roi_ids") if c in frame.columns])
    if seg is not None:
        seg = _seg_columns(seg)
        if sizes is not None:
            seg = seg.join(sizes, on="cell_id", how="left")
        frame = seg if frame is None else frame.with_columns(
            pl.col("cell_id").cast(pl.Int64)).join(seg, on="cell_id", how="left")
    frame.write_csv(path)


def provenance_files(ds, project, result, folder, files):
    """Write qc_provenance.json and qc_findings.csv; returns the document."""
    import csv

    body = provenance.document(ds, project, result, files=dict(files))
    path = folder / "qc_provenance.json"
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    table = folder / "qc_findings.csv"
    with table.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(provenance.FINDINGS_COLUMNS))
        writer.writeheader()
        for row in provenance.findings_rows(body):
            writer.writerow({k: "" if row.get(k) is None else row.get(k)
                             for k in provenance.FINDINGS_COLUMNS})
    files["provenance"] = str(path)
    files["findings"] = str(table)
    return body


def export(call, project, *, what="both", result_id=None):
    session = call.session
    ds = session.image_data(project)
    document = results.load(project)
    result = results.get_result(project, document, result_id) if result_id \
        else results.active(document)
    from plexora.plugins.qc.server.segqc import run as segqc

    seg_summary = segqc.current(project) if result_id is None else None
    seg = segqc.frame(project) if seg_summary is not None else None
    sizes = _sizes(project) if seg is not None else None
    if result is None and seg is None:
        from plexora.agent.errors import AgentError

        raise AgentError("precondition_missing", "this project has no QC result to export",
                         detail={"hint": "finish a QC session, draw QC regions, or run "
                                         "Segmentation QC"})
    if result is None:
        folder = _root(project, "segmentation")
        path = folder / "cells.csv"
        cells_csv(None, path, seg, sizes)
        summary = {"segmentation_qc": {k: v for k, v in seg_summary.items() if k != "mask"}}
        (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                             encoding="utf-8")
        return {"folder": str(folder), "files": {"cells": str(path),
                                                 "summary": str(folder / "summary.json")},
                "summary": summary}
    folder = _root(project, result["result_id"])
    files = {}
    if what in ("rois", "provenance", "both"):
        # The provenance records point into the GeoJSON by ROI id.
        path = folder / "qc_regions.geojson"
        path.write_text(json.dumps(regions_geojson(ds, result)), encoding="utf-8")
        files["regions"] = str(path)
    if what in ("cells", "both"):
        cells = results.cells(project)
        if cells is not None and cells.height and \
                (result_id is None or result_id == document.get("active_result_id")):
            path = folder / "cells.csv"
            cells_csv(cells, path, seg, sizes)
            files["cells"] = str(path)
        elif seg is not None:
            path = folder / "cells.csv"
            cells_csv(None, path, seg, sizes)
            files["cells"] = str(path)
    if what in ("provenance", "both"):
        provenance_files(ds, project, result, folder, files)
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
