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

#: `geometry_hash` is the region as it was read (`roi_link.membership_hash`):
#: a pair whose region still hashes the same is kept, not read again.
CELL_ROIS_SCHEMA = {"cell_id": pl.Int64, "roi_id": pl.Utf8, "fraction": pl.Float32,
                    "method": pl.Utf8, "geometry_hash": pl.Utf8}

_LOCKS: dict = {}
_GUARD = threading.Lock()


class _ProjectLock:
    """A re-entrant lock that also holds an advisory lock on the store's
    folder while it is held: a server and a headless `plexora ai run qc` on
    the same project are two processes, and the thread lock alone let one
    overwrite the other's document."""

    def __init__(self, datasource):
        self._datasource = str(datasource)
        self._lock = threading.RLock()
        self._depth = 0
        self._handle = None

    def _path(self):
        try:
            return _store(self._datasource).directory() / "qc.lock"
        except Exception:  # no store folder (a test double): the thread lock alone
            return None

    def _take_file(self):
        path = self._path()
        if path is None:
            return
        try:
            handle = open(path, "a+b")
        except OSError:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            elif _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        except OSError:
            handle.close()
            return
        self._handle = handle

    def _drop_file(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()

    def acquire(self, blocking=True, timeout=-1):
        if not self._lock.acquire(blocking, timeout):
            return False
        self._depth += 1
        if self._depth == 1:
            self._take_file()
        return True

    def release(self):
        if self._depth == 1:
            self._drop_file()
        self._depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


try:
    import fcntl as _fcntl
    _msvcrt = None
except ImportError:  # Windows
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None


def lock(datasource):
    """The read-modify-write lock of one project's QC store (re-entrant, and
    held across processes for its outermost hold)."""
    with _GUARD:
        return _LOCKS.setdefault(str(datasource), _ProjectLock(datasource))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _store(datasource):
    from plexora import api

    return api.store(datasource, PLUGIN_NAME)


# -- the document ----------------------------------------------------------------


def empty_document() -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision_seq": 0, "active_result_id": None,
            "results": {}, "archived": [],
            "strictness": {"preset": "standard", "thresholds": None},
            # Categories whose findings stay recorded but leave no cell out
            # (`set_qc_categories`): reversible, nothing is deleted.
            "exclusion_filter": {"disabled_categories": [], "disabled_reasons": []},
            # Where the cells' calls were written into the user's own files
            # (`source_write`): what the SCIMAP Pro hand-off may point at.
            "source_writes": []}


def disabled_categories(document) -> list:
    """The categories switched off for exclusions, in the order listed."""
    raw = (document.get("exclusion_filter") or {}).get("disabled_categories") or []
    return [str(c) for c in raw]


#: How many source writes the ledger keeps.
KEEP_SOURCE_WRITES = 20


def record_source_write(document, entry):
    """Note one write of the calls into the user's file (newest last)."""
    ledger = [e for e in document.get("source_writes") or []
              if not (e.get("path") == entry.get("path")
                      and e.get("image_id") == entry.get("image_id"))]
    ledger.append({**entry, "at": entry.get("at") or now_iso()})
    document["source_writes"] = ledger[-KEEP_SOURCE_WRITES:]
    return document


def last_source_write(document, result_id=None):
    """The newest ledger entry (for `result_id`, when given), or None."""
    for entry in reversed(document.get("source_writes") or []):
        if result_id is None or entry.get("result_id") == result_id:
            return entry
    return None


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


def set_reason_color(datasource, reason, color=None):
    """The colour a cell reason is drawn in, kept with the project's QC store
    (every result, not one); `None` puts back the default."""
    with lock(datasource):
        document = load(datasource)
        colors = dict((document.get("colors") or {}).get("reasons") or {})
        if color:
            colors[reason] = color
        else:
            colors.pop(reason, None)
        document["colors"] = {**(document.get("colors") or {}), "reasons": colors}
        save(datasource, document)


def reason_colors(datasource) -> dict:
    return dict(((load(datasource).get("colors") or {}).get("reasons")) or {})


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
        "tissue": None, "cells": {"n": 0, "n_fail": 0, "n_warn": 0, "by_reason": {}},
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


#: A result QC's model-free checks made on their own (`run_basic_qc`, which a
#: gating session runs on an image with no QC): nobody reviewed it.
BASIC_ORIGIN = "basic"


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
                      for k in ("n", "n_fail", "n_warn", "n_noted", "n_background",
                                "by_reason", "n_marker_flagged", "n_marker_unreliable")},
            "strictness": (result or {}).get("strictness"),
            "checks": {check: {key: {k: entry.get(k) for k in (
                "threshold", "threshold_source", "offset_steps", "state", "n_regions",
                "flagged_pct", "denominator")} for key, entry in per.items()}
                for check, per in ((result or {}).get("checks") or {}).items()},
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


#: Where on the scan grid each unconfirmed staining detection was
#: (`candidates.staining_hints`), per result: the cells' calls test the cells
#: under it (`cells.calls.derive(hints=...)`). A table, never the result JSON.
SCAN_HINTS_SCHEMA = {"result_id": pl.Utf8, "channel": pl.Utf8, "class": pl.Utf8,
                     "grid_ny": pl.Int64, "grid_nx": pl.Int64, "cell_full_px": pl.Float64,
                     "cells": pl.List(pl.Int64)}


def put_scan_hints(datasource, result_id, grid, hints):
    """Store `hints` ({channel: {class: {cells, ...}}}) for one result,
    replacing that result's rows and keeping other results'."""
    ny, nx = (int(v) for v in grid["shape"])
    rows = [{"result_id": str(result_id), "channel": str(channel), "class": str(klass),
             "grid_ny": ny, "grid_nx": nx, "cell_full_px": float(grid["cell_full_px"]),
             "cells": [int(c) for c in entry.get("cells", ())]}
            for channel, classes in sorted(hints.items())
            for klass, entry in sorted(classes.items())]
    fresh = pl.DataFrame(rows, schema=SCAN_HINTS_SCHEMA) if rows else _empty(SCAN_HINTS_SCHEMA)
    with lock(datasource):
        kept = get_table(datasource, "qc_scan_hints", SCAN_HINTS_SCHEMA)
        kept = kept.filter(pl.col("result_id") != str(result_id)) if kept.height else kept
        put_table(datasource, "qc_scan_hints",
                  pl.concat([kept, fresh], how="vertical_relaxed"))


def scan_hints(datasource, result_id) -> list:
    """[{channel, class, grid_ny, grid_nx, cell_full_px, cells}] for one result."""
    frame = get_table(datasource, "qc_scan_hints", SCAN_HINTS_SCHEMA)
    if not frame.height:
        return []
    return frame.filter(pl.col("result_id") == str(result_id)).to_dicts()


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


#: The tables a reset snapshots and a restore puts back.
SNAPSHOT_TABLES = ("roi_meta", "qc_cells", "qc_cell_rois", "qc_scan_hints")


def snapshot(datasource, label) -> str:
    """Everything QC holds for a project, to a file; returns its path."""
    from plexora import paths

    folder = paths.agent_root() / "snapshots" / "qc"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(datasource))
    path = folder / f"{safe}__{label}.json"
    body = {"document": load(datasource), "tables": {}}
    for name in SNAPSHOT_TABLES:
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
        for name in SNAPSHOT_TABLES:
            frame = (body.get("frames") or {}).get(name)
            if frame is not None:
                _store(datasource).put_table(name, frame)
            else:
                schema = {"roi_meta": ROI_META_SCHEMA, "qc_cell_rois": CELL_ROIS_SCHEMA,
                          "qc_scan_hints": SCAN_HINTS_SCHEMA}.get(name)
                if schema is not None:
                    _store(datasource).put_table(name, _empty(schema))
