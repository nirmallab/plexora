"""Hybrid gating's cell-model runs inside a session (`SessionOptions.hybrid`).

The cell model (`cellcall.py`) reads a marker's cells near a gate: about a
minute a marker on a laptop's CPU, so it never runs inside a packet's call.
A session keeps a queue of runs (`record["cellcall"]["queue"]`); a session
job (`gating.session_cellcall`, submitted beside the bulk job) drains it,
one marker at a time, nearest first in the order queued, each run in
parallel inside (`cellcall.predict`). What a run said is kept on the unit
(`unit["model"]["runs"][purpose]`) and the run itself on disk, keyed by
gate, data and model (`cellcall.load`).

Two purposes:
  decide   near the gate a look will judge or has approved (a narrower
           reach, `HYBRID["decide_halves"]` / `["decide_cells"]`): what the
           session's steps read. A unit that needs one waits for it
           (`unit["model"]["waiting"]`); the rest of the session goes on.
  layer    at the gate written, at the model's full reach (`CELLCALL`): the
           per-cell layer shown beside the gate. Nothing waits for it, and
           the session may finish first.

A marker the model cannot read (no mask, no image channel, brightfield, no
model file) is `unavailable`, with why, and is gated as without it.
"""

from __future__ import annotations

#: [cal] (validation pending: docs/internal/bench/AUTOGATE_STEP_LEDGER.md).
#:   decide_halves / decide_cells  the decision runs' reach and cap: the window
#:       the correction may move in (`correction.CORRECTION`) and its cells
#:   rerun_halves  a run read at a gate this many band halves from where the
#:       session now stands is read again there (study S2)
#:   correct  step 3: correct a gate a look approved (study S4)
#:   check_cells / reference_cells  what the correction's one question shows
#:   accept_share  the share of the answered cells that must agree with the
#:       move, by direction (from the readers' measured confusion, study S3:
#:       a reader agreed with the truth about 2 times in 3 either way)
#:   layer  run the per-cell layer at each looked-at marker's written gate
#:   confirm  put a correction to the vision model first (`correction_check`);
#:       off, the move is applied as computed. Off by default: in the live
#:       ablation the check took every correction on lsp11385_2 and refused
#:       six of eight on CODEX, whose refused moves would have gained as much
#:       as the taken ones (ledger E9) -- the reader is a weaker judge of
#:       these cells than the model's confident calls (E3)
HYBRID = {"decide_halves": 6.0, "decide_cells": 40_000, "rerun_halves": 2.0,
          "correct": True, "confirm": False, "check_cells": 8, "reference_cells": 3,
          "accept_share": {"up": 0.5, "down": 0.5}, "layer": True}

PURPOSES = ("decide", "layer")


def queue_of(record) -> dict:
    """The session's queue of runs (created empty)."""
    return record.setdefault("cellcall", {"queue": [], "job_id": None, "seq": 0})


def request(engine, unit, low, *, purpose, wait=False):
    """Queue a run of the cell model for `unit` at `low`; with `wait` the
    unit's next decision waits for it. Returns the request id, or None when
    the marker is unavailable or the same run is already queued (then that
    one is waited on)."""
    from plexora.plugins.gating.server.autogate import cellcall
    from plexora.plugins.gating.server.autogate.engine import unit_key

    state = unit.setdefault("model", {})
    if state.get("unavailable"):
        return None
    q = queue_of(engine.record)
    key = unit_key(unit["project"], unit["marker"])
    reach = dict(band_halves=HYBRID["decide_halves"], max_cells=HYBRID["decide_cells"]) \
        if purpose == "decide" else dict(band_halves=cellcall.CELLCALL["band_halves"],
                                         max_cells=cellcall.CELLCALL["max_cells"])
    for queued in q["queue"]:
        if queued["unit"] == key and queued["purpose"] == purpose \
                and abs(queued["low"] - float(low)) <= 1e-9:
            if wait:
                state["waiting"] = queued["id"]
            return None
    # A run of the same purpose not started yet is for a gate the unit has
    # left: this one replaces it.
    stale = {r["id"] for r in q["queue"] if not r.get("taken") and r["unit"] == key
             and r["purpose"] == purpose}
    q["queue"] = [r for r in q["queue"] if r["id"] not in stale]
    q["seq"] += 1
    entry = {"id": q["seq"], "unit": key, "project": unit["project"], "marker": unit["marker"],
             "low": float(low), "high": unit.get("high"), "purpose": purpose, **reach}
    q["queue"].append(entry)
    if wait or state.get("waiting") in stale:
        state["waiting"] = entry["id"]
    return entry["id"]


def waiting(unit) -> bool:
    """Whether the unit's next decision waits on a run of the model."""
    return (unit.get("model") or {}).get("waiting") is not None


def done(engine, request_, outcome):
    """Record a finished run on its unit and take it off the queue."""
    q = queue_of(engine.record)
    q["queue"] = [r for r in q["queue"] if r["id"] != request_["id"]]
    unit = engine.record["units"].get(request_["unit"])
    if unit is None:
        return
    state = unit.setdefault("model", {})
    if outcome.get("status") == "unavailable":
        state["unavailable"] = outcome.get("why") or "the model cannot read this marker"
    state.setdefault("runs", {})[request_["purpose"]] = {"low": request_["low"], **outcome}
    if state.get("waiting") == request_["id"] or outcome.get("status") == "unavailable":
        state.pop("waiting", None)


def run_queue(call, session_id):
    """The session job's body: run what is queued until nothing is. Each run
    is read outside the session's lock; only taking a request and recording
    its result hold it."""
    import time

    from plexora.plugins.gating.server.autogate import cellcall, cellcall_weights
    from plexora.plugins.gating.server.autogate.engine import engine_for

    model = None
    finished = 0
    while True:
        call.check_cancelled()
        with engine_for(call, session_id) as engine:
            q = queue_of(engine.record)
            job_id = (call.job or {}).get("job_id")
            free = [r for r in q["queue"] if not r.get("taken")]
            if not free:
                if q.get("job_id") == job_id:
                    q["job_id"] = None
                break
            entry = free[0]
            entry["taken"] = job_id or True
            req = dict(entry)
            unit = engine.record["units"].get(req["unit"])
            gates = {m: float(g) for m, (g, _grade) in engine.evidence_gates(unit).items()
                     if g is not None} if unit is not None else {}
        started = time.monotonic()
        try:
            if model is None:
                model = cellcall.predictor()
            ds = call.session.data(req["project"])

            def progress(n_done, n, marker=req["marker"]):
                call.progress(done=finished, total=finished + 1,
                              message=f"{marker}: {n_done:,} of {n:,} cells read")

            found = cellcall.run(call.session, ds, req["marker"], req["low"], req.get("high"),
                                 gates=gates, model=model, progress=progress,
                                 cancelled=call.cancelled, band_halves=req["band_halves"],
                                 max_cells=req["max_cells"])
            outcome = {"status": "ready", **cellcall.summary(found),
                       "model": found["info"].get("model"),
                       "seconds": round(time.monotonic() - started, 1)}
        except cellcall.Unavailable as exc:
            outcome = {"status": "unavailable", "why": str(exc)}
        except cellcall_weights.CellModelMissing as exc:
            outcome = {"status": "unavailable", "why": str(exc)}
        except InterruptedError:
            call.check_cancelled()
            raise
        with engine_for(call, session_id) as engine:
            done(engine, req, outcome)
            if outcome["status"] == "unavailable" and model is None:
                # No model file: nothing queued can be read either.
                for other in list(queue_of(engine.record)["queue"]):
                    done(engine, other, outcome)
        finished += 1
    return {"session_id": session_id, "runs": finished}


def stored(ds, unit, purpose="decide"):
    """The stored run a unit's `purpose` run made (`cellcall.load`), or None."""
    from plexora.plugins.gating.server.autogate import cellcall

    run = ((unit.get("model") or {}).get("runs") or {}).get(purpose) or {}
    if run.get("status") != "ready":
        return None
    # By the weights that made it (the run's key names them).
    return cellcall.load(ds, unit["marker"], run["low"], unit.get("high"),
                         model_id=run.get("model"))


def detail(unit) -> dict | None:
    """`detail["cellcall"]` of a gate's provenance row: what the model said
    and did for this marker; None when it never ran."""
    from plexora.plugins.gating.server.autogate import schemas

    state = unit.get("model") or {}
    if not state:
        return None
    runs = {purpose: {k: run.get(k) for k in ("status", "low", "why", "n_run", "n_promoted",
                                             "n_demoted", "n_uncertain", "reach_halves",
                                             "capped", "model", "seconds") if k in run}
            for purpose, run in (state.get("runs") or {}).items()}
    out = {"version": schemas.CELLCALL_VERSION, "hybrid": schemas.HYBRID_VERSION, "runs": runs}
    for key in ("unavailable", "left_out", "correction", "influenced"):
        if state.get(key) is not None:
            out[key] = state[key]
    return out


# -- step 3: correcting a gate a look approved -----------------------------------------


def judged_limits(unit, col):
    """(lo, hi) in the fit space: the gate may not cross an interval a look
    at candidates judged -- one judged `own_stain` keeps the gate below it,
    one judged `unstained` / `neighbour_only` above it. Only intervals on the
    side the approved gate left them on count."""
    import numpy as np

    g = float(col.to_fit(unit["candidate"]))
    lo, hi = -np.inf, np.inf
    for a, b, word in unit.get("judged") or ():
        fa, fb = float(col.to_fit(a)), float(col.to_fit(b))
        if word == "own_stain" and fa >= g - 1e-9:
            hi = min(hi, fa)
        elif word in ("unstained", "neighbour_only") and fb <= g + 1e-9:
            lo = max(lo, fb)
    return lo, hi


def _eligible(unit) -> bool:
    return HYBRID["correct"] and not unit.get("no_image_channel") \
        and not unit.get("condition") and unit.get("path") in ("t2", "t3", "t4") \
        and unit.get("candidate") is not None


def _near_run(ds, unit, col, g_v):
    """The unit's stored decision run, when it was read within
    `rerun_halves` band halves of `g_v`; else None."""
    found = stored(ds, unit)
    if found is None:
        return None
    h = float(found["info"]["band_half"])
    moved = abs(float(col.to_fit(found["info"]["low"])) - float(col.to_fit(g_v))) / max(h, 1e-12)
    return found if moved <= HYBRID["rerun_halves"] else None


def _solve(engine, unit, col, found, g_v):
    """(moved, record, h, gf_v): the correction of `g_v` from a run (and the
    `model_disagrees` flag when the model's own cut lies far away)."""
    import numpy as np

    from plexora.plugins.gating.server.autogate import cellcall, correction

    gf_v = float(col.to_fit(g_v))
    h = float(found["info"]["band_half"])
    window = correction.CORRECTION["window_halves"] * h
    lattice = engine.lattice_for(unit)
    grid = [float(col.to_fit(p["low"])) for p in lattice.get("points") or ()]
    grid = [x for x in grid if abs(x - gf_v) <= window + 1e-12]
    fit = np.sort(np.asarray(col.fit, dtype=np.float64))

    def between(a, b):
        return int(np.searchsorted(fit, b, "right") - np.searchsorted(fit, a, "right"))

    confident = cellcall.CELLCALL["confident"]
    out = correction.solve(grid, gf_v, vf=found["vf"], p=found["p"], between=between,
                           confident=confident, clip=judged_limits(unit, col))
    wide = gf_v + h * np.arange(-24, 24.01, 0.25)
    cut = correction.model_cut(wide, vf=found["vf"], p=found["p"], confident=confident)
    record = {"g_v": g_v, "g_star": float(col.from_fit(out["g_star"])),
              "direction": out["direction"], "steps": out["steps"],
              "flagged_before": out["flagged_before"], "flagged_after": out["flagged_after"],
              "changed": out["changed"], "model_cut": None if cut is None
              else float(col.from_fit(cut))}
    if cut is not None and abs(cut - gf_v) >= correction.CORRECTION["far_halves"] * h:
        # Far from where the looks put the gate: a disagreement on record,
        # never a move by itself.
        unit["look_flags"] = sorted(set(unit.get("look_flags") or []) | {"model_disagrees"})
    return out["moved"], record, h, gf_v


def _apply(unit, record, status):
    unit.setdefault("model", {})["correction"] = {"status": status, **record}
    unit["candidate"] = float(record["g_star"])
    unit.setdefault("code_moves", []).append(f"{record['direction']}:cell_model")


def plan_correction(engine, unit):
    """Step 3 for a unit about to settle at a gate a look approved: True
    while it must wait (a run queued, or the question asked: the unit is then
    `awaiting_correction`), False to settle on. Records
    `unit["model"]["correction"]` whatever it decides."""
    from plexora.plugins.gating.server.autogate import profile as profmod

    state = unit.setdefault("model", {})
    if state.get("correction") is not None or not _eligible(unit):
        return False
    if state.get("unavailable"):
        state["correction"] = {"status": "unavailable"}
        return False
    ds = engine.call.session.data(unit["project"])
    col = profmod.column(ds, unit["marker"])
    g_v = float(unit["candidate"])
    found = _near_run(ds, unit, col, g_v)
    if found is None:
        last = (state.get("runs") or {}).get("decide") or {}
        if last.get("status") == "ready" and abs(float(last["low"]) - g_v) <= 1e-9:
            # Read at this very gate, and gone from disk since: never ask again.
            state["correction"] = {"status": "unreadable"}
            return False
        request(engine, unit, g_v, purpose="decide", wait=True)
        return True
    moved, record, h, gf_v = _solve(engine, unit, col, found, g_v)
    if not moved:
        state["correction"] = {"status": "none", **record}
        return False
    if not HYBRID["confirm"]:
        # Applied as computed (`HYBRID["confirm"]`), recorded either way.
        _apply(unit, record, "applied")
        return False
    cells, references = _check_cells(ds, unit, col, gf_v, float(col.to_fit(record["g_star"])), h)
    if not cells:
        state["correction"] = {"status": "no_cells", **record}
        return False
    state["correction"] = {"status": "asked", **record, "cells": cells,
                           "references": references}
    unit["state"] = "awaiting_correction"
    return True


def correct_for_review(engine, unit):
    """A unit the looks could not settle is written for review at its
    current gate; the model corrects that gate first when a run near it is
    already stored (the one read ahead of its first look) -- never waiting
    for one, never asking. The unit stays flagged for review. Returns the
    corrected gate, or None."""
    from plexora.plugins.gating.server.autogate import profile as profmod

    state = unit.setdefault("model", {})
    if state.get("correction") is not None or not _eligible(unit) or state.get("unavailable"):
        return None
    ds = engine.call.session.data(unit["project"])
    col = profmod.column(ds, unit["marker"])
    g_v = float(unit["candidate"])
    found = _near_run(ds, unit, col, g_v)
    if found is None:
        return None
    moved, record, _h, _gf = _solve(engine, unit, col, found, g_v)
    if not moved:
        state["correction"] = {"status": "none", **record}
        return None
    _apply(unit, record, "applied_review")
    return unit["candidate"]


def _check_cells(ds, unit, col, gf_v, gf_star, h):
    """(cell ids to ask about, reference ids): up to `check_cells` of the
    cells whose call the move changes, spread evenly over their values, and
    `reference_cells` clear positives (four band halves above the farther
    gate and more), both by a seeded draw."""
    import zlib

    import numpy as np

    from plexora.plugins.gating.server.autogate import cells as cellmod

    values = col.to_fit(cellmod.values(ds, unit["marker"]))
    ids = cellmod.cells(ds).ids
    ok = cellmod.eligible(ds, unit["marker"]) & np.isfinite(values)
    lo, hi = min(gf_v, gf_star), max(gf_v, gf_star)
    changed = np.flatnonzero(ok & (values > lo) & (values <= hi))
    rng = np.random.default_rng([zlib.crc32(unit["marker"].encode()), 7])
    n = int(HYBRID["check_cells"])
    picked = []
    if changed.size:
        order = changed[np.argsort(values[changed], kind="stable")]
        if order.size > n:
            # One cell from each of n equal slices of the values, drawn.
            slices = np.array_split(order, n)
            picked = [int(rng.choice(part)) for part in slices if part.size]
        else:
            picked = order.tolist()
    clear = np.flatnonzero(ok & (values > hi + 4.0 * h))
    refs = rng.choice(clear, size=min(int(HYBRID["reference_cells"]), clear.size),
                      replace=False).tolist() if clear.size else []
    return [int(ids[r]) for r in picked], [int(ids[r]) for r in sorted(refs)]


def apply_answer(engine, unit, words):
    """Accept the corrected gate when enough of the answered cells agree with
    the move (`accept_share`), else keep the approved one. Returns the
    record."""
    state = unit.setdefault("model", {})
    record = dict(state.get("correction") or {})
    direction = record.get("direction")
    agree_word = "not_own" if direction == "up" else "own"
    answered = [w for w in words.values() if w in ("own", "not_own")]
    agree = sum(1 for w in answered if w == agree_word)
    share = agree / len(answered) if answered else 0.0
    accepted = bool(answered) and share >= HYBRID["accept_share"][direction]
    record.update(status="accepted" if accepted else "rejected", agree=agree,
                  answered=len(answered), share=round(share, 3))
    state["correction"] = record
    if accepted:
        unit["candidate"] = float(record["g_star"])
        unit.setdefault("code_moves", []).append(f"{direction}:cell_model")
    else:
        unit["look_flags"] = sorted(set(unit.get("look_flags") or [])
                                    | {"model_disagrees"})
    return record
