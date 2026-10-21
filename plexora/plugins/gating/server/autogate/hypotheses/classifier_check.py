"""The classifier as a two-way discrepancy detector on the candidates path.

`SessionOptions.classifier = "check"` with `engine = "candidates"`: once a
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

A classifier whose signal grade is poor, or that cannot run (pixel features
not ready), is skipped and recorded as such (`state["classifier"]`).
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.autogate.hypotheses import controller

#: [cal] the share of a check look the classifier may choose.
FLAGGED_SHARE = 0.5
#: Signal grades (`classifier.rules.grade`) under which it is not consulted:
#: nothing says the classifier sees the stain.
UNTRUSTED_GRADES = ("low",)


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
    total = int(state["params"]["panel_cells"])
    n_flagged = int(total * FLAGGED_SHARE)
    flagged_total = sum(counts[n]["flagged"] for n in sides) or 1
    rng = np.random.default_rng(int(engine.options["seed"]) * 31 + 7)
    ids = []
    for name, (mask, _all) in sides.items():
        # Both directions whenever both have flagged cells: a check that
        # only ever shows the cells below would only ever lower the gate.
        want = max(1 if counts[name]["flagged"] else 0,
                   round(n_flagged * counts[name]["flagged"] / flagged_total))
        rows = np.flatnonzero(mask)
        picked = sampler._spread_by_value(c, values, rows, min(want, rows.shape[0]), 8, rng)
        ids.extend(int(c.ids[r]) for r in picked)
    # The rest from the two intervals beside the leader, chosen blind to the
    # classifier.
    lead = bounds.index(min(bounds, key=lambda b: abs(b - leader)))
    beside = [k for k in (lead - 1, lead) if 0 <= k < len(bounds) - 1]
    left = max(0, total - len(ids))
    intervals = [{"interval": k, "from": bounds[k], "to": bounds[k + 1],
                  "n": left // len(beside) + (1 if i < left % len(beside) else 0)}
                 for i, k in enumerate(beside)] if beside else []
    return {"kind": "classifier_check", "ids": ids, "intervals": [i for i in intervals
                                                                 if i["n"] > 0]}
