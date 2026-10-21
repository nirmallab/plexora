"""Candidate gates as competing hypotheses, chosen by blind cell panels.

`SessionOptions.engine = "candidates"`. A marker that needs a look (not a
clean split the audit sheet settles, not a conditional gate) is decided
here instead of by the classic below/at/above rows and the candidates
beyond them:

1. The unit's frozen lattice (`lattice.build`) gives the candidates
   (`candidates.build`): every estimator, the scored start, partner points,
   and the steps around them. The gate written is always one of them. A
   lattice that holds one point after thinning gets steps either side
   (`_steps`), so a lone candidate is tested, never accepted unseen. A rare
   marker (`PARAMS["rare_fraction"]`, or the `long_tail` class) is marked
   so: its leader must have its own cells read stained (`controller.tail`).
2. Each look is a `cell_panel` packet (`packet`): numbered cells from the
   disputed intervals, a few negative sentinels, shuffled, no value, call or
   gate anywhere. The reader labels each cell (`answers.CellPanelAnswer`).
3. Code scores the candidates by the cells each would get wrong
   (`evidence.support`) and decides (`controller.decide`): resolved when the
   leader beats every rival outside the tolerance (VERSION 5), or when the
   posterior over candidates puts P >= `resolve_p` within the tolerance of
   the plateau member nearest the start (VERSION 6), else another novel
   look (`controller.next_action`) or review with a stated reason.
4. A resolved gate passes the same numeric checks as any other
   (`Engine.run_regression`) and is written with them; a failed check caps
   its confidence, it never moves the gate (`settle`).

Every observation, the candidates and the terminal status are kept on the
unit (`unit["hypotheses"]`) and in the gate's provenance.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import tableops
from plexora.plugins.gating.server.autogate.hypotheses import candidates as candmod
from plexora.plugins.gating.server.autogate.hypotheses import controller, evidence
from plexora.plugins.gating.server.autogate.hypotheses import panel as panelmod

#: Bumped whenever the same answers would choose a different gate ("3": a
#: rival's cells are material relative to a rare population, rare markers
#: confirm their own cells, a single candidate gets steps; "4": steps past
#: the lattice's edge, wide mixed intervals split, unreadable takes two
#: looks; "5": an interval whose reads are ordered by value is split too,
#: and a marker out of looks may split on the reads in hand; "6": the
#: posterior over candidates decides and stops, looks go where they could
#: change it, added points lead only once read on both sides, no split at
#: close, crowded and isolated cells are weighed apart).
VERSION = "6"

#: The VERSION 5 rules, for a bench arm or a replay of stored records
#: (`tools/bench_gates/ablate.py ABL=v5`, `replay_v6.py`).
V5_RULES = {"decision": "wilson", "sampling": "survey_refine", "split_at_close": True,
            "added_min_reads": 0, "strata": None, "uncertain_state": False,
            "positive_sentinels": 0}

#: [cal] the controller's parameters (unmeasured until the dev bench).
PARAMS = {
    # Disputed cells a look shows (bounds 12 to 24), plus the sentinels.
    "panel_cells": 20,
    "sentinels": 4,
    # A look whose sentinels are read stained this often is discarded.
    "sentinel_misses": 2,
    # Looks per marker before it closes for review, and discarded looks
    # that mark the reader unreliable.
    "looks_max": 3,
    "invalid_looks": 2,
    # Fit units within which two candidates need not be told apart: the
    # user's own retest noise.
    "tolerance": 0.2,
    # Resolved once the leader beats every rival farther than this: a rival
    # nearer may stay undecided (the stain's edge can lie between two close
    # candidates, whose cells are then mixed by nature). lsp11385_2, one run
    # (2026-10-21): with every rival past the tolerance required, no marker
    # resolved, CD3e and CD8a included, ending 0.01 and 0.02 from the user.
    "resolve_within": 0.3,
    # Candidates nearer than this (fit units) are one: the lattice packs
    # points a few hundredths apart in the background.
    "min_gap": 0.1,
    # A rival whose call differs from the leader's on fewer cells than this
    # is the same gate in effect (the lattice merges intervals below it) --
    # unless they are more than this share of the larger candidate's
    # positives (`evidence.material`): a rare population can be smaller than
    # `min_between` whole.
    "min_between": 20,
    "min_between_share": 0.25,
    # A marker positive in fewer than this share of its cells at the start,
    # or of the `long_tail` class, is rare: its leader must have its own
    # cells read stained (`controller.tail_status`).
    "rare_fraction": 0.02,
    # Most labels uncertain or artifact (once this many cells were read, over
    # at least this many looks): the marker is unreadable this way. One look
    # is not enough: lsp11385_2 (2026-10-21) closed four markers after one
    # panel of crowded immune cells.
    "min_labelled": 10,
    "max_other_share": 0.5,
    "unreadable_looks": 2,
    # A leader on the lattice's own highest or lowest point: a point this far
    # beyond it (fit units, past the tolerance), at most this many a side.
    "edge_step": 0.25,
    "edge_steps": 2,
    # An interval beside the leader wider than this (fit units: twice the
    # tolerance, so the person's gate can lie farther than the tolerance from
    # both ends) whose cells are mixed -- an own-stain share inside
    # `split_share` on at least `split_reads` definite reads -- is cut into
    # parts about the tolerance wide, at most `split_parts`.
    "split_width": 0.4,
    "split_share": [0.2, 0.8],
    "split_reads": 6,
    "split_parts": 4,
    # ... or whose stained reads lie above its unstained ones: at least this
    # many of each, a one-sided rank test at this level.
    "split_order_min": 2,
    "split_order_p": 0.05,
    # A marker out of looks splits an interval whose reads are ordered, on
    # the reads in hand, once, and closes for review at the leader among the
    # finer points: the order may be plain only after the last look. Not on
    # a mixed share alone, which with no look to come may be the reader's
    # slips. lsp11385_2 replay (2026-10-21): ordered only, CD163 +0.38 to
    # +0.18 and nothing else moved; with mixed shares too, SMA +0.19 to
    # +0.43 and a run-1 CD163 +0.38 to -0.36. Off in VERSION 6: a point cut
    # on the reads in hand is decided by the few reads that drew it (run 1
    # CD8a), and it may not lead before it is read (`added_min_reads`).
    "split_at_close": False,
    # -- VERSION 6 (docs/internal/bench/CANDIDATES_PLAN.md, "VERSION 6") --
    # How the leader is chosen and when it stands: `posterior` (the plateau
    # of the posterior over candidates, its member nearest the start,
    # resolved at P(within the tolerance) >= `resolve_p`) or `wilson`
    # (VERSION 5: the loss argmin, every rival beaten). `plateau` is "at
    # least one chance in ten"; `resolve_p` the moderate/high boundary of
    # `calibration.LEVELS`. Neither was chosen on lsp11385_2 (where they
    # were explored); both are confirmed on the synthetic bench and the dev
    # campaign.
    "decision": "posterior",
    "plateau": 0.1,
    "resolve_p": 0.8,
    "draws": 400,
    # A point a step or a split added may lead once each side of it in the
    # range has this many cells read either way (a fifth of a look).
    "added_min_reads": 5,
    # Later looks by what they could change (`voi`), or by the classic
    # survey-and-refine (`survey_refine`): at most `voi_cap` cells from one
    # interval, `voi_floor` for an interval never shown; the expected gain
    # is over `voi_batch` more reads per interval, and a whole look's over
    # `voi_simulations` simulated looks. A look that could not raise the
    # best P(within the tolerance) by `voi_epsilon` is not taken: the
    # marker closes for review (`looks_uninformative`).
    "sampling": "voi",
    "voi_cap": 8,
    "voi_floor": 1,
    "voi_batch": 8,
    "voi_simulations": 200,
    "voi_epsilon": 0.02,
    # Reads weighed by crowded vs isolated cells (`controller.crowding`):
    # crowded = the brightest of the six nearest neighbours brighter by more
    # than the tolerance; not when either kind has fewer reads than this.
    "strata": "crowding",
    "strata_min_reads": 5,
    # `uncertain` is a state: a marker whose crowded cells hold most of the
    # unreadable reads may resolve on its isolated cells alone (flagged).
    "uncertain_state": True,
    # Clear positives among the sentinels (taking that many of `sentinels`):
    # off until the reader audit on the person's labelled cells (E0.5).
    "positive_sentinels": 0,
}

#: Terminal status -> the unit's closed state (a review state writes the
#: gate at the leader, tagged `needs_review`).
CLOSE_STATE = {status: "manual_review_recommended" for status in controller.STATUSES
               if status != "resolved"}
CLOSE_STATE["no_identifiable_boundary"] = "not_binary"

#: [cal] Steps (fit units) either side of a lattice that holds one point
#: after thinning: past the tolerance, so each is a distinct hypothesis.
SINGLE_STEPS = (0.25, 0.5)


def active(engine, unit) -> bool:
    """Whether this unit's looks are cell panels. Not a conditional gate:
    only a classic look's `within` answer makes one, and a `within:` fit is
    a lattice point the panels may choose among the rest. Not ported:
    5 of 543 stored marker-images were conditional, and on the 3 held-out
    the conditional gate was farther from the person's than the plain one
    twice (CD3e 1.43 vs 0.89; PD1 0.16 vs 0.11; CD20L 0.10 vs 0.19)."""
    return engine.options["engine"] == "candidates" and not unit.get("condition") \
        and (unit.get("hypotheses") or {}).get("status") != "no_candidates"


def _column(engine, unit):
    from plexora.plugins.gating.server.autogate import profile as profmod

    ds = engine.call.session.data(unit["project"])
    return ds, profmod.column(ds, unit["marker"])


def start(engine, unit) -> dict:
    """The unit's hypotheses from its frozen lattice, built once."""
    from plexora.plugins.gating.server.autogate.engine import start_low

    state = unit.get("hypotheses")
    if state is not None and state.get("version") == VERSION:
        return state
    lat = engine.lattice_for(unit)
    ds, col = _column(engine, unit)
    begin = start_low(unit)
    top = float(col.sorted32[-1]) if unit.get("high") is None else float(unit["high"])
    lattice_points = sorted(lat.get("points") or [], key=lambda p: p["low"])
    added = []
    if len(candmod.thin(lattice_points, col.to_fit, PARAMS["min_gap"])) < 2:
        added = _steps(ds, unit["marker"], col, lattice_points, begin, top)
        lattice_points = sorted(lattice_points + added, key=lambda p: p["low"])
    cands = candmod.build({"points": lattice_points}, begin, col.to_fit,
                          PARAMS["tolerance"], min_gap=PARAMS["min_gap"])
    points = [{"low": float(p["low"]), "n_positive": int(p["n_positive"]), "id": p["id"],
               "sources": list(p["sources"])}
              for p in candmod.thin(lattice_points, col.to_fit, PARAMS["min_gap"])]
    if cands["window"] is None or len(cands["bounds"]) < 2:
        unit["hypotheses"] = {"version": VERSION, "status": "no_candidates",
                              "reason": "the lattice has fewer than two candidate gates"}
        return unit["hypotheses"]
    params = {**PARAMS, "looks_max": int(engine.options["panel_looks"])}
    at_start = points[int(cands["prior"])]["n_positive"] / max(1, col.n_finite)
    rare = at_start < params["rare_fraction"] or unit.get("class") == "long_tail"
    unit["hypotheses"] = {
        "version": VERSION, "seed": _unit_seed(engine, unit),
        "points": points, "window": list(cands["window"]),
        "prior": int(cands["prior"]), "start": begin, "top": top,
        "n_finite": int(col.n_finite), "grown": {"up": 0, "down": 0}, "split": [],
        "clusters": cands["clusters"], "spread": candmod.spread(cands, col.to_fit),
        "lattice": lat.get("fingerprint"), "observations": [], "status": None,
        "rare": ({"fraction_at_start": round(at_start, 5), "class": unit.get("class")}
                 if rare else None),
        **({"added_points": [p["id"] for p in added]} if added else {}),
        "params": params}
    return unit["hypotheses"]


def _steps(ds, marker, col, lattice_points, begin, top) -> list:
    """Points `SINGLE_STEPS` either side of a lattice's only point (or the
    start), snapped to the sidebar's grid like every lattice point: a single
    candidate has nothing to be told apart from, so it is tested against its
    neighbours instead of accepted unseen."""
    from plexora.plugins.gating.server import model

    base = lattice_points[0]["low"] if lattice_points else begin
    if base is None:
        return []
    desc = model._description(ds).get(marker) or {}
    g0 = float(col.to_fit(base))
    taken = {round(float(p["low"]), 9) for p in lattice_points}
    out = []
    for step in SINGLE_STEPS:
        for sign, name in ((1, "up"), (-1, "down")):
            value = float(col.from_fit(g0 + sign * step))
            if desc:
                value = model.snap_to_grid(value, top, desc)[0]
            if round(value, 9) in taken or not value < top:
                continue
            taken.add(round(value, 9))
            point_id = f"step:{name}{step:g}"
            out.append({"id": point_id, "low": float(value), "sources": [point_id],
                        "n_positive": int(col.n_positive(value, top))})
    return out


def _shown_ids(state) -> list:
    return [int(c["cell_id"]) for o in state["observations"] for c in o.get("cells") or ()]


def packet(engine, units):
    """The next panel for the unit, or None when the controller stopped (the
    unit is closed then)."""
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import packets, views

    unit = units[0]
    state = start(engine, unit)
    ds, col = _column(engine, unit)
    plan = state.pop("pending", None) or controller.next_action(state, col.to_fit,
                                                                state["params"])
    # A change to the range comes first (each records itself, so none
    # repeats), then the look at the cells it adds.
    for _ in range(4):
        if plan is None or plan["kind"] not in ("extend", "step", "split") \
                or plan.get("intervals"):
            break
        plan = _grow(engine, unit, ds, col, state, plan)
    if plan is None:
        finish(engine, unit, "unresolved_evidence")
        return None
    bounds, _ = controller.window(state)
    params = state["params"]
    # Clear positives take their share of the sentinels (never for a rare
    # marker, whose tail look reads its brightest cells as evidence).
    positive = 0 if state.get("rare") else int(params.get("positive_sentinels") or 0)
    above_top = float(col.from_fit(float(col.to_fit(state["points"][-1]["low"]))
                                   + float(params["tolerance"])))
    # Every look records which cells a brighter neighbour crowds (a record,
    # and what the bench's reader reads by); `strata` says whether the
    # reads are weighed by it (`controller.crowding`).
    sampled = tableops.local_or_node(ds, "gating.autogate.panel_cells", {
        "marker": unit["marker"],
        "intervals": [{"from": iv["from"], "to": iv["to"], "n": iv["n"]}
                      for iv in plan["intervals"]],
        "exclude": _shown_ids(state), "n_sentinels": int(params["sentinels"]) - positive,
        "sentinel_below": bounds[0], "seed": _seed(engine, state),
        "ids": plan.get("ids") or [],
        "n_positive_sentinels": positive, "sentinel_above": above_top if positive else None,
        "strata_bounds": [float(b) for b in bounds],
        "crowding_margin": float(params["tolerance"])})
    if sampled.get("crowding") is not None:
        state["crowding"] = {"bounds": [float(b) for b in bounds], "sizes": sampled["crowding"]}
    for cell in sampled["cells"]:
        if cell.get("interval") is not None:
            cell["interval"] = plan["intervals"][cell["interval"]]["interval"]
    seed = _seed(engine, state)
    arranged = panelmod.arrange(sampled["cells"], sampled["sentinels"], seed)
    if not arranged:
        finish(engine, unit, "unresolved_evidence")
        return None
    channel = views.image_channel(ds, unit["marker"])
    partners = engine.partner_panels(unit)
    layout = "t2m" if partners else "t2"
    per_row = views._per_row(layout)
    # The picture's manifest is served to any client (`get_artifact`): it
    # carries where each cell is and its number, never its value or why it
    # was picked (a sentinel's "clear negative"); `_panel` keeps those.
    rows = panelmod.rows_of([{k: c[k] for k in ("cell_id", "x", "y", "caption", "number")}
                             for c in arranged], per_row)
    main = collage.render_collage(
        engine.call.session, ds, layout=layout, rows=rows, marker=channel, gate=None,
        high=unit.get("high"), fmt=packets._fmt(engine),
        pixel=engine.pixel_for(ds.name), partners=partners or None,
        title=f"{unit['marker']} · numbered cells in random order (each cell: "
              f"{packets._panel_words(layout, partners) or 'morphology | context'})")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    compartment = (unit.get("context") or {}).get("compartment")
    # Which cells carry the marker, so a dim population of its own is read as
    # stained; not the caveats, which can speak of gates.
    lineage = (unit.get("context") or {}).get("lineage")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    numbers = [c["number"] for c in arranged]
    multiview = ({"partner_panels": {"expected": partners["markers"].get("co"),
                                     "not_expected": partners["markers"].get("anti")}}
                 if partners else {})
    guide = ["panel", "collage_panel_m" if partners else "collage_panel"]
    fibrous = ((engine.panel_for(unit["project"]).get("entries") or {})
               .get(unit["marker"]) or {}).get("morphology") == "fibrous"
    if fibrous:
        guide.append("panel_fibrous")
    pkt = {
        "question": (f"{unit['marker']} ({ds.name}): for EVERY numbered cell, does it carry "
                     f"its OWN {stain} stain, around the nucleus the ticks mark? One word per "
                     "cell."),
        "evidence": {"marker": unit["marker"], "project": ds.name,
                     "cells": len(numbers), **multiview,
                     "context": {"compartment": compartment,
                                 **({"lineage": lineage} if lineage else {})},
                     "look": len(state["observations"]) + 1,
                     **packets._reading(engine, guide)},
        "allowed": {"cells": numbers, "labels": list(evidence.LABELS)},
        "_image_meta": [],
        "_panel": {"plan": plan, "cells": [
            {"number": c["number"], "cell_id": int(c["cell_id"]), "value": float(c["value"]),
             **({"sentinel": True, "side": c.get("side") or "negative"} if c.get("sentinel")
                else {"interval": c.get("interval")}),
             **({"crowded": int(c["crowded"])} if c.get("crowded") is not None else {})}
            for c in arranged]},
    }
    images = packets._images(pkt, ((main, "panel_collage", "numbered cells, random order"),))
    unit["last_manifest"] = main["manifest"]
    return pkt, images


def _grow(engine, unit, ds, col, state, plan) -> dict | None:
    """Make the range change `plan` names and return the look at its new
    cells, or the next action when it added nothing."""
    total = int(state["params"]["panel_cells"])
    if plan["kind"] == "extend":
        new = controller.extend(state, plan["side"])
        bounds, n_cells = controller.window(state)
        k = new["interval"]
        out = {"kind": "extend", "side": plan["side"],
               "intervals": [{"interval": k, "from": bounds[k], "to": bounds[k + 1],
                              "n": min(total, n_cells[k])}]}
        if out["intervals"][0]["n"]:
            return out
    elif plan["kind"] == "step":
        added = _step(ds, unit["marker"], col, state, plan["side"])
        if added:
            bounds, n_cells = controller.window(state)
            k = 0 if plan["side"] == "down" else len(n_cells) - 1
            return {"kind": "step", "side": plan["side"],
                    "intervals": [{"interval": k, "from": bounds[k], "to": bounds[k + 1],
                                   "n": min(total, n_cells[k])}]}
    elif plan["kind"] == "split":
        added = _split(ds, unit["marker"], col, state, plan["from"], plan["to"])
        if added:
            bounds, n_cells = controller.window(state)
            seen = controller.shown(state)
            ks = [k for k in range(len(n_cells))
                  if plan["from"] <= bounds[k] and bounds[k + 1] <= plan["to"]]
            available = [max(0, n_cells[k] - seen[k]) for k in ks]
            picks = controller.allocate([n_cells[k] for k in ks], available, total)
            out = controller._plan(state, ks, picks, "split")
            if out is not None:
                return out
    return controller.next_action(state, col.to_fit, state["params"])


def _point(ds, marker, col, fit_value, top, family):
    """A point at `fit_value` (fit units), snapped to the sidebar's grid like
    a lattice point, with its positive count."""
    from plexora.plugins.gating.server import model

    desc = model._description(ds).get(marker) or {}
    value = float(col.from_fit(fit_value))
    if desc:
        value = float(model.snap_to_grid(value, top, desc)[0])
    point_id = f"{family}:{value:g}"
    return {"id": point_id, "low": value, "sources": [point_id],
            "n_positive": int(col.n_positive(value, top))}


def _insert(state, new, side=None):
    """Join `new` points to the range, keeping the window's ends (or moving
    the end on `side` to the new point) and the prior where they were."""
    points = state["points"]
    lo, hi = state["window"]
    keep_lo, keep_hi = points[lo]["low"], points[hi]["low"]
    keep_prior = points[min(max(int(state["prior"]), 0), len(points) - 1)]["low"]
    if side == "up":
        keep_hi = max(p["low"] for p in new)
    elif side == "down":
        keep_lo = min(p["low"] for p in new)
    merged = sorted(points + list(new), key=lambda p: p["low"])
    where = {p["low"]: i for i, p in enumerate(merged)}
    state["points"] = merged
    state["window"] = [where[keep_lo], where[keep_hi]]
    state["prior"] = where[keep_prior]
    state.setdefault("added_points", []).extend(p["id"] for p in new)


def _distinct(state, col, value, min_gap) -> bool:
    fit = float(col.to_fit(value))
    return all(abs(fit - float(col.to_fit(p["low"]))) >= min_gap - 1e-9
               for p in state["points"])


def _step(ds, marker, col, state, side) -> list:
    """One point `edge_step` beyond the lattice's edge on `side`, if it adds
    enough cells and is a gate the column can hold; every try is counted."""
    params = state["params"]
    grown = state.setdefault("grown", {"up": 0, "down": 0})
    grown[side] = int(grown.get(side) or 0) + 1
    lo, hi = state["window"]
    edge = state["points"][hi if side == "up" else lo]
    sign = 1.0 if side == "up" else -1.0
    new = _point(ds, marker, col, float(col.to_fit(edge["low"])) + sign * params["edge_step"],
                 float(state["top"]), "step")
    cells = (edge["n_positive"] - new["n_positive"]) if side == "up" \
        else (new["n_positive"] - edge["n_positive"])
    if not new["low"] < float(state["top"]) or cells < evidence.MIN_MATERIAL \
            or not _distinct(state, col, new["low"], params["min_gap"]):
        grown[side] = int(params["edge_steps"])
        return []
    _insert(state, [new], side)
    return [new]


def _split(ds, marker, col, state, a, b) -> list:
    """Points cutting the interval (a, b] into parts about the tolerance
    wide (at most `split_parts`), evenly in the fit space; recorded so the
    interval is never split again."""
    import math

    params = state["params"]
    state.setdefault("split", []).append([round(a, 6), round(b, 6)])
    fa, fb = float(col.to_fit(a)), float(col.to_fit(b))
    parts = min(int(params["split_parts"]),
                max(2, math.ceil((fb - fa) / float(params["tolerance"]) - 1e-9)))
    new = []
    for k in range(1, parts):
        point = _point(ds, marker, col, fa + (fb - fa) * k / parts, float(state["top"]),
                       "split")
        if a < point["low"] < b and _distinct(state, col, point["low"], params["min_gap"]) \
                and all(abs(float(col.to_fit(point["low"])) - float(col.to_fit(p["low"])))
                        >= params["min_gap"] - 1e-9 for p in new):
            new.append(point)
    if new:
        _insert(state, new)
    return new


def _seed(engine, state) -> int:
    return int(engine.options["seed"]) * 1009 + len(state["observations"]) * 7919 + 17


def _unit_seed(engine, unit) -> int:
    """The posterior's draws for this unit: fixed by the session's seed, the
    image and the marker, so a replay draws the same."""
    import zlib

    key = f"{unit.get('project')}::{unit.get('marker')}".encode()
    return (int(engine.options["seed"]) * 100_003 + zlib.crc32(key)) & 0x7FFFFFFF


def apply(engine, packet, answer) -> dict:
    """Record the labels as one observation; decide; ask again, finish, or
    close."""
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import transitions

    unit = transitions._units(engine, packet)[0]
    state = start(engine, unit)
    shown = (packet.get("_panel") or {}).get("cells") or []
    numbers = [c["number"] for c in shown]
    missing = [n for n in numbers if n not in answer.cells]
    if missing:
        raise AgentError("invalid_input", "label EVERY numbered cell",
                         detail={"missing": missing, "allowed": numbers})
    transitions._note(unit, answer)
    cells = [dict(c, label=answer.cells[c["number"]]) for c in shown]
    valid, record = sentinel_check(cells, int(state["params"]["sentinel_misses"]))
    state["observations"].append({
        "packet_id": packet.get("packet_id"), "plan": (packet.get("_panel") or {}).get("plan"),
        "cells": cells, "valid": valid, "sentinels": record})
    return step(engine, unit)


def sentinel_check(cells, bar) -> tuple:
    """(valid, record) of one look's labelled cells: discarded when `bar`
    clear negatives were read stained, or `bar` clear positives (all of
    them, when fewer were shown) read unstained. `artifact` on a sentinel
    is not a miss."""
    sentinels = [c for c in cells if c.get("sentinel")]
    negatives = [c for c in sentinels if (c.get("side") or "negative") == "negative"]
    positives = [c for c in sentinels if c.get("side") == "positive"]
    misses = sum(1 for c in negatives if c["label"] == evidence.OWN)
    missed = sum(1 for c in positives if c["label"] == evidence.NOT_OWN)
    valid = misses < bar and (not positives or missed < min(bar, len(positives)))
    return valid, {"n": len(sentinels), "read_stained": misses,
                   **({"positive": len(positives), "read_unstained": missed}
                      if positives else {})}


def reader(state) -> dict:
    """How the reader did on the sentinels over the marker's looks: the
    share of clear negatives read stained (`fpr`), of clear positives read
    unstained (`fnr`, when any were shown), and the looks discarded. A
    record and a calibration feature; never evidence about a gate."""
    neg = pos = fp = fn = 0
    for obs in state.get("observations") or ():
        for cell in obs.get("cells") or ():
            if not cell.get("sentinel"):
                continue
            if cell.get("side") == "positive":
                pos += 1
                fn += cell.get("label") == evidence.NOT_OWN
            else:
                neg += 1
                fp += cell.get("label") == evidence.OWN
    return {"negatives": neg, "fpr": round(fp / neg, 4) if neg else None,
            "positives": pos, "fnr": round(fn / pos, 4) if pos else None,
            "invalid_looks": sum(1 for o in state.get("observations") or ()
                                 if not o.get("valid", True))}


def step(engine, unit) -> dict:
    """Decide after an observation: keep looking, or finish."""
    from plexora.plugins.gating.server.autogate import transitions

    state = unit["hypotheses"]
    ds, col = _column(engine, unit)
    status, s = controller.decide(state, col.to_fit, state["params"],
                                  continuous=bool(unit.get("continuous")))
    if status == "budget_exhausted" and s is not None and state["params"].get("split_at_close"):
        s = _split_at_close(ds, unit["marker"], col, state, s)
    if s is not None:
        state["leader"] = s["low"]
        state["support"] = {k: s[k] for k in ("loss", "fractions", "rivals", "labelled",
                                              "other_share")}
        state["support"].update({k: s[k] for k in ("rule", "posterior", "strata_reads", "voi")
                                 if k in s})
        if s.get("isolated_only"):
            state["isolated_only"] = True
        else:
            state.pop("isolated_only", None)
    state["reader"] = reader(state)
    if status is not None:
        finish(engine, unit, status)
    if unit["state"] == "awaiting_panel":
        # Still looking (or a classifier check's look is next): nothing that
        # says where any gate is goes back to the reader, whose next panel
        # must stay blind.
        return {"unit": f"{unit['project']}::{unit['marker']}", "state": unit["state"],
                "look": len(state["observations"])}
    return transitions._outcome(unit, status=status, leader=state.get("leader"))


def _split_at_close(ds, marker, col, state, s):
    """A marker out of looks cuts a wide interval beside its leader whose
    reads are ordered (`controller.split_plan`, not on a mixed share), once,
    and takes the leader among the finer points on the reads in hand: no
    cell is shown, and the marker still closes for review. The support
    after it, or `s`."""
    params = state["params"]
    plan = controller.split_plan(state, s, col.to_fit, params, mixed=False)
    if plan is None or not _split(ds, marker, col, state, plan["from"], plan["to"]):
        return s
    after = controller.support(state, col.to_fit, params)
    if after is None:
        return s
    state["split_at_close"] = {"from": plan["from"], "to": plan["to"],
                               "leader_before": s["low"], "leader_after": after["low"]}
    return after


def finish(engine, unit, status):
    """Write the unit's outcome: a resolved leader goes through the numeric
    checks (`settle`); anything else closes for review at the leader."""
    state = unit["hypotheses"]
    state["status"] = status
    leader = state.get("leader")
    if status == "resolved":
        from plexora.plugins.gating.server.autogate.hypotheses import classifier_check

        check = classifier_check.plan(engine, unit, state, _shown_ids(state))
        if check is not None:
            # One more look before anything is written, and the last: the
            # classifier's disagreements on both sides, among cells it did
            # not choose. Its strata hold for this range only.
            state["status"] = None
            state["pending"] = check
            state["params"]["looks_max"] = len(state["observations"]) + 1
            return
        posterior = (state.get("support") or {}).get("posterior")
        state["reason"] = (
            f"the reads put P {posterior['within']:.2f} on the best gate lying within the "
            "tolerance of the leading candidate" if posterior else
            "the leading candidate beat every rival outside the tolerance")
        if state.get("isolated_only"):
            state["reason"] += (" -- on the isolated cells' reads alone: most crowded cells "
                                "could not be read")
        unit["candidate"] = float(leader)
        unit["path"] = "panel"
        unit["method"] = "ai_panel"
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return
    state["reason"] = controller.REASONS.get(status, status)
    if status != "readers_unreliable":
        state["reason"] += plateau_note(state)
    proposed = leader if leader is not None and status != "readers_unreliable" \
        else state.get("start")
    engine.close(unit, CLOSE_STATE[status], f"cell panels: {state['reason']}",
                 proposed=proposed)


def plateau_note(state) -> str:
    """For a review note: the candidates the reads still leave, with how
    likely each is the best gate ("; the reads leave 7.31 (P 0.55) and
    7.69 (P 0.35)"), or "" with one candidate or no posterior."""
    plateau = ((state.get("support") or {}).get("posterior") or {}).get("plateau") or []
    if len(plateau) < 2:
        return ""
    parts = [f"{m['low']:.0f} (P {m['p']:.2f})" if abs(m["low"]) >= 100 else
             f"{m['low']:.3g} (P {m['p']:.2f})"
             for m in sorted(plateau, key=lambda m: -m["p"])]
    return "; the reads leave " + ", ".join(parts[:-1]) + " and " + parts[-1]


def settle(engine, unit):
    """The numeric checks on a resolved leader: written whatever they say,
    a failure capping its confidence (`confidence`), never moving it."""
    result = engine.run_regression(unit)
    if "lineage" in result["failed"]:
        # As on the classic path: the whole image is shown before a
        # population on an excluding lineage is closed (`lineage_by_eye`).
        if not engine.lineage_by_eye(unit):
            engine.close_contaminated(unit)
        return
    if unit["state"] == "awaiting_regression":
        engine.finalize(unit, method=unit.get("method") or "ai_panel")


def confidence(unit) -> str:
    """The panel path's confidence, before the shared caps
    (`engine.confidence_for`). With a frozen calibration table
    (`calibration.load`), the level of P(|gap| <= 0.2) its features give;
    without one, a rule tagged `uncalibrated`. Under the posterior rule:
    the level (`calibration.LEVELS`) of P(the best gate lies within the
    tolerance of the leader), capped at moderate when the candidates'
    estimators disagree or the gate stood on isolated cells alone. Under
    the Wilson rule: high when every rival outside the tolerance was beaten
    by a clear margin -- its interval at least 0.1 past one half on the
    leader's side -- and the estimators agree; moderate when resolved. Low
    for anything not resolved."""
    from plexora.plugins.gating.server.autogate.hypotheses import calibration

    state = unit.get("hypotheses") or {}
    if state.get("status") != "resolved":
        return "low"
    table = calibration.load()
    row = calibration.features(state, table.get("features")) if table else None
    if row is not None:
        p = calibration.predict(table, row)
        state["calibrated"] = {"version": table["version"], "p_within": round(p, 3)}
        return calibration.level(p)
    agreed = (state.get("spread") or 0.0) <= 2 * float(state["params"]["tolerance"])
    posterior = (state.get("support") or {}).get("posterior")
    if posterior is not None:
        level = calibration.level(float(posterior["within"]))
        if (state.get("isolated_only") or not agreed) and level == "high":
            level = "moderate"
        return level
    rivals = (state.get("support") or {}).get("rivals") or []
    leader = float(state.get("leader") or 0.0)
    # The signed distance of each rival's interval from one half on the
    # leader's side: an undecided rival (an interval across one half) is
    # negative, never "clear" (it was the distance to the nearer end before
    # 2026-10-21, so [0.35, 0.68] counted as a clear 0.15).
    margins = [(r["ci"][0] - 0.5) if r["low"] > leader else (0.5 - r["ci"][1]) for r in rivals]
    clear = not margins or min(margins) >= 0.1
    return "high" if clear and agreed else "moderate"


def provenance(unit) -> dict | None:
    """What the gate's provenance keeps of the hypotheses."""
    state = unit.get("hypotheses")
    if not state:
        return None
    return {"version": state.get("version"), "status": state.get("status"),
            "reason": state.get("reason"), "leader": state.get("leader"),
            "start": state.get("start"), "clusters": state.get("clusters"),
            "rare": state.get("rare"), "added_points": state.get("added_points"),
            "observations": [{"packet_id": o.get("packet_id"), "valid": o.get("valid"),
                              "kind": (o.get("plan") or {}).get("kind"),
                              "n_cells": len(o.get("cells") or ())}
                             for o in state.get("observations") or ()],
            "confidence": ({"calibrated": state["calibrated"]} if state.get("calibrated")
                           else "uncalibrated"),
            **_posterior_summary(state)}


def _posterior_summary(state) -> dict:
    """The provenance's view of the VERSION 6 record: which gates the reads
    left and how likely each, P(within the tolerance) of the gate written,
    the reader's sentinel rates, and the flags."""
    support = state.get("support") or {}
    posterior = support.get("posterior")
    out = {}
    if posterior:
        out["posterior"] = {"within": posterior.get("within"),
                            "plateau": [{"low": m["low"], "p": m["p"]}
                                        for m in posterior.get("plateau") or ()],
                            "non_monotone": posterior.get("non_monotone")}
    if state.get("reader"):
        out["reader"] = state["reader"]
    if state.get("isolated_only"):
        out["isolated_only"] = True
    if support.get("strata_reads"):
        out["strata_reads"] = support["strata_reads"]
    return out
