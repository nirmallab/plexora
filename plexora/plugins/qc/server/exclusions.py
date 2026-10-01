"""The cells QC says must be left out of estimation (`Plugin.cell_exclusions_factory`).

What automatic gating, or anything else that fits or samples a table's values,
reads to stay off QC failures (plexora/agent/cell_exclusions.py). Answers from
the active result's per-cell calls:

- **strict**: every cell called exclude or warn, for every marker; and, for one
  marker, the cells that marker was flagged in (exclude or warn);
- **exclude**: exclude calls, and the per-marker flags with status exclude.

The calls must describe the regions as they are now. A region drawn or reshaped
in the ROI panel since the calls were derived (no `refresh_qc` yet) would
otherwise be invisible to gating, so a stale result is re-derived first -- the
same sync and derivation `refresh_qc` runs, QC's own reversible derived state,
never a source write. Staleness is the ROI document's hash against the one
stamped on the result's cells when they were derived (`calls.write_for_active`).

Nothing here removes a cell from anything: the gate that comes out still
applies to every cell.
"""

from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import numpy as np

from plexora.plugins.qc.server import results

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 16


def roi_revision(project) -> str:
    """A hash of the ROI document as stored ("" when there is none)."""
    from plexora import api

    try:
        blob = api.store(project, "roi").get_state()
    except Exception:
        return ""
    return hashlib.sha1(blob).hexdigest()[:16] if blob else ""


def _has_qc_regions(ds) -> bool:
    """Whether the ROI document holds any region in a QC category."""
    from plexora.plugins.qc.server import roi_link

    try:
        state = roi_link._repo(ds).load()
    except Exception:
        return False
    labels = roi_link._labels(state)
    return any(roi_link.class_in(f.get("category_id"), labels,
                                 roi_link.class_token(f.get("notes"))) is not None
               for f in roi_link._features(state))


def _call_for(ds):
    """What `write_for_active` needs of a capability call: a session that
    hands back this handle set."""
    return SimpleNamespace(session=SimpleNamespace(data=lambda _p: ds,
                                                   image_data=lambda _p: ds))


def _rederive(ds):
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.qc.server.cells import calls

    with results.lock(ds.name):
        document = results.load(ds.name)
        roi_link.sync(ds, document)
        if results.active(document) is not None:
            results.put_result(document, results.active(document))
            results.save(ds.name, document)
    calls.write_for_active(_call_for(ds), ds.name)


def _stale(ds, result, roi_rev) -> bool:
    cells = (result or {}).get("cells") or {}
    if "n" not in cells:
        return True
    return cells.get("roi_revision") != roi_rev


def _record(ds, mode, document, result):
    from plexora.agent import cell_exclusions

    frame = results.cells(ds.name)
    if frame is None or not frame.height:
        return None
    if "result_id" in frame.columns and frame.height and \
            frame["result_id"][0] != result.get("result_id"):
        return None
    action = frame["action"].to_numpy()
    ids = frame["cell_id"].to_numpy().astype(np.int64)
    excluded = action == "exclude"
    warned = action == "warn"
    whole_mask = excluded | warned if mode == "strict" else excluded
    statuses = ("exclude", "warn") if mode == "strict" else ("exclude",)
    by_marker: dict = {}
    if "marker_flags" in frame.columns:
        for cell_id, entries in zip(ids.tolist(), frame["marker_flags"].to_list()):
            for entry in entries or ():
                marker, _reason, status = (entry.split("|") + ["", ""])[:3]
                if status in statuses and marker:
                    by_marker.setdefault(marker, []).append(cell_id)
    regions = []
    for candidate in (result.get("candidates") or {}).values():
        user = candidate.get("user_state") or {}
        if candidate.get("roi_id") and not user.get("deleted") \
                and not user.get("removed_from_qc") \
                and (candidate.get("action") or "exclude") in ("exclude", "warn"):
            regions.append(candidate["roi_id"])
    source = {"result_id": result.get("result_id"),
              "origin": result.get("origin") or ("session" if result.get("session_id")
                                                 else "manual"),
              "n_exclude": int(excluded.sum()), "n_warn": int(warned.sum()),
              "n_marker": int(sum(len(set(v)) for v in by_marker.values())),
              "regions": len(regions)}
    fp = hashlib.sha1(f"{result.get('result_id')}|{results.revision(ds.name, document)}"
                      .encode()).hexdigest()[:16]
    return cell_exclusions.make_record(ids[whole_mask], by_marker, fingerprint=fp,
                                       mode=mode, source=source)


def _file_token(project):
    """(mtime, size) of the project's store file, which every plugin store of
    the project -- QC's and the ROI document alike -- writes to: unchanged
    means nothing QC reads has changed. None when it cannot be read."""
    from plexora.server.models import database_model

    try:
        st = database_model._db_path_for_datasource(project).stat()
    except (OSError, ValueError, AttributeError):
        return None
    return (st.st_mtime_ns, st.st_size)


_FAST: dict = {}
_ABSENT = object()
_BUSY = threading.local()


def provider(ds, mode):
    """`provider(ds, mode)` for `cell_exclusions`: None when QC was never run.

    Asked by every estimate gating makes, so the common answer -- nothing was
    written to the project since the last ask -- is a stat and a lookup."""
    if not getattr(ds.table, "available", False) or getattr(_BUSY, "on", False):
        # Asked from inside QC's own derivation (a render it draws, say): the
        # calls are being made, so there is nothing to leave out yet.
        return None
    token = _file_token(ds.name)
    if token is not None:
        with _CACHE_LOCK:
            found = _FAST.get((ds.name, mode, token), _ABSENT)
        if found is not _ABSENT:
            return found
    _BUSY.on = True
    try:
        record = _provide(ds, mode)
    finally:
        _BUSY.on = False
    after = _file_token(ds.name)
    if after is not None:
        with _CACHE_LOCK:
            if len(_FAST) > _CACHE_MAX:
                _FAST.clear()
            _FAST[(ds.name, mode, after)] = record
    return record


def _provide(ds, mode):
    roi_rev = roi_revision(ds.name)
    document = results.load(ds.name)
    result = results.active(document)
    if result is None and not _has_qc_regions(ds):
        return None
    key = (ds.name, mode, roi_rev, results.revision(ds.name, document))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    try:
        if result is None or _stale(ds, result, roi_rev):
            _rederive(ds)
            document = results.load(ds.name)
            result = results.active(document)
            if result is None:
                return None
        record = _record(ds, mode, document, result)
    except Exception as exc:
        # Remembered, so a derivation that fails is not retried by every
        # estimate of the same pass; reported in each result's qc_exclusion.
        from plexora.agent import cell_exclusions

        record = cell_exclusions.make_record([], fingerprint="error", mode=mode,
                                             source={"error": str(exc)})
        with _CACHE_LOCK:
            _CACHE[key] = record
        return record
    # Keyed by what was read before any re-derive as well as after, so the
    # next call with the same inputs is a dictionary lookup.
    after = (ds.name, mode, roi_revision(ds.name), results.revision(ds.name, document))
    with _CACHE_LOCK:
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = _CACHE[after] = record
    return record


def summary(ds, mode="strict") -> dict:
    """What `get_qc_exclusions` reports: counts by reason and marker, never ids."""
    from plexora.agent import cell_exclusions

    with cell_exclusions.mode(mode):
        record = provider(ds, mode) if mode != "off" else None
        if record is None:
            return {"mode": mode, "applied": False,
                    "reason": "qc='off'" if mode == "off" else "no QC result for this image"}
        block = cell_exclusions.describe(ds, record)
    frame = results.cells(ds.name)
    by_reason: dict = {}
    if frame is not None and frame.height:
        left = frame.filter(frame["action"].is_in(
            ["exclude", "warn"] if mode == "strict" else ["exclude"]))
        for reasons in left["reasons"].to_list():
            for reason in reasons or ():
                by_reason[reason] = by_reason.get(reason, 0) + 1
    block["by_reason"] = dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))
    block["by_marker"] = {m: int(v.size) for m, v in sorted(record.by_marker.items())}
    return block


def factory():
    return provider
