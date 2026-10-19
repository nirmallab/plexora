"""Run, revert, reset: what the wand, the eraser and Reset do.

A run is all or nothing. It validates the examples, builds the features,
fits, scores every eligible cell, writes the arrays, and only then swaps the
marker's run record to the new one -- so a run that is refused, fails or is
cancelled leaves the previous run exactly as it was.

The first run on a marker captures its **baseline**: which source decided
the marker before (Thresholding, with whatever gate the slider left). The
eraser goes back to it and keeps the examples; the next run captures a new
one. Thresholds are never changed by a run, so the baseline needs no copy of
the gate -- the gate list itself is what Thresholding reads.
"""

from __future__ import annotations

import secrets

import numpy as np

from plexora.plugins.gating.server.classifier import (
    MIN_EXAMPLES, UNCERTAIN_BAND, ClassifierRefused, features, learner, store)

#: Stages a run reports, in order (`progress` messages).
STAGES = ("examples", "features", "fit", "predict", "store")


def example_rows(ds, marker):
    """(rows, labels, report) for a marker's examples: their table rows,
    1/0, and which ids the table does not have."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    pos, neg = store.examples(ds.name, marker)
    ids = cells(ds).ids
    order = np.argsort(ids, kind="stable")
    sorted_ids = ids[order]

    def rows_of(values):
        values = np.asarray(values, dtype=np.int64)
        if values.size == 0 or sorted_ids.size == 0:
            return np.zeros(0, dtype=np.int64), values
        at = np.clip(np.searchsorted(sorted_ids, values), 0, sorted_ids.size - 1)
        hit = sorted_ids[at] == values
        return order[at[hit]], values[~hit]

    pos_rows, pos_missing = rows_of(pos)
    neg_rows, neg_missing = rows_of(neg)
    rows = np.concatenate([pos_rows, neg_rows])
    labels = np.concatenate([np.ones(pos_rows.size, np.int8), np.zeros(neg_rows.size, np.int8)])
    return rows, labels, {"n_positive": int(pos_rows.size), "n_negative": int(neg_rows.size),
                          "missing": sorted(int(v) for v in np.concatenate([pos_missing,
                                                                             neg_missing]))}


def check_ready(ds, marker):
    """The examples' report, or ClassifierRefused saying what is missing."""
    if marker not in ds.table.markers:
        raise ClassifierRefused(f"{marker!r} is not a marker of this project")
    rows, labels, report = example_rows(ds, marker)
    if report["n_positive"] < MIN_EXAMPLES or report["n_negative"] < MIN_EXAMPLES:
        raise ClassifierRefused(
            f"Needs {MIN_EXAMPLES} positive and {MIN_EXAMPLES} negative examples "
            f"(has {report['n_positive']} positive, {report['n_negative']} negative)")
    return rows, labels, report


def qc_state(ds, marker):
    """(not-called mask or None, fingerprint, mode) under today's QC."""
    from plexora.agent import cell_exclusions

    record = cell_exclusions.call_record(ds)
    return (cell_exclusions.not_called_mask(ds, marker, record),
            cell_exclusions.fingerprint(record), cell_exclusions.current_call_mode())


def run(ds, marker, *, progress=None, cancelled=None, pixels=True) -> dict:
    """Train on the marker's examples and call every eligible cell; the new
    run's record. `progress(done, total, message)`; `cancelled()` polled."""

    def say(stage, done=None, total=None):
        if progress is not None:
            progress(done if done is not None else STAGES.index(stage),
                     total if total is not None else len(STAGES), stage)

    def stop():
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")

    say("examples")
    rows, labels, report = check_ready(ds, marker)
    stop()
    say("features")

    def pixel_progress(done, total):
        say("features", 1 + done / max(total, 1), len(STAGES))

    matrix, names, info = features.feature_matrix(ds, marker, progress=pixel_progress,
                                                  cancelled=cancelled, pixels=pixels)
    stop()
    say("fit")
    fitted = learner.fit(matrix[rows], labels)
    stop()
    say("predict")
    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    skip, qc_fp, qc_mode = qc_state(ds, marker)
    eligible = np.isfinite(raw)
    if skip is not None and skip.shape[0] == eligible.shape[0]:
        eligible &= ~skip
    scores = np.full(raw.shape[0], np.nan, dtype=np.float32)
    picked = np.flatnonzero(eligible)
    scores[picked] = fitted.scores(matrix[picked], cancelled=cancelled)
    calls = np.full(raw.shape[0], -1, dtype=np.int8)
    calls[picked] = (scores[picked] >= 0.5).astype(np.int8)
    uncertain = np.zeros(raw.shape[0], dtype=bool)
    uncertain[picked] = np.abs(scores[picked] - 0.5) < UNCERTAIN_BAND
    # A cell a person labelled is called as they labelled it: the model is
    # for the cells nobody looked at.
    labelled = rows[eligible[rows]]
    calls[labelled] = labels[eligible[rows]]
    uncertain[labelled] = False
    stop()
    say("store")
    run_id = secrets.token_hex(6)
    record = {
        "run_id": run_id,
        "created": store.now(),
        "marker": marker,
        "n_examples_positive": report["n_positive"],
        "n_examples_negative": report["n_negative"],
        "examples_missing": report["missing"][:50],
        "model": fitted.kind,
        "calibrated": bool(fitted.calibrated),
        "score_name": "probability" if fitted.calibrated else "score",
        "cv_balanced_accuracy": (None if fitted.cv_balanced_accuracy is None
                                 else round(float(fitted.cv_balanced_accuracy), 4)),
        "notes": fitted.notes,
        "features": names,
        "feature_groups": info.get("groups"),
        "pixel_reason": info.get("pixel_reason"),
        "pixels": info.get("pixels") if isinstance(info.get("pixels"), dict) else None,
        "n_rows": int(raw.shape[0]),
        "n_eligible": int(picked.size),
        "n_positive": int((calls == 1).sum()),
        "n_negative": int((calls == 0).sum()),
        "n_uncertain": int(uncertain.sum()),
        "n_not_classified": int((calls == -1).sum()),
        "uncertain_band": UNCERTAIN_BAND,
        "table_fp": features.table_fingerprint(ds),
        "mask_fp": features.mask_fingerprint(ds),
        "qc_fp": qc_fp,
        "qc_mode": qc_mode,
    }
    store.write_arrays(ds.name, marker, run_id, calls=calls, scores=scores,
                       uncertain=uncertain)
    try:
        with store.editing(ds.name) as document:
            # Checked again under the lock the eraser takes: an eraser pressed
            # while this run was writing wins, and nothing of the run is kept.
            stop()
            entry = store.marker_state(document, marker)
            _swap_in(ds, marker, entry, record)
    except InterruptedError:
        store.run_path(ds.name, marker, run_id).unlink(missing_ok=True)
        raise
    store.remove_runs(ds.name, marker, keep=run_id)
    return record


def _swap_in(ds, marker, entry, record):
    if entry.get("baseline") is None:
        entry["baseline"] = {"active": entry.get("active") or "threshold",
                             "captured": record["created"],
                             "gate": _gate_snapshot(ds, marker)}
    entry["run"] = record
    entry["active"] = "classifier"


def _gate_snapshot(ds, marker):
    from plexora.plugins.gating.server import model

    gate = model.get_gate(ds, marker) or {}
    return {k: gate.get(k) for k in ("low", "high", "thresholded")}


def revert(ds, marker) -> dict:
    """The eraser: the marker back to its baseline, its run gone, its examples
    kept. Returns {reverted, active}."""
    cancel_running(ds.name, marker)
    with store.editing(ds.name) as document:
        entry = store.marker_state(document, marker)
        had = entry.get("run") is not None
        baseline = entry.get("baseline") or {}
        entry["active"] = baseline.get("active") or "threshold"
        if entry["active"] == "classifier":
            entry["active"] = "threshold"
        entry["run"] = None
        entry["baseline"] = None
    store.remove_runs(ds.name, marker)
    return {"reverted": had, "active": "threshold"}


def reset(ds) -> dict:
    """Reset classifier: every marker reverted and every example forgotten."""
    reverted = []
    for marker in list(store.read_state(ds.name)["markers"]):
        if revert(ds, marker)["reverted"]:
            reverted.append(marker)
    cleared = store.clear_examples(ds.name)
    return {"reverted": reverted, "examples_cleared": cleared}


#: {project: {"job_id", "marker"}} -- the classifier run in progress on each
#: project. One at a time per project: a second wand press while one runs is
#: answered with the first one's job.
_RUNNING: dict = {}


def started(project, job_id, marker):
    with store.lock(project):
        _RUNNING[project] = {"job_id": job_id, "marker": marker}


def finished(project, job_id):
    with store.lock(project):
        if (_RUNNING.get(project) or {}).get("job_id") == job_id:
            _RUNNING.pop(project, None)


def running(project) -> dict | None:
    """{"job_id", "marker"} of the run in progress on this project, or None."""
    from plexora.agent import jobs

    with store.lock(project):
        held = _RUNNING.get(project)
    if held is None:
        return None
    record = jobs.store().get(held["job_id"])
    if record is None or record.get("status") not in ("queued", "running"):
        finished(project, held["job_id"])
        return None
    return dict(held)


def cancel_running(project, marker=None):
    """Cancel the classifier run in progress on this project (only when it is
    on `marker`, when one is named)."""
    from plexora.agent import jobs

    held = running(project)
    if held is None or (marker is not None and held["marker"] != marker):
        return None
    jobs.store().cancel(held["job_id"])
    return held["job_id"]


def stale_reason(ds, record) -> str | None:
    """Why a run's calls no longer describe the project, or None."""
    if record is None:
        return None
    if record.get("table_fp") != features.table_fingerprint(ds):
        return "the table changed since this run"
    if record.get("mask_fp") != features.mask_fingerprint(ds):
        return "the segmentation mask changed since this run"
    _skip, qc_fp, _mode = qc_state(ds, record["marker"])
    if record.get("qc_fp") != qc_fp:
        return "QC changed since this run"
    return None
