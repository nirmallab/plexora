"""Run, revert, reset: what the wand, the eraser and Reset do -- and the layer
a gating session makes at its gate.

A run is all or nothing. It validates the examples, builds the features,
fits, scores every eligible cell, writes the arrays, and only then swaps the
marker's run record to the new one -- so a run that is refused, fails or is
cancelled leaves the previous run exactly as it was.

The first run on a marker captures its **baseline**: which source decided
the marker before (Thresholding, with whatever gate the slider left). The
eraser goes back to it and keeps the examples; the next run captures a new
one. Thresholds are never changed by a run, so the baseline needs no copy of
the gate -- the gate list itself is what Thresholding reads.

**A session's layer** (`session_run`) is a run made by automatic gating at
the gate it just placed, kept in a slot of its own (`session_run` in the
marker's state): it never decides the marker -- the threshold does -- until
a person adopts it (`adopt_session`). Its picks are made at that gate
(`autopick.pick(gate=...)`) and never from the bands just either side of it,
which are what it is asked about; it trains on them and on the examples a
person gave, and calls each cell by the gate unless it is confident the
other way (`rules.apply`), keeping its own 0.5 call beside it.
"""

from __future__ import annotations

import secrets

import numpy as np

from plexora.plugins.gating.server.classifier import (
    MIN_EXAMPLES, UNCERTAIN_DISAGREEMENT, ClassifierRefused, autopick, features, learner,
    rules, signal, store)

#: Stages a run reports (`progress` messages). "pick" only when the wand
#: chooses the examples itself, and then after "features", because its picks
#: read the pixels; Train checks the examples first, so that a refusal comes
#: before any pixel is read.
STAGES = ("features", "pick", "examples", "fit", "predict", "store")

#: What one of the wand's examples weighs against one a person gave.
AUTO_WEIGHT = 0.5

#: Origins of examples a person gave (weight 1, and they count toward
#: calibration). Everything else -- the wand's picks ("auto"), an agent's
#: labels ("agent") -- weighs AUTO_WEIGHT: an agent's word about a cell is
#: not a person's.
PERSON_ORIGINS = ("click", "import")

#: Where a run came from (`origin` on its record).
ORIGINS = ("panel", "agent", "session")


def versions() -> dict:
    """What a run's calls were made with, for its record and the memo."""
    from plexora.server.utils.cell_features import FEATURE_VERSION

    return {"feature": FEATURE_VERSION, "table": features.TABLE_VERSION,
            "autopick": autopick.VERSION, "learner": learner.VERSION, "c": learner.C,
            "rules": rules.VERSION}


def _rows_of(ds, values):
    from plexora.plugins.gating.server.classifier.geometry import cells

    ids = cells(ds).ids
    order = np.argsort(ids, kind="stable")
    sorted_ids = ids[order]
    values = np.asarray(values, dtype=np.int64)
    if values.size == 0 or sorted_ids.size == 0:
        return np.zeros(0, dtype=np.int64), values
    at = np.clip(np.searchsorted(sorted_ids, values), 0, sorted_ids.size - 1)
    hit = sorted_ids[at] == values
    return order[at[hit]], values[~hit]


def example_rows(ds, marker):
    """(rows, labels, report) for a marker's examples: their table rows,
    1/0, and which ids the table does not have."""
    pos, neg = store.examples(ds.name, marker)
    pos_rows, pos_missing = _rows_of(ds, pos)
    neg_rows, neg_missing = _rows_of(ds, neg)
    rows = np.concatenate([pos_rows, neg_rows])
    labels = np.concatenate([np.ones(pos_rows.size, np.int8), np.zeros(neg_rows.size, np.int8)])
    return rows, labels, {"n_positive": int(pos_rows.size), "n_negative": int(neg_rows.size),
                          "missing": sorted(int(v) for v in np.concatenate([pos_missing,
                                                                             neg_missing]))}


def example_weights(ds, marker, rows):
    """(one weight per example row, how many a person gave): 1 for a
    person's (`PERSON_ORIGINS`), AUTO_WEIGHT for the rest."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    origin = store.origins(ds.name, marker)
    ids = cells(ds).ids[rows]
    person = np.asarray([origin.get(int(v), "click") in PERSON_ORIGINS for v in ids],
                        dtype=bool)
    return np.where(person, 1.0, AUTO_WEIGHT), int(person.sum())


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


def _score_all(ds, marker, matrix, fitted, cancelled=None):
    """(raw values, eligible, scores, agreement, raw calls, uncertain) over
    every row: NaN / -1 / False where the cell is not classified."""
    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    skip, _qc_fp, _qc_mode = qc_state(ds, marker)
    eligible = np.isfinite(raw)
    if skip is not None and skip.shape[0] == eligible.shape[0]:
        eligible &= ~skip
    scores = np.full(raw.shape[0], np.nan, dtype=np.float32)
    agreement = np.full(raw.shape[0], np.nan, dtype=np.float32)
    called = np.flatnonzero(eligible)
    scores[called], agreement[called] = fitted.scores(matrix[called], cancelled=cancelled)
    calls = np.full(raw.shape[0], -1, dtype=np.int8)
    calls[called] = (scores[called] >= 0.5).astype(np.int8)
    uncertain = np.zeros(raw.shape[0], dtype=bool)
    uncertain[called] = learner.uncertain(agreement[called])
    return raw, eligible, scores, agreement, calls, uncertain


def _base_record(ds, marker, run_id, fitted, names, info, report, n_given, calls, uncertain,
                 eligible):
    _skip, qc_fp, qc_mode = qc_state(ds, marker)
    return {
        "run_id": run_id,
        "created": store.now(),
        "marker": marker,
        "n_examples_positive": report["n_positive"],
        "n_examples_negative": report["n_negative"],
        "n_examples_given": n_given,
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
        "n_rows": int(calls.shape[0]),
        "n_eligible": int(eligible.sum()),
        "n_positive": int((calls == 1).sum()),
        "n_negative": int((calls == 0).sum()),
        "n_uncertain": int(uncertain.sum()),
        "n_not_classified": int((calls == -1).sum()),
        "uncertain_rule": (f"at least {round(UNCERTAIN_DISAGREEMENT * 100)}% of "
                           f"{learner.RESAMPLES_TREES if fitted.kind == 'gradient_boosting' else learner.RESAMPLES} "
                           "models fitted to resampled examples call it the other way"),
        "table_fp": features.table_fingerprint(ds),
        "mask_fp": features.mask_fingerprint(ds),
        "qc_fp": qc_fp,
        "qc_mode": qc_mode,
        "versions": versions(),
    }


def run(ds, marker, *, progress=None, cancelled=None, pixels=True, auto_pick=False,
        origin="panel") -> dict:
    """Train on the marker's examples and call every eligible cell; the new
    run's record. `progress(done, total, message)`; `cancelled()` polled.

    `auto_pick`: the wand. Positives and negatives are picked across the
    image first (autopick.py), replacing its earlier picks for the marker; the
    examples a person gave are kept, trained on too, and weigh more.
    """

    def say(stage, done=None, total=None):
        if progress is not None:
            progress(done if done is not None else STAGES.index(stage),
                     total if total is not None else len(STAGES), stage)

    def stop():
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")

    def pixel_progress(done, total):
        # The one long stage reports its own tiles, which the panel shows as
        # a percent.
        say("features", done, total)

    def read_features():
        say("features")
        out = features.feature_matrix(ds, marker, progress=pixel_progress,
                                      cancelled=cancelled, pixels=pixels)
        stop()
        return out

    picked = None
    if auto_pick:
        if marker not in ds.table.markers:
            raise ClassifierRefused(f"{marker!r} is not a marker of this project")
        matrix, names, info = read_features()
        say("pick")
        manual = [cell for cell, origin_ in store.origins(ds.name, marker).items()
                  if origin_ != "auto"]
        pos, neg, picked = autopick.pick(ds, marker, exclude=manual, matrix=matrix,
                                         names=names)
        stop()
        store.set_auto(ds.name, marker, pos, neg)
    say("examples")
    rows, labels, report = check_ready(ds, marker)
    stop()
    if not auto_pick:
        matrix, names, info = read_features()
    say("fit")
    weights, n_given = example_weights(ds, marker, rows)
    fitted = learner.fit(matrix[rows], labels, weights=weights, n_given=n_given)
    stop()
    say("predict")
    _raw, eligible, scores, agreement, calls, uncertain = _score_all(ds, marker, matrix, fitted,
                                                                     cancelled)
    # A cell a person labelled is called as they labelled it: the model is
    # for the cells nobody looked at.
    labelled = rows[eligible[rows]]
    calls[labelled] = labels[eligible[rows]]
    uncertain[labelled] = False
    agreement[labelled] = 1.0
    stop()
    say("store")
    run_id = secrets.token_hex(6)
    record = _base_record(ds, marker, run_id, fitted, names, info, report, n_given, calls,
                          uncertain, eligible)
    record.update({"origin": origin if origin in ORIGINS else "panel", "auto_pick": picked,
                   "call_rule": "score_0.5"})
    store.write_arrays(ds.name, marker, run_id, calls=calls, scores=scores,
                       uncertain=uncertain, agreement=agreement)
    try:
        with store.editing(ds.name) as document:
            # Checked again under the lock the eraser takes: an eraser pressed
            # while this run was writing wins, and nothing of the run is kept.
            stop()
            entry = store.marker_state(document, marker)
            _swap_in(ds, marker, entry, record)
            keep = store.kept_run_ids(entry)
    except InterruptedError:
        store.run_path(ds.name, marker, run_id).unlink(missing_ok=True)
        raise
    store.remove_runs(ds.name, marker, keep=keep)
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


# -- a gating session's layer -------------------------------------------------


def _session_examples(ds, marker):
    """(rows, labels, weights, n_given, labelled ids) of the examples a
    session run trains on beside its own picks: a person's (weight 1) and an
    agent's (AUTO_WEIGHT). The panel wand's picks are left out: they were
    made at another gate."""
    origin = store.origins(ds.name, marker)
    pos, neg = store.examples(ds.name, marker)
    kept = [(int(c), 1) for c in pos if origin.get(int(c)) != "auto"] + \
           [(int(c), 0) for c in neg if origin.get(int(c)) != "auto"]
    if not kept:
        return (np.zeros(0, np.int64), np.zeros(0, np.int8), np.zeros(0), 0, [])
    ids = np.asarray([c for c, _ in kept], dtype=np.int64)
    labels = np.asarray([y for _, y in kept], dtype=np.int8)
    rows, missing = _rows_of(ds, ids)
    present = ~np.isin(ids, missing)
    ids, labels = ids[present], labels[present]
    rows, _ = _rows_of(ds, ids)
    person = np.asarray([origin.get(int(c), "click") in PERSON_ORIGINS for c in ids], bool)
    weights = np.where(person, 1.0, AUTO_WEIGHT)
    return rows, labels, weights, int(person.sum()), [int(c) for c in pos + neg]


def session_run(ds, marker, *, low, high=None, session_id=None, gate_source="session",
                rule=rules.DEFAULT_RULE, c_add=rules.C_ADD, c_clean=rules.C_CLEAN,
                band_halves=signal.BAND_HALVES, seed=0, pixels=True, keep=True,
                origin="session", progress=None, cancelled=None) -> dict:
    """The classifier's layer at a gate (`low`, `high` in the table's units).

    Returns {"record", "arrays"}: the run's record (with `status` "ready" or
    "withheld" and why), and its row-aligned arrays (None when withheld).
    `keep` stores it in the marker's session slot (never the deciding run,
    never flipping which source decides); False computes it only -- what a
    propose-mode session reports before anything is committed.
    """
    from plexora.agent import cell_exclusions

    def stop():
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")

    if marker not in ds.table.markers:
        raise ClassifierRefused(f"{marker!r} is not a marker of this project")
    low = float(low)
    matrix, names, info = features.feature_matrix(
        ds, marker, pixels=pixels, cancelled=cancelled,
        progress=None if progress is None else (lambda d, t: progress(d, t, "features")))
    stop()
    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    gate_calls = cell_exclusions.calls(ds, marker, raw, low, high)
    called = gate_calls >= 0
    snr = signal.assess(ds, marker, low, raw, called, matrix, names)
    common = {"origin": origin if origin in ORIGINS else "session", "session_id": session_id,
              "marker": marker, "gate_used": [low, None if high is None else float(high)],
              "gate_source": gate_source, "call_rule": rule, "c_add": c_add,
              "c_clean": c_clean, "snr": snr, "feature_groups": info.get("groups"),
              "pixel_reason": info.get("pixel_reason"), "versions": versions()}
    edges, h = signal.band(ds, marker, low, band_halves)
    exclude_range = None if edges is None else (edges[0], edges[2])
    rows_p, labels_p, weights_p, n_given, labelled_ids = _session_examples(ds, marker)
    try:
        pos, neg, picked = autopick.pick(ds, marker, exclude=labelled_ids, seed=seed,
                                         matrix=matrix, names=names, gate=low,
                                         exclude_range=exclude_range)
    except ClassifierRefused as exc:
        return {"record": {**common, "status": "withheld", "why": "no_examples",
                           "reason": str(exc), "created": store.now()}, "arrays": None}
    stop()
    pick_pos, _ = _rows_of(ds, pos)
    pick_neg, _ = _rows_of(ds, neg)
    rows = np.concatenate([pick_pos, pick_neg, rows_p]).astype(np.int64)
    labels = np.concatenate([np.ones(pick_pos.size, np.int8), np.zeros(pick_neg.size, np.int8),
                             labels_p])
    weights = np.concatenate([np.full(pick_pos.size + pick_neg.size, AUTO_WEIGHT), weights_p])
    fitted = learner.fit(matrix[rows], labels, weights=weights, n_given=n_given)
    stop()
    _raw, eligible, scores, agreement, raw_calls, uncertain = _score_all(
        ds, marker, matrix, fitted, cancelled)
    eligible &= called
    raw_calls[~eligible] = -1
    gate_calls = np.where(eligible, gate_calls, -1).astype(np.int8)
    # Low signal: the classifier sees background as stain, so it may take
    # positives away but never add them.
    c_add_used = None if snr["grade"] == "low" else c_add
    calls = rules.apply(scores, gate_calls, rule, c_add=c_add_used, c_clean=c_clean)
    if rows_p.size:
        # A cell a person (or an agent) labelled is called as labelled.
        ok = eligible[rows_p]
        calls[rows_p[ok]] = labels_p[ok]
        uncertain[rows_p[ok]] = False
        agreement[rows_p[ok]] = 1.0
    uncertain &= eligible
    stop()
    run_id = secrets.token_hex(6)
    report = {"n_positive": int((labels == 1).sum()), "n_negative": int((labels == 0).sum()),
              "missing": []}
    record = _base_record(ds, marker, run_id, fitted, names, info, report, n_given, calls,
                          uncertain, eligible)
    record.update(common)
    record.update({
        "status": "ready",
        "c_add_used": c_add_used,
        "band": None if edges is None else {"low": edges[0], "high": edges[2], "half_fit": h,
                                            "halves": band_halves},
        "auto_pick": picked,
        "n_picks": int(pick_pos.size + pick_neg.size),
        "compare": rules.compare(calls, gate_calls, raw_calls),
    })
    arrays = {"calls": calls, "scores": scores, "uncertain": uncertain, "agreement": agreement,
              "raw_calls": raw_calls, "gate_calls": gate_calls}
    record["stored"] = None
    result = {"record": record, "arrays": arrays}
    if keep:
        store_session_result(ds, marker, result, cancelled=cancelled)
    return result


def store_session_result(ds, marker, result, *, cancelled=None) -> dict:
    """Put a computed session layer (`session_run(keep=False)`) in the
    marker's session slot, replacing an earlier session's."""
    record, arrays = result["record"], result["arrays"]
    if record.get("status") != "ready" or arrays is None:
        return record
    run_id = record["run_id"]
    store.write_arrays(ds.name, marker, run_id, **arrays)
    try:
        with store.editing(ds.name) as document:
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            entry = store.marker_state(document, marker)
            record["stored"] = "project"
            entry["session_run"] = record
            keep_ids = store.kept_run_ids(entry)
    except InterruptedError:
        record["stored"] = None
        store.run_path(ds.name, marker, run_id).unlink(missing_ok=True)
        raise
    store.remove_runs(ds.name, marker, keep=keep_ids)
    return record


def adopt_session(ds, marker) -> dict:
    """The session's layer decides the marker from now on (as a panel run
    would): its record becomes the marker's run, the baseline is captured,
    and the eraser goes back to Thresholding. The session slot is kept, so
    the eraser leaves the layer drawn."""
    with store.editing(ds.name) as document:
        entry = store.marker_state(document, marker)
        record = entry.get("session_run")
        if not record:
            raise ClassifierRefused(f"No gating session has classified {marker}")
        _swap_in(ds, marker, entry, dict(record))
        keep = store.kept_run_ids(entry)
    store.remove_runs(ds.name, marker, keep=keep)
    _experience(ds, marker, "classifier_adopted", record)
    return {"adopted": True, "run_id": record["run_id"], "session_id": record.get("session_id"),
            "active": "classifier"}


def _experience(ds, marker, what, record):
    """A person's verdict on a gating session's layer, for the record
    automatic gating learns from by hand (`server/experience.py`)."""
    if not record or not record.get("session_id"):
        return
    from plexora.plugins.gating.server import experience

    compare = record.get("compare") or {}
    experience.correction(ds.name, marker, what, session_id=record.get("session_id"),
                          by="user", run_id=record.get("run_id"),
                          n_added=compare.get("n_added"), n_removed=compare.get("n_removed"))


def drop_session_run(ds, marker) -> dict:
    """Forget the session's layer for one marker. When it was adopted, the
    run that decides the marker is left as it is."""
    with store.editing(ds.name) as document:
        entry = store.marker_state(document, marker)
        dropped = entry.get("session_run")
        entry["session_run"] = None
        keep = store.kept_run_ids(entry)
    store.remove_runs(ds.name, marker, keep=keep)
    _experience(ds, marker, "classifier_discarded", dropped)
    return {"discarded": dropped is not None}


def discard_session_runs(ds, session_id) -> list:
    """A gating session rolled back: its layers go with its gates, and a
    marker that adopted one goes back to Thresholding. The markers touched."""
    touched = []
    for marker, entry in list(store.read_state(ds.name)["markers"].items()):
        session = (entry or {}).get("session_run") or {}
        decided = (entry or {}).get("run") or {}
        if decided.get("session_id") == session_id:
            revert(ds, marker, discard=True)
            touched.append(marker)
        elif session.get("session_id") == session_id:
            drop_session_run(ds, marker)
            touched.append(marker)
    return touched


def revert(ds, marker, discard=False) -> dict:
    """The eraser: the marker back to its baseline, its run gone, its examples
    kept. A session's layer stays drawn unless `discard`. Returns
    {reverted, active, discarded}."""
    cancel_running(ds.name, marker)
    with store.editing(ds.name) as document:
        entry = store.marker_state(document, marker)
        had = entry.get("run") is not None
        decided = entry.get("run")
        dropped = entry.get("session_run") if discard else None
        baseline = entry.get("baseline") or {}
        entry["active"] = baseline.get("active") or "threshold"
        if entry["active"] == "classifier":
            entry["active"] = "threshold"
        entry["run"] = None
        entry["baseline"] = None
        discarded = bool(discard and entry.get("session_run"))
        if discard:
            entry["session_run"] = None
        keep = store.kept_run_ids(entry)
    store.remove_runs(ds.name, marker, keep=keep)
    if decided:
        _experience(ds, marker, "classifier_reverted", decided)
    if dropped:
        _experience(ds, marker, "classifier_discarded", dropped)
    return {"reverted": had, "active": "threshold", "discarded": discarded}


def reset(ds) -> dict:
    """Reset classifier: every marker reverted, every session layer dropped
    and every example forgotten."""
    reverted = []
    for marker in list(store.read_state(ds.name)["markers"]):
        result = revert(ds, marker, discard=True)
        if result["reverted"] or result["discarded"]:
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
    used = record.get("gate_used")
    if used and used[0] is not None:
        from plexora.plugins.gating.server import model

        gate = model.get_gate(ds, record["marker"]) or {}
        if gate.get("low") is None or not np.isclose(float(gate["low"]), float(used[0]),
                                                     rtol=1e-6, atol=1e-9):
            return "the gate moved since this run"
    return None
