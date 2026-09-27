"""Where a gate came from, beside the gate itself.

The gate's VALUE stays where it always was -- the pickled row list the sidebar
reads and autosaves whole -- because anything added to those rows would be
dropped by the browser's next save. What a gate's value cannot say lives here,
in a sidecar table of this plugin's store (`plugin_gating_provenance`), one
current row per marker: how it was set (GMM accepted, refined by an agent,
transferred from a reference image, set by hand), how confident the run was,
what flagged it, which decisions and evidence it rests on -- and its status,
which is the one part that constrains writers:

- `locked`   -- nothing but the user changes it; the sidebar's own save is
               reverted for it (`merge_locked`).
- `approved` -- the user signed it off; agents may not overwrite it (a run
               stores a `proposed` value beside it instead).
- `excluded` -- automatic runs skip the marker.
- `proposed` / `accepted` -- ordinary states a run leaves.

`value_written_*` is what the agent wrote; a stored gate that no longer equals
it was changed in the sidebar since (`edited_since`), and a run never
overwrites such a gate without `overwrite_manual`.
"""

from __future__ import annotations

import hashlib
import json
import threading

import polars as pl

STATUSES = ("proposed", "accepted", "approved", "locked", "excluded")
METHODS = ("gmm", "ai_accepted", "ai_refined", "transfer_aligned", "agent_set", "manual",
           "imported", "rolled_back")
#: Statuses that refuse an agent's write.
PROTECTED = ("locked", "approved")

TABLE = "provenance"

COLUMNS = {
    "marker": pl.Utf8, "status": pl.Utf8, "method": pl.Utf8, "tier": pl.Utf8,
    "confidence": pl.Utf8, "state": pl.Utf8, "value_low": pl.Float64,
    "value_high": pl.Float64, "written_low": pl.Float64, "written_high": pl.Float64,
    "gmm_proposal": pl.Float64, "proposed_low": pl.Float64, "delta_fit": pl.Float64,
    "delta_fraction": pl.Float64, "refinement_rounds": pl.Int64, "session_id": pl.Utf8,
    "operation_id": pl.Utf8, "timestamp": pl.Utf8, "principal": pl.Utf8, "note": pl.Utf8,
    "detail": pl.Utf8,
}

_LOCKS: dict = {}
_GUARD = threading.Lock()


def _lock(name):
    with _GUARD:
        return _LOCKS.setdefault(name, threading.RLock())


def _store(name):
    from plexora import api

    return api.store(name, "gating")


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def read(name) -> dict:
    """{marker: row} with `detail` decoded."""
    frame = _store(name).get_table(TABLE)
    if frame is None or frame.height == 0:
        return {}
    out = {}
    for row in frame.iter_rows(named=True):
        row = dict(row)
        try:
            row["detail"] = json.loads(row.get("detail") or "{}")
        except ValueError:
            row["detail"] = {}
        out[row["marker"]] = row
    return out


def _canonical(rows):
    return json.dumps([rows[k] for k in sorted(rows)], sort_keys=True, default=str)


def revision(name) -> str:
    rows = read(name)
    if not rows:
        return "0"
    return hashlib.sha1(_canonical(rows).encode("utf-8")).hexdigest()[:16]


def _write(name, rows):
    records = []
    for marker in sorted(rows):
        row = dict(rows[marker])
        row["detail"] = json.dumps(row.get("detail") or {}, sort_keys=True, default=str)
        records.append({key: row.get(key) for key in COLUMNS})
    frame = pl.DataFrame(records, schema=COLUMNS, orient="row") if records else \
        pl.DataFrame(schema=COLUMNS)
    _store(name).put_table(TABLE, frame)


def status_of(name, marker) -> str | None:
    return (read(name).get(marker) or {}).get("status")


def record(name, marker, **fields) -> dict:
    """Upsert one marker's row; returns the row. `detail` is merged."""
    with _lock(name):
        rows = read(name)
        row = dict(rows.get(marker) or {"marker": marker})
        detail = dict(row.get("detail") or {})
        detail.update(fields.pop("detail", None) or {})
        row.update({k: v for k, v in fields.items() if k in COLUMNS})
        row["detail"] = detail
        row.setdefault("timestamp", _now())
        if "timestamp" not in fields:
            row["timestamp"] = _now()
        if row.get("status") is not None and row["status"] not in STATUSES:
            raise ValueError(f"status is one of {STATUSES}")
        rows[marker] = row
        _write(name, rows)
        return row


def set_status(name, marker, status, *, note=None, principal=None, current=None) -> tuple:
    """(before, after) status. `status` may also be `unlocked`/`included`,
    which return a locked/excluded marker to `accepted` (or `proposed` when it
    was never accepted)."""
    with _lock(name):
        rows = read(name)
        row = dict(rows.get(marker) or {"marker": marker, "detail": {}})
        before = row.get("status")
        if status in ("unlocked", "included", "unapproved"):
            if before not in ("locked", "excluded", "approved"):
                return before, before
            status = "accepted" if row.get("method") not in (None, "manual") else "proposed"
        if status not in STATUSES:
            raise ValueError(f"status is one of {STATUSES + ('unlocked', 'included')}")
        row["status"] = status
        if status in ("locked", "approved") and current is not None:
            # What the lock protects is the value on screen when it was set.
            row["written_low"], row["written_high"] = current
            row["value_low"], row["value_high"] = current
            if row.get("method") is None:
                row["method"] = "manual"
        if note is not None:
            row["note"] = note
        if principal is not None:
            row["principal"] = principal
        row["timestamp"] = _now()
        rows[marker] = row
        _write(name, rows)
        return before, status


def mark_rolled_back(name, marker, *, operation_id=None):
    with _lock(name):
        rows = read(name)
        row = rows.get(marker)
        if row is None:
            return None
        row = dict(row)
        row["method"] = "rolled_back"
        row["status"] = "proposed"
        detail = dict(row.get("detail") or {})
        detail["rolled_back_by"] = operation_id
        row["detail"] = detail
        row["timestamp"] = _now()
        rows[marker] = row
        _write(name, rows)
        return row


def protected(name) -> dict:
    """{marker: status} for every marker an agent may not write."""
    return {m: r["status"] for m, r in read(name).items() if r.get("status") in PROTECTED}


def excluded(name) -> set:
    return {m for m, r in read(name).items() if r.get("status") == "excluded"}


def edited_since(row, gate) -> bool:
    """Whether a stored gate differs from what the agent last wrote for it."""
    if not row or gate is None or row.get("written_low") is None:
        return False
    if row.get("method") in ("manual", "rolled_back"):
        return False
    low, high = gate.get("low"), gate.get("high")
    return not (_same(low, row.get("written_low")) and _same(high, row.get("written_high")))


def _same(a, b):
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))


def summary(name, gates) -> dict:
    """{marker: {method, status, confidence, state, edited_since[, proposed_low]}}
    for the markers with a provenance row -- what `get_all_gates` adds per
    gate. `proposed_low` is a threshold a session reached but did not write."""
    rows = read(name)
    by_marker = {g["marker"]: g for g in gates or []}
    out = {}
    for marker, row in rows.items():
        out[marker] = {"method": row.get("method"), "status": row.get("status"),
                       "confidence": row.get("confidence"), "state": row.get("state"),
                       "session_id": row.get("session_id"),
                       "edited_since": edited_since(row, by_marker.get(marker))}
        if row.get("status") == "proposed" and row.get("proposed_low") is not None:
            out[marker]["proposed_low"] = row["proposed_low"]
    return out


def merge_locked(name, saved_rows) -> tuple:
    """(rows, reverted markers): the sidebar's saved rows with every locked
    marker's range put back to its locked value."""
    locked = {m: r for m, r in read(name).items() if r.get("status") == "locked"
              and r.get("written_low") is not None}
    if not locked:
        return saved_rows, []
    reverted = []
    out = []
    for row in saved_rows:
        row = dict(row)
        lock = locked.get(row.get("channel"))
        if lock is not None:
            low, high = lock["written_low"], lock["written_high"]
            if not (_same(row.get("gate_start"), low) and _same(row.get("gate_end"), high)):
                row["gate_start"], row["gate_end"] = low, high
                reverted.append(row.get("channel"))
        out.append(row)
    return out, reverted
