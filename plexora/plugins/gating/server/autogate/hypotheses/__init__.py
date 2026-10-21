"""Candidate gates as competing hypotheses, chosen by blind cell panels.

`SessionOptions.engine = "candidates"`. A marker that needs a look (not a
clean split the audit sheet settles, not a conditional gate) is decided
here instead of by the classic below/at/above rows and the candidates
beyond them:

1. The unit's frozen lattice (`lattice.build`) gives the candidates
   (`candidates.build`): every estimator, the scored start, partner points,
   and the steps around them. The gate written is always one of them.
2. Each look is a `cell_panel` packet (`packet`): numbered cells from the
   disputed intervals, a few negative sentinels, shuffled, no value, call or
   gate anywhere. The reader labels each cell (`answers.CellPanelAnswer`).
3. Code scores the candidates by the cells each would get wrong
   (`evidence.support`) and decides (`controller.decide`): resolved when the
   leader beats every rival outside the tolerance, else another novel look
   (`controller.next_action`) or review with a stated reason.
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

#: Bumped whenever the same answers would choose a different gate.
VERSION = "1"

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
    # Most labels uncertain or artifact (once this many cells were read):
    # the marker is unreadable this way.
    "min_labelled": 10,
    "max_other_share": 0.5,
}

#: Terminal status -> the unit's closed state (a review state writes the
#: gate at the leader, tagged `needs_review`).
CLOSE_STATE = {status: "manual_review_recommended" for status in controller.STATUSES
               if status != "resolved"}
CLOSE_STATE["no_identifiable_boundary"] = "not_binary"


def active(engine, unit) -> bool:
    """Whether this unit's looks are cell panels."""
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
    _ds, col = _column(engine, unit)
    begin = start_low(unit)
    cands = candmod.build(lat, begin, col.to_fit, PARAMS["tolerance"])
    points = [{"low": float(p["low"]), "n_positive": int(p["n_positive"]), "id": p["id"],
               "sources": list(p["sources"])}
              for p in sorted(lat.get("points") or [], key=lambda p: p["low"])]
    if cands["window"] is None or len(cands["bounds"]) < 2:
        unit["hypotheses"] = {"version": VERSION, "status": "no_candidates",
                              "reason": "the lattice has fewer than two candidate gates"}
        return unit["hypotheses"]
    unit["hypotheses"] = {
        "version": VERSION, "points": points, "window": list(cands["window"]),
        "prior": int(cands["prior"]), "start": begin,
        "clusters": cands["clusters"], "spread": candmod.spread(cands, col.to_fit),
        "lattice": lat.get("fingerprint"), "observations": [], "status": None,
        "params": dict(PARAMS)}
    return unit["hypotheses"]


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
    plan = controller.next_action(state, col.to_fit, state["params"])
    if plan is not None and plan["kind"] == "extend":
        new = controller.extend(state, plan["side"])
        bounds, n_cells = controller.window(state)
        k = new["interval"]
        plan = {"kind": "extend", "side": plan["side"],
                "intervals": [{"interval": k, "from": bounds[k], "to": bounds[k + 1],
                               "n": min(int(state["params"]["panel_cells"]), n_cells[k])}]}
        if not plan["intervals"][0]["n"]:
            plan = controller.next_action(state, col.to_fit, state["params"])
    if plan is None:
        finish(engine, unit, "unresolved_evidence")
        return None
    bounds, _ = controller.window(state)
    params = state["params"]
    sampled = tableops.local_or_node(ds, "gating.autogate.panel_cells", {
        "marker": unit["marker"],
        "intervals": [{"from": iv["from"], "to": iv["to"], "n": iv["n"]}
                      for iv in plan["intervals"]],
        "exclude": _shown_ids(state), "n_sentinels": int(params["sentinels"]),
        "sentinel_below": bounds[0], "seed": _seed(engine, state)})
    for cell in sampled["cells"]:
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
    rows = panelmod.rows_of(arranged, per_row)
    main = collage.render_collage(
        engine.call.session, ds, layout=layout, rows=rows, marker=channel, gate=None,
        high=unit.get("high"), fmt=packets._fmt(engine),
        pixel=engine.pixel_for(ds.name), partners=partners or None,
        title=f"{unit['marker']} · numbered cells in random order (each cell: "
              f"{packets._panel_words(layout, partners) or 'morphology | context'})")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    numbers = [c["number"] for c in arranged]
    multiview = ({"partner_panels": {"expected": partners["markers"].get("co"),
                                     "not_expected": partners["markers"].get("anti")}}
                 if partners else {})
    guide = ["panel", "collage_panel_m" if partners else "collage_panel"]
    pkt = {
        "question": (f"{unit['marker']} ({ds.name}): for EVERY numbered cell, does it carry "
                     f"its OWN {stain} stain, around the nucleus the ticks mark? One word per "
                     "cell."),
        "evidence": {"marker": unit["marker"], "project": ds.name,
                     "cells": len(numbers), **multiview,
                     "context": {"compartment": compartment},
                     "look": len(state["observations"]) + 1,
                     **packets._reading(engine, guide)},
        "allowed": {"cells": numbers, "labels": list(evidence.LABELS)},
        "_image_meta": [],
        "_panel": {"plan": plan, "cells": [
            {"number": c["number"], "cell_id": int(c["cell_id"]), "value": float(c["value"]),
             **({"sentinel": True} if c.get("sentinel") else
                {"interval": c.get("interval")})} for c in arranged]},
    }
    images = packets._images(pkt, ((main, "panel_collage", "numbered cells, random order"),))
    unit["last_manifest"] = main["manifest"]
    return pkt, images


def _seed(engine, state) -> int:
    return int(engine.options["seed"]) * 1009 + len(state["observations"]) * 7919 + 17


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
    sentinels = [c for c in cells if c.get("sentinel")]
    misses = sum(1 for c in sentinels if c["label"] == evidence.OWN)
    valid = misses < int(state["params"]["sentinel_misses"])
    state["observations"].append({
        "packet_id": packet.get("packet_id"), "plan": (packet.get("_panel") or {}).get("plan"),
        "cells": cells, "valid": valid,
        "sentinels": {"n": len(sentinels), "read_stained": misses}})
    return step(engine, unit)


def step(engine, unit) -> dict:
    """Decide after an observation: keep looking, or finish."""
    from plexora.plugins.gating.server.autogate import transitions

    state = unit["hypotheses"]
    _ds, col = _column(engine, unit)
    status, s = controller.decide(state, col.to_fit, state["params"],
                                  continuous=bool(unit.get("continuous")))
    if s is not None:
        state["leader"] = s["low"]
        state["support"] = {k: s[k] for k in ("loss", "fractions", "rivals", "labelled",
                                              "other_share")}
    if status is None:
        return transitions._outcome(unit, look=len(state["observations"]),
                                    leader=state.get("leader"))
    finish(engine, unit, status)
    return transitions._outcome(unit, status=status, leader=state.get("leader"))


def finish(engine, unit, status):
    """Write the unit's outcome: a resolved leader goes through the numeric
    checks (`settle`); anything else closes for review at the leader."""
    state = unit["hypotheses"]
    state["status"] = status
    leader = state.get("leader")
    if status == "resolved":
        state["reason"] = "the leading candidate beat every rival outside the tolerance"
        unit["candidate"] = float(leader)
        unit["path"] = "panel"
        unit["method"] = "ai_panel"
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return
    state["reason"] = controller.REASONS.get(status, status)
    proposed = leader if leader is not None and status != "readers_unreliable" \
        else state.get("start")
    engine.close(unit, CLOSE_STATE[status], f"cell panels: {state['reason']}",
                 proposed=proposed)


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
    (`engine.confidence_for`), tagged uncalibrated until it is fitted on
    the dev bench: high when every rival outside the tolerance was beaten
    by a clear margin and the candidates' estimators agree; moderate when
    resolved; low otherwise."""
    state = unit.get("hypotheses") or {}
    if state.get("status") != "resolved":
        return "low"
    rivals = (state.get("support") or {}).get("rivals") or []
    margins = [min(abs(r["ci"][0] - 0.5), abs(r["ci"][1] - 0.5)) for r in rivals]
    clear = not margins or min(margins) >= 0.1
    agreed = (state.get("spread") or 0.0) <= 2 * float(state["params"]["tolerance"])
    return "high" if clear and agreed else "moderate"


def provenance(unit) -> dict | None:
    """What the gate's provenance keeps of the hypotheses."""
    state = unit.get("hypotheses")
    if not state:
        return None
    return {"version": state.get("version"), "status": state.get("status"),
            "reason": state.get("reason"), "leader": state.get("leader"),
            "start": state.get("start"), "clusters": state.get("clusters"),
            "observations": [{"packet_id": o.get("packet_id"), "valid": o.get("valid"),
                              "kind": (o.get("plan") or {}).get("kind"),
                              "n_cells": len(o.get("cells") or ())}
                             for o in state.get("observations") or ()],
            "confidence": "uncalibrated"}
