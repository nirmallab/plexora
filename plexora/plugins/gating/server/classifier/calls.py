"""Which source decides a marker, and what it calls each cell.

The one seam every consumer reads through -- the panel's counts, the agent's
summary, the viewer's colours, the source-file write, the SCIMAP Pro hand-off
-- so a marker the classifier decides is reported the same way everywhere,
and a marker it does not is exactly what Thresholding always said.

A run's stored calls are re-masked with TODAY's QC on every read: a cell QC
has since ruled out is not called, whatever the run said, as with a gate.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.classifier import (
    VIEW_NEGATIVE, VIEW_NOT_CLASSIFIED, VIEW_POSITIVE, VIEW_UNCERTAIN, runs, store)


def active_source(ds, marker) -> str:
    return store.active(ds.name, marker)


def classified_markers(ds) -> list:
    """Markers the classifier decides right now, in table order."""
    document = store.read_state(ds.name)
    return [m for m in ds.table.markers
            if (document["markers"].get(m) or {}).get("active") == "classifier"
            and (document["markers"].get(m) or {}).get("run")]


def run_arrays(ds, marker):
    """(record, arrays) of the marker's run with today's QC applied, or
    (record, None) when its arrays no longer line up with the table (a
    reloaded table with other rows), or (None, None) without a run."""
    record = store.run_record(ds.name, marker)
    if record is None:
        return None, None
    arrays = store.read_arrays(ds.name, marker, record["run_id"])
    n_rows = int(ds.table.geometry().height) if ds.table.geometry() is not None else 0
    if arrays is None or arrays["calls"].shape[0] != n_rows:
        return record, None
    if record.get("table_fp") and runs.features.table_fingerprint(ds) != record["table_fp"]:
        return record, None
    skip, _fp, _mode = runs.qc_state(ds, marker)
    calls = arrays["calls"].copy()
    uncertain = arrays["uncertain"].copy()
    if skip is not None and skip.shape[0] == calls.shape[0]:
        calls[skip] = -1
        uncertain[skip] = False
    arrays = {"calls": calls, "scores": arrays["scores"], "uncertain": uncertain,
              "agreement": arrays.get("agreement")}
    return record, arrays


def marker_calls(ds, marker):
    """(int8 row-aligned 1/0/-1 or None, source). Thresholding's calls are
    `cell_exclusions.calls` on the stored gate (None when the marker was
    never gated)."""
    if active_source(ds, marker) == "classifier":
        _record, arrays = run_arrays(ds, marker)
        if arrays is not None:
            return arrays["calls"], "classifier"
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server import model

    gate = model.active_gates(ds).get(marker)
    if gate is None:
        return None, "threshold"
    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    return cell_exclusions.calls(ds, marker, raw, gate[0], gate[1]), "threshold"


def viewer_codes(ds, marker):
    """(uint8 per row, headers) for the panel: 0 negative, 1 positive,
    2 not classified, 3 uncertain."""
    n = int(ds.table.geometry().height) if ds.table.geometry() is not None else 0
    out = np.full(n, VIEW_NOT_CLASSIFIED, dtype=np.uint8)
    record, arrays = run_arrays(ds, marker)
    state = "not_run" if record is None else ("stale" if arrays is None else "ready")
    counts = {"positive": 0, "eligible": 0, "uncertain": 0, "not_classified": n}
    if arrays is not None:
        calls = arrays["calls"]
        out[calls == 1] = VIEW_POSITIVE
        out[calls == 0] = VIEW_NEGATIVE
        out[arrays["uncertain"] & (calls >= 0)] = VIEW_UNCERTAIN
        counts = {"positive": int((calls == 1).sum()), "eligible": int((calls >= 0).sum()),
                  "uncertain": int((arrays["uncertain"] & (calls >= 0)).sum()),
                  "not_classified": int((calls < 0).sum())}
    return out, {"state": state, "run": record["run_id"] if record else "", **counts}


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
            "feature_groups", "pixel_reason", "n_eligible", "n_positive", "n_uncertain",
            "n_not_classified", "qc_mode", "uncertain_rule", "auto_pick", "notes")
    return {k: record.get(k) for k in keep}
