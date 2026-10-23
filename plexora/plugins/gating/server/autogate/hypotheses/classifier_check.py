"""The classifier as a two-way discrepancy detector on the candidates path.

`SessionOptions.classifier = "check"` with `engine = "v2"`: once a
leader resolves, the Single Cell Classifier is trained at it
(`runs.session_run`, kept nowhere) and its raw calls compared with the
leader's across the surveyed range. Where it disagrees enough
(`schemas.CLASSIFIER["check"]`) one more panel is shown before anything is
written: cells it calls positive below the leader AND cells it calls
negative above it -- never only one side, which would only ever lower a
gate -- at most half the panel, the rest cells from either side of the
leader picked without the classifier. The reader is blind to which is
which, the labels join the same evidence, and the choice is still among the
candidates: the classifier can make a rival win, never move a gate by
itself, and its agreement is never counted as support.

The classifier picks cells by its own calls, so its picks are an enriched
sample: the evidence is post-stratified by its call (`evidence.estimate`).
Every cell shown for the marker gets the classifier's call, every interval
the number of cells it calls each way (`state["classifier"]["calls"]`,
`["strata"]`), and each interval's share is read per call, weighted by the
cells it holds. The check's look is the marker's last, so the strata stay
those of the range it was computed over.

Consulted only where it has earned it: a label-set score on record for the
marker (`TRUST_PATH`, written by `tools/bench_gates/classifier_trust.py`
from the classifier bench on the user's labelled cells, never the image
being gated) in which the classifier beat the gate. Without one the check
runs exploratory: its disagreements are counted and kept, no look is shown.
A classifier whose signal grade is poor, or that cannot run (pixel features
not ready), is skipped and recorded as such (`state["classifier"]`).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from plexora.plugins.gating.server.autogate.hypotheses import controller

#: [cal] the share of a check look the classifier may choose.
FLAGGED_SHARE = 0.5
#: Signal grades (`classifier.rules.grade`) under which it is not consulted:
#: nothing says the classifier sees the stain.
UNTRUSTED_GRADES = ("low",)

#: The label-set scores the check is trusted on (absent: none).
TRUST_PATH = Path(__file__).with_name("classifier_trust.json")
#: Bumped whenever a record's meaning changes.
TRUST_VERSION = "1"
#: [cal] a record trusts the classifier when its balanced accuracy beat the
#: gate's by this much on at least this many labelled cells.
TRUST_MARGIN = 0.02
TRUST_MIN_CELLS = 50


def trust_records(path=None) -> list:
    path = Path(path or TRUST_PATH)
    if not path.exists():
        return []
    table = json.loads(path.read_text(encoding="utf-8"))
    return (table.get("records") or []) if table.get("version") == TRUST_VERSION else []


def trusted(marker, project, records=None) -> dict | None:
    """The record that trusts the classifier on `marker`, from an image other
    than `project`, or None."""
    for record in trust_records() if records is None else records:
        if str(record.get("marker", "")).lower() != str(marker).lower() \
                or record.get("project") == project:
            continue
        if int(record.get("n") or 0) >= TRUST_MIN_CELLS and \
                float(record["classifier"]) >= float(record["gate"]) + TRUST_MARGIN:
            return record
    return None


def plan(engine, unit, state, shown_ids) -> dict | None:
    """The check look's plan ({kind, ids, intervals}), or None (recorded in
    `state["classifier"]` with why)."""
    from plexora.plugins.gating.server.autogate import cellcalls

    if cellcalls.mode(engine) != "check" or state.get("classifier"):
        return None
    state["classifier"] = {"status": "skipped"}
    try:
        return _plan(engine, unit, state, shown_ids)
    except Exception as exc:  # noqa: BLE001 -- advice only; the leader still stands
        state["classifier"] = {"status": "error", "why": str(exc)[:200]}
        return None


def _plan(engine, unit, state, shown_ids):
    from plexora.plugins.gating.server.autogate import cellcalls, schemas
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import sampler
    from plexora.plugins.gating.server.classifier import features, runs

    ds = engine.call.session.data(unit["project"])
    marker = unit["marker"]
    ready, why = features.pixels_ready(ds, marker)
    if not ready:
        cellcalls.queue(engine, unit)
        state["classifier"] = {"status": "skipped", "why": why or "features_not_ready"}
        return None
    settings = cellcalls._settings(engine)
    leader = float(state["leader"])
    result = runs.session_run(ds, marker, low=leader, high=unit.get("high"),
                              session_id=engine.id, rule=settings["rule"],
                              c_add=settings["c_add"], c_clean=settings["c_clean"],
                              band_halves=settings["band_halves"], seed=settings["seed"],
                              keep=False)
    record = result["record"]
    if record.get("status") != "ready":
        state["classifier"] = {"status": "skipped", "why": record.get("why") or "withheld"}
        return None
    grade = (record.get("snr") or {}).get("grade") or record.get("grade")
    if grade in UNTRUSTED_GRADES:
        state["classifier"] = {"status": "untrusted", "grade": grade}
        return None
    arrays = result["arrays"]
    values = cellmod.values(ds, marker)
    c = cellmod.cells(ds)
    bounds, _ = controller.window(state)
    called = (arrays["gate_calls"] >= 0) & c.valid & np.isfinite(values)
    if len(shown_ids):
        called &= ~np.isin(c.ids, np.asarray(list(shown_ids), dtype=c.ids.dtype))
    raw = arrays["raw_calls"]
    below_all = called & (values > bounds[0]) & (values <= leader)
    above_all = called & (values > leader) & (values <= bounds[-1])
    below, above = below_all & (raw == 1), above_all & (raw == 0)
    bar = schemas.CLASSIFIER["check"]
    sides = {"below": (below, below_all), "above": (above, above_all)}
    counts = {name: {"flagged": int(f.sum()), "cells": int(a.sum()),
                     "share": round(float(f.sum()) / max(1, int(a.sum())), 4)}
              for name, (f, a) in sides.items()}
    passing = [name for name, n in counts.items()
               if n["flagged"] >= bar["min_cells"] and n["share"] >= bar["min_share"]]
    state["classifier"] = {"status": "agrees" if not passing else "look", "grade": grade,
                           "counts": counts}
    if not passing:
        return None
    record = trusted(marker, unit["project"])
    if record is None:
        state["classifier"].update(status="exploratory",
                                   why="no label-set score on record for this marker")
        return None
    state["classifier"]["trusted_on"] = {k: record.get(k) for k in
                                         ("project", "marker", "n", "classifier", "gate",
                                          "source")}
    total = int(state["params"]["panel_cells"])
    n_flagged = int(total * FLAGGED_SHARE)
    flagged_total = sum(counts[n]["flagged"] for n in sides) or 1
    rng = np.random.default_rng(int(engine.options["seed"]) * 31 + 7)
    picked_rows = []
    by_side = {}
    for name, (mask, _all) in sides.items():
        # Both directions whenever both have flagged cells: a check that
        # only ever shows the cells below would only ever lower the gate.
        want = max(1 if counts[name]["flagged"] else 0,
                   round(n_flagged * counts[name]["flagged"] / flagged_total))
        rows = np.flatnonzero(mask)
        chosen = sampler._spread_by_value(c, values, rows, min(want, rows.shape[0]), 8, rng)
        by_side[name] = int(len(chosen))
        picked_rows.extend(int(r) for r in chosen)
    if by_side.get("below"):
        # Cells it would add below the leader were its own stain 47 % of the
        # time on the person's labelled cells, cells it would remove above
        # every time (2026-10-19 wand bench): a record for the review note,
        # never a weight on the evidence.
        state["classifier"]["low_trust"] = {"side": "below", "picked": by_side["below"]}
    # The rest from the two intervals beside the leader, chosen blind to the
    # classifier (either call).
    lead = bounds.index(min(bounds, key=lambda b: abs(b - leader)))
    beside = [k for k in (lead - 1, lead) if 0 <= k < len(bounds) - 1]
    left = max(0, total - len(picked_rows))
    taken = np.zeros(c.n, dtype=bool)
    taken[picked_rows] = True
    for i, k in enumerate(beside):
        n = left // len(beside) + (1 if i < left % len(beside) else 0)
        rows = np.flatnonzero(called & ~taken & (values > bounds[k]) & (values <= bounds[k + 1]))
        picked_rows.extend(int(r) for r in
                           sampler._spread_by_value(c, values, rows, min(n, rows.shape[0]),
                                                    8, rng))
    # The strata: every interval's cells by the classifier's call, and the
    # call of every cell shown so far or about to be.
    called_all = (arrays["gate_calls"] >= 0) & c.valid & np.isfinite(values)
    strata = []
    for k in range(len(bounds) - 1):
        inside = called_all & (values > bounds[k]) & (values <= bounds[k + 1])
        strata.append({"1": int((inside & (raw == 1)).sum()),
                       "0": int((inside & (raw == 0)).sum())})
    rows_shown = np.flatnonzero(called_all & np.isin(c.ids, np.asarray(list(shown_ids) or [-1],
                                                                  dtype=c.ids.dtype)))
    calls = {str(int(c.ids[r])): int(raw[r]) for r in [*rows_shown.tolist(), *picked_rows]}
    state["classifier"].update(calls=calls, strata=strata, window=list(state["window"]))
    return {"kind": "classifier_check", "ids": [int(c.ids[r]) for r in picked_rows],
            "intervals": []}
