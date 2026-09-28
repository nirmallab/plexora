"""Where QC results live: the plugin store, never the session folder.

Session folders are swept (`sessions/store.py`), and a QC result must outlive
the session that made it -- and a licence -- so everything a result says is in
`api.store(<datasource>, "qc")`:

- the **document** (`put_state`, JSON): every result the project has had
  (newest `KEEP_RESULTS` inline, older ones archived to files), which one is
  active, and the strictness last applied;
- **`roi_meta`** (Parquet): one row per QC ROI id -- the metadata the ROI
  schema has no room for (class, scope, severity, confidence, action,
  detector, evidence, who made it, and the geometry hash it was written with,
  which is how a user's edit is recognised);
- **`qc_cells`** (Parquet): the active result's per-cell calls, reasons and
  the module measurements (`m_*`) a strictness change re-derives them from;
- **`qc_cell_rois`** (Parquet, long): which QC region each cell falls in, and
  by how much -- re-joined on a strictness change without touching the mask.

A result's `candidates` hold the measurements (immutable after the scan) and
the agent's strictness-free decision; the action under each preset is derived
(`strictness.action_for`), never stored as truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone

import polars as pl

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas

PLUGIN_NAME = "qc"
SCHEMA_VERSION = 1
KEEP_RESULTS = 5

ROI_META_SCHEMA = {
    "roi_id": pl.Utf8, "candidate_id": pl.Utf8, "result_id": pl.Utf8, "session_id": pl.Utf8,
    "class": pl.Utf8, "channels": pl.List(pl.Utf8), "cycles": pl.List(pl.Int16),
    "scope": pl.Utf8, "severity": pl.Utf8, "confidence": pl.Utf8, "action": pl.Utf8,
    "strictness_used": pl.Utf8, "detector": pl.Utf8, "detector_version": pl.Utf8,
    "evidence_artifacts": pl.List(pl.Utf8), "agent_id": pl.Utf8, "created_by": pl.Utf8,
    "created_at": pl.Utf8, "user_edited": pl.Boolean, "approved": pl.Boolean,
    "approved_action": pl.Utf8, "locked": pl.Boolean, "deleted": pl.Boolean,
    "removed_from_qc": pl.Boolean, "written_geometry_hash": pl.Utf8,
    "written_category_id": pl.Utf8, "operation_id": pl.Utf8,
}

CELL_ROIS_SCHEMA = {"cell_id": pl.Int64, "roi_id": pl.Utf8, "fraction": pl.Float32,
                    "method": pl.Utf8}

_LOCKS: dict = {}
_GUARD = threading.Lock()


def lock(datasource):
    """The read-modify-write lock of one project's QC store."""
    with _GUARD:
        return _LOCKS.setdefault(str(datasource), threading.RLock())


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _store(datasource):
    from plexora import api

    return api.store(datasource, PLUGIN_NAME)


# -- the document ----------------------------------------------------------------


def empty_document() -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision_seq": 0, "active_result_id": None,
            "results": {}, "archived": [],
            "strictness": {"preset": "standard", "thresholds": None}}


def load(datasource) -> dict:
    blob = _store(datasource).get_state()
    if not blob:
        return empty_document()
    try:
        document = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # Loud, as the ROI store is: "your QC results are unreadable" must not
        # read as "this project has no QC results".
        raise AgentError("internal_error", f"stored QC results could not be read: {exc}") \
            from exc
    base = empty_document()
    base.update(document)
    return base


def _canonical(document) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)


def revision(datasource, document=None) -> str:
    """A short hash of the document and the table sequence (the receipts'
    `revision_before/after`, and `expected_revision` for Free writes)."""
    document = load(datasource) if document is None else document
    return hashlib.sha1(_canonical(document).encode("utf-8")).hexdigest()[:16]


def save(datasource, document, *, expected_revision=None) -> str:
    """Store the document (archiving results past `KEEP_RESULTS`); returns
    the new revision. `expected_revision` refuses a stale caller."""
    with lock(datasource):
        if expected_revision is not None:
            current = revision(datasource)
            if current != expected_revision:
                raise AgentError("conflict", "the QC results changed since they were read",
                                 detail={"current_revision": current}, retryable=True)
        _archive(datasource, document)
        document["revision_seq"] = int(document.get("revision_seq") or 0) + 1
        _store(datasource).put_state(_canonical(document).encode("utf-8"))
        return revision(datasource, document)


def _archive(datasource, document):
    results = document.get("results") or {}
    if len(results) <= KEEP_RESULTS:
        return
    ordered = sorted(results.values(), key=lambda r: r.get("created_at") or "")
    keep = {r["result_id"] for r in ordered[-KEEP_RESULTS:]}
    keep.add(document.get("active_result_id"))
    folder = _store(datasource).directory() / "results"
    folder.mkdir(parents=True, exist_ok=True)
    for result in ordered:
        if result["result_id"] in keep:
            continue
        path = folder / f"{result['result_id']}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(result, default=str), encoding="utf-8")
        tmp.replace(path)
        document.setdefault("archived", []).append(
            {"result_id": result["result_id"], "session_id": result.get("session_id"),
             "created_at": result.get("created_at"), "path": str(path)})
        results.pop(result["result_id"], None)


def read_archived(datasource, result_id):
    path = _store(datasource).directory() / "results" / f"{result_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# -- results ---------------------------------------------------------------------


def new_result_id() -> str:
    return f"qr_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{secrets.token_hex(3)}"


def new_result(project, *, session_id=None, strictness=None, agent=None) -> dict:
    stamp = now_iso()
    return {
        "result_id": new_result_id(), "version": schemas.RESULT_VERSION,
        "session_id": session_id, "project": project, "created_at": stamp,
        "updated_at": stamp, "finished_at": None,
        "strictness": strictness or {"preset": "standard", "thresholds": None},
        "detector_versions": {}, "scan_version": None, "scan_fingerprint": None,
        "calibration_revision": None, "cycles_method": None, "agent": agent or {},
        "software_version": _software_version(), "image_identity": None,
        "table_identity": None, "channels": [], "cycles": [], "candidates": {},
        "tissue": None, "cells": {"n": 0, "n_fail": 0, "n_warn": 0, "by_reason": {},
                                  "modules": {}},
        "receipts": [], "warnings": [], "report_paths": {}, "residual": [],
    }


def _software_version():
    try:
        from importlib.metadata import version

        return version("plexora")
    except Exception:
        return None


def active(document):
    rid = document.get("active_result_id")
    return (document.get("results") or {}).get(rid) if rid else None


def get_result(datasource, document, result_id):
    found = (document.get("results") or {}).get(result_id)
    if found is None:
        found = read_archived(datasource, result_id)
    return found


def ensure_active(document, project) -> dict:
    """The active result, or a new manual one (a user drawing QC regions
    without a session) made active."""
    found = active(document)
    if found is not None:
        return found
    result = new_result(project, strictness=copy.deepcopy(document.get("strictness")))
    result["origin"] = "manual"
    document.setdefault("results", {})[result["result_id"]] = result
    document["active_result_id"] = result["result_id"]
    return result


def put_result(document, result, *, activate=False):
    result["updated_at"] = now_iso()
    document.setdefault("results", {})[result["result_id"]] = result
    if activate:
        document["active_result_id"] = result["result_id"]
    return result


def summary(result) -> dict:
    """A result in counts: what `get_qc_results` leads with."""
    candidates = list((result or {}).get("candidates", {}).values())
    by_action = {}
    for candidate in candidates:
        if candidate.get("roi_id") and not (candidate.get("user_state") or {}).get("deleted"):
            action = candidate.get("action") or "ignore"
            by_action[action] = by_action.get(action, 0) + 1
    channels = (result or {}).get("channels") or []
    statuses = {}
    for channel in channels:
        statuses[channel.get("status")] = statuses.get(channel.get("status"), 0) + 1
    return {"result_id": (result or {}).get("result_id"),
            "session_id": (result or {}).get("session_id"),
            "regions": by_action, "channels": statuses,
            "cells": {k: ((result or {}).get("cells") or {}).get(k)
                      for k in ("n", "n_fail", "n_warn", "by_reason")},
            "strictness": (result or {}).get("strictness"),
            "warnings": len((result or {}).get("warnings") or [])}


# -- tables --------------------------------------------------------------------


def _empty(schema):
    return pl.DataFrame(schema=schema)


def get_table(datasource, name, schema=None):
    frame = _store(datasource).get_table(name)
    if frame is None:
        return _empty(schema) if schema is not None else None
    return frame


def put_table(datasource, name, frame):
    with lock(datasource):
        _store(datasource).put_table(name, frame)


def roi_meta(datasource) -> pl.DataFrame:
    return get_table(datasource, "roi_meta", ROI_META_SCHEMA)


def _row(values):
    out = {}
    for key, dtype in ROI_META_SCHEMA.items():
        value = values.get(key)
        if isinstance(dtype, pl.List) and value is None:
            value = []
        out[key] = value
    return out


def upsert_roi_meta(datasource, rows):
    """Insert or replace `roi_meta` rows by `roi_id`."""
    rows = [_row(r) for r in rows]
    if not rows:
        return
    with lock(datasource):
        current = roi_meta(datasource)
        ids = {r["roi_id"] for r in rows}
        kept = current.filter(~pl.col("roi_id").is_in(list(ids))) if current.height else current
        fresh = pl.DataFrame(rows, schema=ROI_META_SCHEMA)
        put_table(datasource, "roi_meta", pl.concat([kept, fresh], how="vertical_relaxed"))


def drop_roi_meta(datasource, roi_ids):
    with lock(datasource):
        current = roi_meta(datasource)
        if current.height:
            put_table(datasource, "roi_meta",
                      current.filter(~pl.col("roi_id").is_in(list(roi_ids))))


def cells(datasource):
    return get_table(datasource, "qc_cells")


def cell_rois(datasource):
    return get_table(datasource, "qc_cell_rois", CELL_ROIS_SCHEMA)


def put_cells(datasource, frame, pairs=None):
    with lock(datasource):
        previous = get_table(datasource, "qc_cells")
        if previous is not None:
            put_table(datasource, "qc_cells_prev", previous)
        put_table(datasource, "qc_cells", frame)
        if pairs is not None:
            put_table(datasource, "qc_cell_rois", pairs)


# -- snapshots (reset / restore) ------------------------------------------------------


def snapshot(datasource, label) -> str:
    """Everything QC holds for a project, to a file; returns its path."""
    from plexora import paths

    folder = paths.agent_root() / "snapshots" / "qc"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(datasource))
    path = folder / f"{safe}__{label}.json"
    body = {"document": load(datasource), "tables": {}}
    for name in ("roi_meta", "qc_cells", "qc_cell_rois"):
        frame = get_table(datasource, name)
        if frame is not None:
            parquet = path.with_name(f"{path.stem}.{name}.parquet")
            frame.write_parquet(parquet)
            body["tables"][name] = str(parquet)
    path.write_text(json.dumps(body, default=str), encoding="utf-8")
    return str(path)


def read_snapshot(path) -> dict:
    from pathlib import Path

    target = Path(path)
    if not target.is_file():
        raise AgentError("invalid_input", f"no QC snapshot at {path}")
    body = json.loads(target.read_text(encoding="utf-8"))
    tables = {}
    for name, parquet in (body.get("tables") or {}).items():
        if Path(parquet).is_file():
            tables[name] = pl.read_parquet(parquet)
    body["frames"] = tables
    return body


def restore_snapshot(datasource, body):
    with lock(datasource):
        document = body.get("document") or empty_document()
        _store(datasource).put_state(_canonical(document).encode("utf-8"))
        for name in ("roi_meta", "qc_cells", "qc_cell_rois"):
            frame = (body.get("frames") or {}).get(name)
            if frame is not None:
                _store(datasource).put_table(name, frame)
            else:
                schema = ROI_META_SCHEMA if name == "roi_meta" else (
                    CELL_ROIS_SCHEMA if name == "qc_cell_rois" else None)
                if schema is not None:
                    _store(datasource).put_table(name, _empty(schema))
