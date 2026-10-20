"""Which source decides a marker, and what it calls each cell.

The one seam every consumer reads through -- the panel's counts, the agent's
summary, the viewer's colours, the source-file write, the SCIMAP Pro hand-off
-- so a marker the classifier decides is reported the same way everywhere,
and a marker it does not is exactly what Thresholding always said.

A run's stored calls are re-masked with TODAY's QC on every read: a cell QC
has since ruled out is not called, whatever the run said, as with a gate.

A gating session's layer (`runs.session_run`) is read here too
(`session_arrays`), but never decides a marker until it is adopted: the
threshold and the cell calls stay two things, and where they part ways --
a positive below the gate, a negative above it -- is drawn and exported as
such (`viewer_codes`, codes 4 and 5).
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.classifier import (
    VIEW_ADDED, VIEW_NEGATIVE, VIEW_NOT_CLASSIFIED, VIEW_POSITIVE, VIEW_REMOVED, VIEW_UNCERTAIN,
    runs, store)

#: Which calls the viewer draws (`viewer_codes`): the run's own, or the
#: classifier's 0.5 call before a rule weighed it against the gate.
VIEWS = ("integrated", "raw")


def active_source(ds, marker) -> str:
    return store.active(ds.name, marker)


def classified_markers(ds) -> list:
    """Markers the classifier decides right now, in table order."""
    document = store.read_state(ds.name)
    return [m for m in ds.table.markers
            if (document["markers"].get(m) or {}).get("active") == "classifier"
            and (document["markers"].get(m) or {}).get("run")]


def markers_with_runs(ds) -> dict:
    """{marker: {"active", "run", "session_run"}} for every marker with a run
    or a session's layer, in table order; `run`/`session_run` are run ids."""
    document = store.read_state(ds.name)
    out = {}
    for marker in ds.table.markers:
        entry = document["markers"].get(marker) or {}
        run, session = entry.get("run"), entry.get("session_run")
        if not run and not session:
            continue
        out[marker] = {"active": "classifier" if entry.get("active") == "classifier" and run
                       else "threshold",
                       "run": (run or {}).get("run_id"),
                       "session_run": (session or {}).get("run_id"),
                       "session_id": (session or run or {}).get("session_id")}
    return out


def _arrays_for(ds, marker, record):
    if record is None or record.get("status", "ready") != "ready":
        return record, None
    arrays = store.read_arrays(ds.name, marker, record["run_id"])
    n_rows = int(ds.table.geometry().height) if ds.table.geometry() is not None else 0
    if arrays is None or arrays["calls"].shape[0] != n_rows:
        return record, None
    if record.get("table_fp") and runs.features.table_fingerprint(ds) != record["table_fp"]:
        return record, None
    skip, _fp, _mode = runs.qc_state(ds, marker)
    out = {name: (None if arrays.get(name) is None else arrays[name].copy())
           for name in ("calls", "uncertain", "raw_calls", "gate_calls")}
    if skip is not None and skip.shape[0] == out["calls"].shape[0]:
        for name in ("calls", "raw_calls", "gate_calls"):
            if out[name] is not None:
                out[name][skip] = -1
        out["uncertain"][skip] = False
    out["scores"] = arrays["scores"]
    out["agreement"] = arrays.get("agreement")
    return record, out


def run_arrays(ds, marker):
    """(record, arrays) of the marker's run with today's QC applied, or
    (record, None) when its arrays no longer line up with the table (a
    reloaded table with other rows), or (None, None) without a run."""
    return _arrays_for(ds, marker, store.run_record(ds.name, marker))


def session_arrays(ds, marker):
    """(record, arrays) of the session's layer for the marker, as `run_arrays`."""
    return _arrays_for(ds, marker, store.session_run_record(ds.name, marker))


def shown_arrays(ds, marker):
    """(record, arrays, which) the panel draws: the run that decides the
    marker; else a session's layer; else the last panel run."""
    if active_source(ds, marker) == "classifier":
        record, arrays = run_arrays(ds, marker)
        return record, arrays, "run"
    record, arrays = session_arrays(ds, marker)
    if record is not None:
        return record, arrays, "session_run"
    record, arrays = run_arrays(ds, marker)
    return record, arrays, "run"


def gate_calls_for(ds, marker, gate):
    """int8 1/0/-1 per row at `gate` ((low, high), table units) under
    today's QC."""
    from plexora.agent import cell_exclusions

    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    low, high = gate
    return cell_exclusions.calls(ds, marker, raw, low, high)


def marker_calls(ds, marker):
    """(int8 row-aligned 1/0/-1 or None, source). Thresholding's calls are
    `cell_exclusions.calls` on the stored gate (None when the marker was
    never gated)."""
    if active_source(ds, marker) == "classifier":
        _record, arrays = run_arrays(ds, marker)
        if arrays is not None:
            return arrays["calls"], "classifier"
    from plexora.plugins.gating.server import model

    gate = model.active_gates(ds).get(marker)
    if gate is None:
        return None, "threshold"
    return gate_calls_for(ds, marker, gate), "threshold"


def _gate_of(ds, marker, record, arrays):
    """The gate calls a run is compared with: the ones it stored, else its
    gate's under today's QC; None for a run made at no gate."""
    if arrays is not None and arrays.get("gate_calls") is not None:
        return arrays["gate_calls"]
    used = (record or {}).get("gate_used")
    if used and used[0] is not None:
        return gate_calls_for(ds, marker, used)
    return None


def viewer_codes(ds, marker, view="integrated"):
    """(uint8 per row, headers) for the panel: 0 negative, 1 positive,
    2 not classified, 3 uncertain, and for a run made at a gate 4 positive
    below it and 5 negative above it. `view="raw"` draws the classifier's
    own 0.5 call where the run kept one."""
    n = int(ds.table.geometry().height) if ds.table.geometry() is not None else 0
    out = np.full(n, VIEW_NOT_CLASSIFIED, dtype=np.uint8)
    record, arrays, which = shown_arrays(ds, marker)
    state = "not_run" if record is None else ("stale" if arrays is None else "ready")
    counts = {"positive": 0, "eligible": 0, "uncertain": 0, "not_classified": n,
              "added": 0, "removed": 0}
    if arrays is not None:
        calls = arrays["calls"]
        if view == "raw" and arrays.get("raw_calls") is not None:
            calls = arrays["raw_calls"]
        out[calls == 1] = VIEW_POSITIVE
        out[calls == 0] = VIEW_NEGATIVE
        out[arrays["uncertain"] & (calls >= 0)] = VIEW_UNCERTAIN
        gate = _gate_of(ds, marker, record, arrays)
        added = removed = np.zeros(n, dtype=bool)
        if gate is not None and gate.shape[0] == n:
            added = (calls == 1) & (gate == 0)
            removed = (calls == 0) & (gate == 1)
            out[added] = VIEW_ADDED
            out[removed] = VIEW_REMOVED
        counts = {"positive": int((calls == 1).sum()), "eligible": int((calls >= 0).sum()),
                  "uncertain": int((arrays["uncertain"] & (calls >= 0)).sum()),
                  "not_classified": int((calls < 0).sum()),
                  "added": int(added.sum()), "removed": int(removed.sum())}
    return out, {"state": state, "run": record.get("run_id", "") if record else "",
                 "which": which, "view": view, **counts}


def compare(ds, marker, which="session_run") -> dict | None:
    """Where a run's calls and its gate's part ways, counted under today's
    QC, with the gate it was made at and whether that gate still stands."""
    from plexora.plugins.gating.server.classifier import rules

    record, arrays = (session_arrays if which == "session_run" else run_arrays)(ds, marker)
    if record is None or arrays is None:
        return None
    gate = _gate_of(ds, marker, record, arrays)
    if gate is None:
        return None
    out = rules.compare(arrays["calls"], gate, arrays.get("raw_calls"))
    out.update({"gate_used": record.get("gate_used"), "stale": runs.stale_reason(ds, record)})
    return out


def summary(ds, marker) -> dict | None:
    """`model.gated_summary`'s shape for a classifier-decided marker, plus
    `source`, `n_uncertain` and the run; None when Thresholding decides."""
    if active_source(ds, marker) != "classifier":
        return None
    record, arrays = run_arrays(ds, marker)
    if arrays is None:
        return None
    from plexora.agent import cell_exclusions

    calls = arrays["calls"]
    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    n_called = int((calls >= 0).sum())
    n_positive = int((calls == 1).sum())
    skip = cell_exclusions.not_called_mask(ds, marker)
    return {
        "marker": marker,
        "source": "classifier",
        "low": None,
        "high": None,
        "n_cells": int(calls.size),
        "n_finite": int(np.isfinite(raw).sum()),
        "n_positive": n_positive,
        "n_negative": int((calls == 0).sum()),
        "n_not_called": int(skip.sum()) if skip is not None else 0,
        "n_not_classified": int((calls < 0).sum()),
        "n_uncertain": int((arrays["uncertain"] & (calls >= 0)).sum()),
        "call_mode": cell_exclusions.current_call_mode(),
        "fraction": (n_positive / n_called) if n_called else None,
        "classifier": public_run(record),
        "stale": runs.stale_reason(ds, record),
    }


def public_run(record):
    if record is None:
        return None
    keep = ("run_id", "created", "model", "calibrated", "score_name",
            "cv_balanced_accuracy", "n_examples_positive", "n_examples_negative",
            "n_examples_given", "feature_groups", "pixel_reason", "n_eligible", "n_positive", "n_uncertain",
            "n_not_classified", "qc_mode", "uncertain_rule", "auto_pick", "notes",
            "origin", "session_id", "gate_used", "gate_source", "call_rule", "c_add",
            "c_clean", "c_add_used", "snr", "compare", "status", "why", "push", "versions")
    out = {k: record.get(k) for k in keep if k in record or k in keep[:19]}
    out.setdefault("origin", "panel")
    return out
