"""The Single Cell Classifier beside a gating session's gates.

Automatic gating places one threshold per marker; that stays the best
threshold placer measured (held-out 12 PCA images: classic 0.368 MAE against
the user's gates). A threshold cannot call the cells either side of it well
-- near the gate it agrees with the user's own cell labels about as often as
the user's own gate does (~59 %) -- and the classifier, trained AT the gate,
agrees more (72 % pooled with no label, 76-79 % with four to eight; bench
CLASSIFIER_WAND_2026-10-19). So each written gate gets a per-cell layer,
and the two stay distinct: the threshold is what Thresholding exports; the
layer's calls are kept beside it, with every place they part ways, and
decide the marker only when the user adopts them in the panel.

Three modes, by `SessionOptions.classifier` (`schemas.CLASSIFIER_MODES`):

- **`layer`** (the default): after a gate is written -- accepted, or written
  for review -- the classifier is trained at it (`runs.session_run`) and its
  calls stored in the marker's session slot: the gate's call unless the
  classifier is confident the other way, with a looser margin for taking a
  positive away than for adding one (`classifier.rules`). A marker whose
  signal is poor (`classifier.signal.grade`) gets a cleaning-only layer, or
  none. A user's gate a session kept gets a layer at that gate. The layer is
  `pending` until a job of the session's (`gating.session_classifier`) has
  made it -- never a wait in `gating_next` or `gating_answer`.
- **`check`**: the layer, and once per marker, before a looked-at gate is
  accepted, the classifier trained at that gate is compared with it in the
  first row of candidates on each side (`disagreement`). If the gate is
  right the two largely agree. Where they do not -- the classifier calling
  many cells below the gate positive, or many above it negative -- the
  cells it flags are shown to a look at the candidates on that side, row by
  row, beside cells from the same range it agrees on, the two unmarked
  (`check_rows`; the user's design, 2026-10-19). The gate walks past each
  row whose flagged cells the look reads the classifier's way while it does
  not read the others so (`confirms`), and stops at the first row that does
  not: the look decides, in either direction, and a reader who sees every
  cell alike moves nothing. On p193 the classifier at the session's gate
  called 12.6 % of cells CD3E positive (gate 5.3 %, the user 22.5 %) and
  10.4 % CD8a (gate 5.0 %, the user 12.9 %), which the cleaning-only layer
  could never add. Unmeasured on the reader bench
  (`schemas.CLASSIFIER["measured"]`).
- **`off`**: nothing.

Nothing here enters a packet, a memo key or `regression.checks`: a session
with the layer on asks the same questions as one with it off; a check adds
one look at the candidates for a marker the classifier disagrees on.

**Read ahead.** The pixel pass a layer needs reads the whole image once per
marker (seconds to minutes; on a data node, the node does it). When a
marker's first packet goes out, it is queued for the session's job, which
builds the features while the reader looks (`after_issue`), so the layer is
usually ready by the time the gate is.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.autogate import schemas

#: The summary kept on a unit (`unit["classifier"]`) and shown by status,
#: report and get_gate.
SUMMARY_KEYS = ("status", "why", "reason", "run_id", "gate_used", "gate_source", "call_rule",
                "c_add", "c_add_used", "c_clean", "snr", "model", "cv_balanced_accuracy",
                "n_picks", "n_uncertain", "feature_groups", "pixel_reason", "compare",
                "stored")


def mode(engine) -> str:
    options = engine.options
    if options["purpose"] == "contrast":
        return "off"
    chosen = options["classifier"]
    return chosen if chosen in schemas.CLASSIFIER_MODES else "off"


def _key(unit):
    from plexora.plugins.gating.server.autogate.engine import unit_key

    return unit_key(unit["project"], unit["marker"])


def _ds(engine, unit):
    return engine.call.session.data(unit["project"])


def _held(engine) -> dict:
    """Layers computed in this engine context (a check's at the candidate,
    reused by the layer when the gate stays there)."""
    return engine.__dict__.setdefault("_cellcalls", {})


def _summary(record) -> dict:
    out = {k: record.get(k) for k in SUMMARY_KEYS if record.get(k) is not None}
    out["cv"] = out.pop("cv_balanced_accuracy", None)
    return out


def _gate_for(engine, unit, ds):
    """(low, high, source) of the gate a unit's layer is made at, or None."""
    from plexora.plugins.gating.server import model

    gate = model.get_gate(ds, unit["marker"]) or {}
    if unit.get("state") == "skipped_manual":
        if gate.get("low") is None or not gate.get("thresholded", True):
            return None
        return float(gate["low"]), gate.get("high"), "user"
    if unit.get("final") is None:
        return None
    high = unit.get("high") if unit.get("high") is not None else gate.get("high")
    source = "review" if unit.get("state") in schemas.REVIEW_WRITE_STATES else "session"
    return float(unit["final"]), high, source


def _settings(engine):
    from plexora.plugins.gating.server.classifier import rules

    return {"rule": rules.DEFAULT_RULE, "c_add": rules.C_ADD, "c_clean": rules.C_CLEAN,
            "band_halves": schemas.CLASSIFIER["band_halves"],
            "seed": int(engine.options["seed"])}


def _layer_due(engine, unit) -> bool:
    if mode(engine) == "off" or unit.get("user_edited"):
        return False
    state = unit.get("state")
    return state in schemas.ACCEPTED_STATES + schemas.REVIEW_WRITE_STATES + ("skipped_manual",)


def after_write(engine, unit) -> None:
    """The layer at the gate a unit was just closed with (`Engine.finalize`,
    `Engine._close_tail`). Failures are logged, never raised: a layer is
    never a reason for a gate not to stand."""
    if not _layer_due(engine, unit):
        return
    try:
        _layer(engine, unit)
    except Exception as exc:  # noqa: BLE001
        unit["classifier"] = {"status": "failed", "reason": str(exc)[:300]}
        engine.log(event="classifier_error", unit=_key(unit), error=str(exc)[:200])


def _layer(engine, unit):
    from plexora.plugins.gating.server.classifier import runs

    ds = _ds(engine, unit)
    marker = unit["marker"]
    if marker not in ds.table.markers:
        return
    gate = _gate_for(engine, unit, ds)
    if gate is None:
        return
    low, high, source = gate
    keep = engine.options["mode"] == "apply"
    held = _held(engine).pop(_key(unit), None)
    if held is not None and np.isclose(held[0], low, rtol=1e-6, atol=1e-9):
        # The check already trained the classifier at this very gate.
        result = held[1]
        if keep:
            runs.store_session_result(ds, marker, result)
            _tell(engine.call, unit["project"], marker)
        _record(engine, unit, result["record"], source)
        return
    # Made by the session's job: seconds a marker on a large table, which an
    # answer never waits for.
    unit["classifier"] = {"status": "pending", "gate_used": [low, high],
                          "gate_source": source, "store": keep}
    queue(engine, unit, layer=True)
    engine.log(event="classifier_pending", unit=_key(unit))


def _record(engine, unit, record, source):
    summary = _summary(record)
    summary["gate_source"] = source
    unit["classifier"] = summary
    if record.get("status") == "ready":
        compare = record.get("compare") or {}
        engine.log(event="classifier_layer", unit=_key(unit), run_id=record.get("run_id"),
                   added=compare.get("n_added"), removed=compare.get("n_removed"),
                   grade=(record.get("snr") or {}).get("grade"))
    else:
        engine.log(event="classifier_withheld", unit=_key(unit), why=record.get("why"))


# -- the check: the classifier flags, a look decides --------------------------------------


def _skip(engine, unit, why):
    if unit.get("classifier_check_skipped") != why:
        unit["classifier_check_skipped"] = why
        engine.log(event="classifier_check_skipped", unit=_key(unit), reason=why)
    return False


def check(engine, unit) -> bool:
    """Before a looked-at gate is accepted (`Engine.finalize`): once per
    marker, the classifier is trained at the candidate gate, and where its
    own calls part from the gate's just beside it -- below the gate calling
    cells positive, or above it calling them negative -- the cells it flags
    go to a look at the candidates on that side, with cells it agrees on
    mixed in, unmarked (`check_rows`). True when it did (the unit then waits
    for that look; `transitions.apply_t4` reads it with `confirms`)."""
    if mode(engine) != "check" or unit.get("classifier_check") or unit.get("condition"):
        return False
    if unit.get("path") not in ("t2", "t3", "t4") or not unit.get("regression"):
        return False
    if int(engine.options["max_tier"]) < 4 or not engine.rounds_left(unit) \
            or engine.over_budget(unit) or unit.get("candidate") is None:
        return False
    try:
        return _check(engine, unit)
    except Exception as exc:  # noqa: BLE001 -- a check is advice; the gate still stands
        engine.log(event="classifier_error", unit=_key(unit), error=str(exc)[:200])
        return False


def _check(engine, unit):
    from plexora.plugins.gating.server.autogate import lattice as latmod
    from plexora.plugins.gating.server.classifier import features, runs

    ds = _ds(engine, unit)
    marker = unit["marker"]
    ready, _why = features.pixels_ready(ds, marker)
    if not ready:
        queue(engine, unit)
        return _skip(engine, unit, "features_not_ready")
    settings = _settings(engine)
    candidate = float(unit["candidate"])
    result = runs.session_run(ds, marker, low=candidate, high=unit.get("high"),
                              session_id=engine.id, rule=settings["rule"],
                              c_add=settings["c_add"], c_clean=settings["c_clean"],
                              band_halves=settings["band_halves"], seed=settings["seed"],
                              keep=False)
    _held(engine)[_key(unit)] = (candidate, result)
    if result["record"].get("status") != "ready":
        return _skip(engine, unit, result["record"].get("why") or "withheld")
    lat = engine.lattice_for(unit)
    found = {}
    for direction in ("down", "up"):
        chain = check_points(ds, unit, candidate, _reach(lat, candidate, direction))
        if not chain:
            continue
        lo, hi = sorted((candidate, float(chain[0]["low"])))
        if _contradicted(unit, direction, lo, hi):
            _skip(engine, unit, f"rows_disagree_{direction}")
            continue
        found[direction] = disagreement(ds, unit, result, lo, hi, direction)
    measured = {d: {k: v for k, v in m.items() if k not in ("flagged", "control")}
                for d, m in found.items()}
    unit["classifier_check_measure"] = measured
    passing = [d for d, m in found.items() if m["pass"]]
    if not passing:
        return False
    direction = max(passing, key=lambda d: found[d]["share"])
    split = []
    if engine.options["split_mixed"]:
        first = latmod.chain(lat, candidate, direction)[0]["low"]
        split = [p["low"] for p in engine.split_row(unit, candidate, first)]
    chain = check_points(ds, unit, candidate,
                         _reach(engine.lattice_for(unit), candidate, direction))
    rows = check_rows(ds, unit, result, candidate, chain, direction,
                      seed=settings["seed"])
    if not rows:
        return _skip(engine, unit, "nothing_to_show")
    _held(engine).pop(_key(unit), None)
    morphology = ((engine.panel_for(unit["project"]).get("entries") or {})
                  .get(marker) or {}).get("morphology")
    unit["classifier_check"] = {"direction": direction, "at": candidate, "rows": rows,
                                "asked": False, "split": split, "morphology": morphology,
                                "reading": reading_for(engine.options["check_reading"],
                                                       morphology),
                                **{k: measured[direction][k] for k in
                                   ("n_cells", "n_flagged", "share", "tiles_hit")}}
    unit.setdefault("code_moves", []).append(f"{direction}:classifier_check")
    unit["direction"] = direction
    unit["magnitude"] = None
    unit["span_from"] = candidate
    unit["state"] = "awaiting_t4"
    engine.log(event="classifier_check", unit=_key(unit), direction=direction, at=candidate,
               n_flagged=measured[direction]["n_flagged"], share=measured[direction]["share"])
    return True


#: The walk's points a check may choose its rows from (`_reach`).
CHECK_REACH = 64


def _contradicted(unit, direction, lo, hi) -> bool:
    """A row a look already judged in (lo, hi] says the opposite of moving
    that way: going down, cells there seen as not positive; going up, cells
    there seen positive by their own stain."""
    from plexora.plugins.gating.server.autogate import verdicts

    against = verdicts.AGAINST[direction]
    return any(word in against and max(a, b) > lo and min(a, b) < hi
               for a, b, word in unit.get("judged") or ())


def _flags(arrays, values, lo, hi, direction):
    """(cells in (lo, hi], flagged, control) as row masks: flagged are the
    cells the classifier calls the other way from the gate, control the
    ones it agrees on."""
    called = arrays["gate_calls"] >= 0
    raw = arrays["raw_calls"]
    inside = called & (values > lo) & (values <= hi)
    if direction == "down":
        return inside, inside & (raw == 1), inside & (raw == 0)
    return inside, inside & (raw == 0), inside & (raw == 1)


def disagreement(ds, unit, result, lo, hi, direction) -> dict:
    """How much the classifier at the gate disagrees with it in the first row
    on one side, and whether that is enough for a look
    (`schemas.CLASSIFIER["check"]`): enough flagged cells, a share of the
    row, spread over tiles."""
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import qc_cells

    bar = schemas.CLASSIFIER["check"]
    values = cellmod.values(ds, unit["marker"])
    inside, flagged, _control = _flags(result["arrays"], values, lo, hi, direction)
    n_cells, n_flagged = int(inside.sum()), int(flagged.sum())
    share = n_flagged / n_cells if n_cells else 0.0
    tiles_hit = 0
    if n_flagged:
        c = cellmod.cells(ds)
        tile, _nx, _ny, _centres = qc_cells.tiles(c, c.valid & np.isfinite(values))
        hit = tile[flagged & c.valid]
        tiles_hit = int(np.unique(hit[hit >= 0]).size)
    return {"direction": direction, "from": lo, "to": hi, "n_cells": n_cells,
            "n_flagged": n_flagged, "share": round(share, 4), "tiles_hit": tiles_hit,
            "pass": bool(n_flagged >= bar["min_cells"] and share >= bar["min_share"]
                         and tiles_hit >= bar["min_tiles"])}


def pending(unit) -> dict | None:
    """The unit's check not yet shown, when its next look is that check's:
    the same direction, from the same gate (`packets._pending_check` then
    matches its points)."""
    check = unit.get("classifier_check")
    if not check or check.get("asked") or check["direction"] != unit.get("direction") \
            or unit.get("candidate") is None or abs(float(check["at"]) - float(unit["candidate"])) > 1e-9:
        return None
    return check


def _reach(lat, candidate, direction) -> list:
    """Every point of the walk on that side, up to its anchor -- not a
    look's few nearest (`lattice.MAX_CHAIN`): those can all sit inside one
    half-band of the gate (p193 Ecadherin, 5.15 to 5.22), and the check
    would have no row to show."""
    from plexora.plugins.gating.server.autogate import lattice as latmod

    return latmod.chain(lat, candidate, direction, max_points=CHECK_REACH)


def check_points(ds, unit, candidate, chain) -> list:
    """The walk's points a check shows, nearest the gate first: each the
    first at least `CLASSIFIER["check"]["row_halves"]` half-bands beyond the
    one before, so no row is a sliver; the chain as it is without a fit."""
    from plexora.plugins.gating.server.classifier import signal

    need = schemas.CLASSIFIER["check"]["row_halves"]
    out, prev = [], float(candidate)
    for point in chain:
        apart = signal.halves_apart(ds, unit["marker"], prev, float(point["low"]))
        if apart is None:
            return list(chain)
        if apart >= need:
            out.append(point)
            prev = float(point["low"])
    return out


def check_rows(ds, unit, result, candidate, chain, direction, *, seed=0) -> list:
    """Per row of the walk on that side (nearest the gate first, at most
    `CLASSIFIER["check"]["rows"]`): a sample of the cells the classifier
    flags there and one of the cells it agrees on, each spread over the
    value range and the tissue, and which of the two rows (a or b) the
    flagged ones are shown as -- seeded, and never said in the packet."""
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import sampler
    from plexora.plugins.gating.server.autogate.views import _per_row

    bar = schemas.CLASSIFIER["check"]
    values = cellmod.values(ds, unit["marker"])
    c = cellmod.cells(ds)
    rng = np.random.default_rng(int(seed) + 7)
    per_row = _per_row("flips")
    bounds = [candidate] + [float(p["low"]) for p in chain[:bar["rows"]]]
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        lo, hi = sorted((a, b))
        _inside, flagged, control = _flags(result["arrays"], values, lo, hi, direction)
        picks = {}
        for name, mask in (("flagged", flagged), ("control", control)):
            rows = np.flatnonzero(mask & c.valid)
            picked = sampler._spread_by_value(c, values, rows, per_row, 8, rng)
            picks[name] = [int(r) for r in picked]
        out.append({"from": lo, "to": hi, "point": float(b), **picks,
                    "n_flagged": int(flagged.sum()), "n_control": int(control.sum()),
                    "flagged_is": "a" if rng.random() < 0.5 else "b"})
    return out


def reading_for(option, morphology) -> str:
    """The reading a check uses (`schemas.CHECK_READINGS`): `auto` is
    `word` for a fibrous stain -- a nucleus sits in stain wherever fibres or
    vessel walls cross it, so half-stained rows say little (p193 aSMA: dim
    haze along spindle nuclei read `own_stain` and `mixed`, the gate went
    to 30 % positive against the user's 16 %) -- and `mixed` for the rest."""
    if option != "auto":
        return option
    return "word" if morphology == "fibrous" else "mixed"


def confirms(direction, flagged_word, control_word=None, reading="word", cells=(1, 0)) -> bool:
    """Whether the gate walks past a row of a check. Walking a row turns
    every cell in it, so the row is read by its cells: each half reads the
    way the gate would move or not -- going down, its own stain (read
    `mixed`, a half read `mixed` too); going up, not its own -- and the
    halves that do must hold `row_share` of the row (`cells`: the flagged
    half's cells, then the other half's, as counted when the row was
    chosen). Both halves stained is a row of positives, a reason to go
    down, not to stay (p193 CD3E, 2026-10-19: `own_stain` beside `own_stain`
    held the gate at 5.94 against the user's 5.08 while the other half could
    veto). A stained flagged half that is a sliver of its row walks nothing
    (lsp11385 Ki67, 2026-10-20: 230 stained cells beside 28,217 unstained
    took the gate from 6.19 to 5.89, 18 % positive against the user's 3 %).
    Without `cells` the flagged half decides alone."""
    return _row_moves((_half_moves(direction, flagged_word, reading),
                       _half_moves(direction, control_word, reading)), cells)


def confirms_count(direction, flagged, n_flagged, control=None, n_control=0,
                   cells=(1, 0)) -> bool:
    """The same question read by counts (`check_reading: count`): a half
    reads the way the gate would move when, going down, at least
    `count_share` of its shown cells carry their own stain; going up, at
    most `1 - count_share` do. A `cannot_tell` (None) half reads nothing."""
    return _row_moves((_count_moves(direction, flagged, n_flagged),
                       _count_moves(direction, control, n_control)), cells)


def _half_moves(direction, word, reading) -> bool:
    from plexora.plugins.gating.server.autogate import verdicts

    if direction == "down":
        return word in (("own_stain", "mixed") if reading == "mixed" else ("own_stain",))
    return word in verdicts.NOT_OWN


def _count_moves(direction, stained, shown) -> bool:
    if stained is None or not shown:
        return False
    share = schemas.CLASSIFIER["check"]["count_share"]
    f = stained / shown
    return f >= share if direction == "down" else f <= 1 - share


def _row_moves(moved, cells) -> bool:
    total = sum(cells)
    held = sum(n for m, n in zip(moved, cells) if m)
    return bool(total) and held >= schemas.CLASSIFIER["check"]["row_share"] * total


# -- the session's job: read ahead, finish pending layers --------------------------------


def _state(record) -> dict:
    return record.setdefault("classifier", {"schema": 1})


def queue(engine, unit, *, layer=False) -> None:
    """Have the session's job build the unit's features (and, `layer`, its
    pending layer); starts the job when none is running."""
    state = _state(engine.record)
    key = _key(unit)
    queued = state.setdefault("queue", [])
    if key not in queued and key not in state.get("ready", []):
        queued.append(key)
    if layer:
        pending = state.setdefault("pending", [])
        if key not in pending:
            pending.append(key)
    kick(engine)


def after_issue(engine, packet) -> None:
    """A packet just went out: its markers' features are read ahead."""
    if mode(engine) == "off" or not packet:
        return
    units = engine.record.get("units") or {}
    for ref in packet.get("units") or ():
        unit = units.get(_key(ref)) if isinstance(ref, dict) and "marker" in ref else None
        if unit is not None and unit.get("state") not in schemas.TERMINAL_STATES:
            state = _state(engine.record)
            key = _key(unit)
            if key not in state.get("ready", []) and key not in state.setdefault("queue", []):
                state["queue"].append(key)
    state = _state(engine.record)
    if state.get("queue") or state.get("pending"):
        # Also what resumes layers a stopped process left pending.
        kick(engine)


def kick(engine) -> None:
    """Submit the session's classifier job unless one is running."""
    from plexora.agent import jobs

    state = _state(engine.record)
    held = state.get("job_id")
    if held:
        record = jobs.store().get(held)
        if record is not None and record.get("status") in ("queued", "running"):
            return
    try:
        state["job_id"] = submit_job(engine.call, engine.id)["job_id"]
    except Exception as exc:  # noqa: BLE001
        engine.log(event="classifier_error", error=f"job not started: {str(exc)[:160]}")


def submit_job(call, session_id):
    import dataclasses

    from plexora.agent import jobs
    from plexora.plugins.gating.capabilities_session import JobInput, _cap_by_name

    capability = _cap_by_name("gating.session_classifier")
    child = dataclasses.replace(call, capability=capability,
                                operation_id=f"{call.operation_id}.clf",
                                arguments={"session_id": session_id}, receipted=False,
                                extras=dict(call.extras))
    return jobs.submit(child, JobInput(session_id=session_id))


def run_job(call, session_id) -> dict:
    """The session's classifier job: features for every queued marker, then
    every pending layer, each merged into the session under its lock."""
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.classifier import features, runs

    done = {"features": 0, "layers": 0, "failed": 0}
    while True:
        if call.cancelled():
            break
        with engines.engine_for(call, session_id) as engine:
            state = _state(engine.record)
            units = engine.record.get("units") or {}
            work = None
            for key in list(state.get("pending", [])):
                unit = units.get(key)
                if unit is None or (unit.get("classifier") or {}).get("status") != "pending":
                    state["pending"].remove(key)
                    continue
                work = ("layer", key, unit["project"], unit["marker"], dict(unit["classifier"]))
                break
            if work is None:
                for key in list(state.get("queue", [])):
                    state["queue"].remove(key)
                    unit = units.get(key)
                    if unit is not None and key not in state.get("ready", []):
                        work = ("features", key, unit["project"], unit["marker"], None)
                        break
            if work is None:
                state.pop("job_id", None)
                break
            settings = _settings(engine)
            sid = engine.id
        kind, key, project, marker, pending = work
        ds = call.session.data(project)
        try:
            if kind == "features":
                features.feature_matrix(ds, marker, cancelled=call.cancelled)
                outcome = None
                done["features"] += 1
            else:
                low, high = pending["gate_used"]
                outcome = runs.session_run(
                    ds, marker, low=low, high=high, session_id=sid,
                    gate_source=pending.get("gate_source") or "session",
                    rule=settings["rule"], c_add=settings["c_add"], c_clean=settings["c_clean"],
                    band_halves=settings["band_halves"], seed=settings["seed"],
                    keep=bool(pending.get("store")), cancelled=call.cancelled)
                done["layers"] += 1
        except InterruptedError:
            break
        except Exception as exc:  # noqa: BLE001
            outcome = exc
            done["failed"] += 1
        with engines.engine_for(call, session_id) as engine:
            state = _state(engine.record)
            unit = (engine.record.get("units") or {}).get(key)
            if kind == "features":
                if isinstance(outcome, Exception):
                    engine.log(event="classifier_error", unit=key, error=str(outcome)[:200])
                elif key not in state.setdefault("ready", []):
                    state["ready"].append(key)
                    engine.log(event="classifier_prefetch", unit=key)
                continue
            if key in state.get("pending", []):
                state["pending"].remove(key)
            if unit is None or (unit.get("classifier") or {}).get("status") != "pending":
                continue
            if isinstance(outcome, Exception):
                unit["classifier"] = {"status": "failed", "reason": str(outcome)[:300]}
                engine.log(event="classifier_error", unit=key, error=str(outcome)[:200])
            elif (unit["classifier"].get("gate_used") or [None])[0] != low:
                continue    # the gate moved meanwhile; its own layer is pending
            else:
                _record(engine, unit, outcome["record"],
                        pending.get("gate_source") or "session")
                if outcome["record"].get("stored"):
                    _tell(call, project, marker)
    return {"session_id": session_id, **done}


def _tell(call, project, marker):
    """An open Thresholding panel redraws the marker's dot and layer."""
    if getattr(call, "notify", None) is not None:
        try:
            call.notify(project, "gating", "classifier.changed", {"marker": marker})
        except Exception:  # noqa: BLE001 -- a tab that went away
            pass


# -- the end of a session -----------------------------------------------------------------


def on_commit(engine) -> None:
    """A propose-mode session committed: every written unit's layer is made
    again and stored (seeded picks and fits: the same calls it reported)."""
    if mode(engine) == "off":
        return
    for unit in (engine.record.get("units") or {}).values():
        if not _layer_due(engine, unit) or (unit.get("written") is None
                                            and unit.get("state") != "skipped_manual"):
            continue
        summary = unit.get("classifier") or {}
        if summary.get("status") not in ("ready", "pending") or summary.get("stored"):
            continue
        unit["classifier"] = {"status": "pending", "gate_used": summary.get("gate_used"),
                              "gate_source": summary.get("gate_source") or "session",
                              "store": True}
        if summary.get("gate_used") is None:
            ds = _ds(engine, unit)
            gate = _gate_for(engine, unit, ds)
            if gate is None:
                unit.pop("classifier", None)
                continue
            unit["classifier"]["gate_used"] = [gate[0], gate[1]]
        queue(engine, unit, layer=True)


def on_rollback(engine) -> list:
    """A session rolled back: its job stopped and its layers dropped."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.classifier import runs

    state = _state(engine.record)
    if state.get("job_id"):
        try:
            jobs.store().cancel(state["job_id"])
        except Exception:  # noqa: BLE001
            pass
    touched = []
    for project in engine.record.get("images") or ():
        try:
            touched += runs.discard_session_runs(engine.call.session.data(project), engine.id)
        except Exception as exc:  # noqa: BLE001
            engine.log(event="classifier_error", error=f"rollback: {str(exc)[:160]}")
    return touched


def on_cancel(engine) -> None:
    from plexora.agent import jobs

    state = _state(engine.record)
    if state.get("job_id"):
        try:
            jobs.store().cancel(state["job_id"])
        except Exception:  # noqa: BLE001
            pass


def status_block(record) -> dict:
    """Per marker, what the session's classifier did: for status and report."""
    out = {}
    for key, unit in (record.get("units") or {}).items():
        summary = unit.get("classifier")
        checked = unit.get("classifier_check")
        if not summary and not checked and not unit.get("classifier_check_skipped"):
            continue
        entry = {"marker": unit.get("marker"), "project": unit.get("project")}
        if summary:
            compare = summary.get("compare") or {}
            entry.update({"status": summary.get("status"), "why": summary.get("why"),
                          "gate_used": summary.get("gate_used"),
                          "gate_source": summary.get("gate_source"),
                          "grade": (summary.get("snr") or {}).get("grade"),
                          "added": compare.get("n_added"), "removed": compare.get("n_removed"),
                          "raw_added": compare.get("n_raw_added"),
                          "raw_removed": compare.get("n_raw_removed"),
                          "stored": summary.get("stored")})
        if checked:
            entry["check"] = {"direction": checked["direction"], "from": checked["at"],
                              "to": unit.get("final"), "share": checked.get("share"),
                              "confirmed": checked.get("confirmed")}
        elif unit.get("classifier_check_skipped"):
            entry["check_skipped"] = unit["classifier_check_skipped"]
        out[key] = entry
    return out
