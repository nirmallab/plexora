"""What each answer does to a unit -- the transition table, as code.

Every handler takes the engine, the packet the answer is for, and the
validated answer, moves the unit(s) on and returns a small outcome dict. The
rules the agent cannot talk its way past:

- a plausibility failure discards the direction (a look at bad staining says
  nothing about where the gate goes) and sends the marker to confirmation;
- a direction that contradicts the distribution -- "too low" for a gate already
  at the positive population's centre -- goes to manual review, not to the
  candidates;
- candidates move one way only; asking to go back is oscillation, and ends in
  manual review;
- a user's edit in the viewer always wins (checked before any write).
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import schemas
from plexora.plugins.gating.server.autogate.engine import ENGINE, TERMINAL, unit_key

#: Artifact flags that make a look unusable for placing a gate.
DOMINANT_ARTIFACTS = ("image_quality", "saturation", "autofluorescence", "tissue_fold")


def _units(engine, packet):
    out = []
    for ref in packet["units"]:
        unit = engine.record["units"].get(unit_key(ref["project"], ref["marker"]))
        if unit is not None:
            out.append(unit)
    return out


def _note(unit, answer):
    unit["artifact_flags"] = sorted(set(unit.get("artifact_flags") or [])
                                    | set(answer.artifact_flags or []))
    unit["last_answer"] = {k: v for k, v in answer.model_dump(mode="json").items()
                           if k not in ("notes",)}
    if answer.notes:
        unit.setdefault("notes", []).append(answer.notes[:300])


def _outcome(unit, **extra):
    return {"unit": unit_key(unit["project"], unit["marker"]), "state": unit["state"],
            "candidate": unit.get("candidate"), "final": unit.get("final"),
            "confidence": unit.get("confidence"), **extra}


def _contradicts(unit, direction):
    """True when the distribution says the direction cannot be right."""
    import math

    m = unit.get("metrics") or {}
    if m.get("mu_pos") is None or m.get("sd_bg") is None:
        return False
    g = float(unit["candidate"])
    if unit.get("fit_space") == "log1p":
        g = math.log1p(max(g, 0.0))
    if direction == "too_low":
        return g >= m["mu_pos"] - 0.5 * (m.get("sd_pos") or 0.0)
    if direction == "too_high":
        return g <= m["mu_bg"] + 1.0 * m["sd_bg"]
    return False


def _to_t4(engine, unit, direction, magnitude=None):
    step = "up" if direction == "too_low" else "down"
    history = unit.setdefault("directions", [])
    if history and history[-1] != step:
        engine.close(unit, "manual_review_recommended",
                     "the looks disagreed about which way the gate is wrong (oscillation)")
        return
    history.append(step)
    if int(engine.options.get("max_tier", 4)) < 4:
        _accept_or_low(engine, unit, "the gate looked off but refinement is not allowed "
                                     "in this run (max_tier < 4)")
        return
    unit["direction"] = step
    unit["magnitude"] = magnitude
    unit["span_from"] = unit["candidate"]
    unit["state"] = "awaiting_t4"


def _accept_or_low(engine, unit, reason):
    unit["reason"] = reason
    unit["state"] = "awaiting_regression"
    unit["ai_confidence"] = min(unit.get("ai_confidence") or 0.3, 0.3)
    engine.settle(unit)


def _references_ready(engine, unit):
    """Refresh the unit's usable references from what this run has gated."""
    from plexora.plugins.gating.server.autogate import context

    ds = engine.call.session.data(unit["project"])
    panel = context.for_project(ds)
    gated = {}
    gates = {}
    for other in engine.units_of(unit["project"]):
        if other is unit:
            continue
        conf = other.get("confidence")
        if other["state"] == "accepted_t1":
            conf = "high"
        if other["state"] in ("accepted", "accepted_t1") and conf in ("high", "moderate"):
            gated[other["marker"]] = conf
            gates[other["marker"]] = other.get("final") or other.get("candidate")
    refs = context.references_for(panel, unit["marker"], gated)
    unit["reference_gates"] = [{"marker": r["marker"], "relation": r["relation"],
                                "gate": gates[r["marker"]], "confidence": gated[r["marker"]]}
                               for r in refs]
    return unit["reference_gates"]


def _plausibility_failed(answer):
    p = answer.plausibility
    return (not p.positives_look_real or p.compartment == "mismatch"
            or any(a in DOMINANT_ARTIFACTS for a in answer.artifact_flags or []))


# -- T2 -----------------------------------------------------------------------------


def apply_t2(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    unit["ai_confidence"] = float(answer.confidence)
    unit["plausibility"] = answer.plausibility.model_dump(mode="json")
    unit["path"] = "t2"
    if answer.ask_user and engine.ask(unit, answer.ask_user):
        return _outcome(unit, asked=True)
    if _plausibility_failed(answer):
        if unit.get("qc_done"):
            engine.close(unit, "manual_review_recommended",
                         "the positives did not look like real staining, and the channel "
                         "had already passed its technical check")
        else:
            unit["qc_reason"] = ["positives did not look real" if not
                                 answer.plausibility.positives_look_real else
                                 "stain in the wrong compartment"] + list(answer.artifact_flags)
            unit["state"] = "qc_confirm"
        return _outcome(unit, direction_discarded=True)
    rows = {k: v for k, v in answer.rows.items() if v != "cannot_tell"}
    rows_ok = all(v == "plausible" for v in rows.values())
    direction = answer.direction
    if direction == "about_right" and answer.confidence >= ENGINE["t2_min_confidence"] \
            and rows_ok:
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit)
    if direction in ("too_low", "too_high"):
        if _contradicts(unit, direction):
            engine.close(unit, "manual_review_recommended",
                         f"the look said {direction.replace('_', ' ')}, but the gate already "
                         f"sits {'at the positive population' if direction == 'too_low' else 'at the background'}"
                         "; a person should judge this one")
            return _outcome(unit, contradiction=True)
        _to_t4(engine, unit, direction, answer.magnitude)
        return _outcome(unit)
    if direction == "not_binary":
        binary = (unit.get("context") or {}).get("binary", True)
        if not binary or not (unit.get("context") or {}).get("canonical"):
            engine.close(unit, "not_binary",
                         "looks continuous: no boundary between negative and positive cells; "
                         "a provisional GMM threshold is kept but not written",
                         confidence="manual_review")
            return _outcome(unit)
    # cannot_tell, low confidence, not_binary for a binary marker: references.
    refs = _references_ready(engine, unit)
    if refs and int(engine.options.get("max_tier", 4)) >= 3 and not unit.get("t3_done"):
        unit["state"] = "awaiting_t3"
        return _outcome(unit, references=[r["marker"] for r in refs])
    if direction == "about_right":
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, note="low confidence and no reference available")
    engine.close(unit, "manual_review_recommended",
                 "the look could not settle the gate and no reference marker is gated yet")
    return _outcome(unit)


# -- T3 -----------------------------------------------------------------------------


def apply_t3(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    unit["t3_done"] = True
    unit["ai_confidence"] = float(answer.confidence)
    unit["path"] = "t3"
    unit["references"] = [r["marker"] for r in unit.get("reference_gates") or []]
    if answer.ask_user and engine.ask(unit, answer.ask_user):
        return _outcome(unit, asked=True)
    consistent = answer.coexpression_consistent is not False and \
        answer.exclusion_consistent is not False
    if not consistent:
        unit.setdefault("bio_flags", []).append("reference_inconsistent")
    if _plausibility_failed(answer):
        if answer.background_pattern in ("segmentation_boundary", "nuclear_bleed", "edge"):
            unit.setdefault("bio_flags", []).append(answer.background_pattern)
            engine.close(unit, "manual_review_recommended",
                         f"positives look like {answer.background_pattern.replace('_', ' ')}, "
                         "not staining")
            return _outcome(unit)
        if not unit.get("qc_done"):
            unit["qc_reason"] = ["positives did not look real beside the reference"]
            unit["state"] = "qc_confirm"
            return _outcome(unit, direction_discarded=True)
        engine.close(unit, "manual_review_recommended", "implausible beside the reference")
        return _outcome(unit)
    if answer.direction == "about_right" and consistent:
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit)
    if answer.direction in ("too_low", "too_high"):
        if _contradicts(unit, answer.direction):
            engine.close(unit, "manual_review_recommended",
                         "the reference view contradicts the distribution")
            return _outcome(unit, contradiction=True)
        _to_t4(engine, unit, answer.direction, answer.magnitude)
        return _outcome(unit)
    if answer.direction == "not_binary" and not (unit.get("context") or {}).get("binary",
                                                                                True):
        engine.close(unit, "not_binary", "continuous beside its reference too",
                     confidence="manual_review")
        return _outcome(unit)
    # mixed / cannot_tell / inconsistent-but-about-right: keep the GMM gate,
    # low confidence, if it passes the numeric checks.
    _accept_or_low(engine, unit, "the reference view left the gate uncertain")
    return _outcome(unit)


# -- T4 -----------------------------------------------------------------------------


def apply_t4(engine, packet, answer):
    unit = _units(engine, packet)[0]
    choice = answer.chosen_candidate.strip()
    candidates = unit.get("candidates") or {}
    if choice not in candidates and choice not in ("keep", "none_separates"):
        from plexora.agent.errors import AgentError

        raise AgentError("invalid_input", f"{choice!r} is not one of this packet's candidates",
                         detail={"allowed": list(candidates) + ["keep", "none_separates"]})
    _note(unit, answer)
    unit["ai_confidence"] = float(answer.confidence)
    unit["path"] = "t4"
    unit["rounds"] = int(unit.get("rounds", 0)) + 1
    unit["method"] = "ai_refined"
    if answer.ask_user and engine.ask(unit, answer.ask_user):
        return _outcome(unit, asked=True)
    if choice in candidates:
        unit["candidate"] = float(candidates[choice])
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, chosen=choice)
    if choice == "keep":
        unit["method"] = "ai_accepted"
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, chosen="keep")
    if choice == "none_separates":
        if unit["rounds"] < ENGINE["t4_rounds"] and candidates:
            farthest = (max if unit.get("direction") == "up" else min)(candidates.values())
            unit["span_from"] = float(farthest)
            unit["state"] = "awaiting_t4"
            return _outcome(unit, respanned=True)
    engine.close(unit, "manual_review_recommended",
                 "no candidate threshold separated the cells")
    return _outcome(unit)


# -- confirmations -------------------------------------------------------------------


def apply_qc(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    unit["qc_done"] = True
    if answer.verdict == "technical_failure":
        engine.close(unit, "technically_failed",
                     "confirmed by eye: no usable staining (" +
                     ", ".join(unit.get("qc_reason") or unit.get("flags") or []) + ")",
                     confidence="failed_qc")
        return _outcome(unit)
    if answer.verdict == "real_signal":
        came_from_look = bool(unit.get("qc_reason")) and unit.get("path") in ("t2", "t3")
        if came_from_look:
            engine.close(unit, "manual_review_recommended",
                         "the channel carries signal, but the cells at the gate did not look "
                         "right; a person should set this gate")
            return _outcome(unit)
        unit["flags"] = [f for f in unit.get("flags") or [] if f not in schemas.HARD_FLAGS] + \
            [f"{f}_downgraded" for f in unit.get("flags") or [] if f in schemas.HARD_FLAGS]
        unit["state"] = "awaiting_t2"
        return _outcome(unit)
    engine.close(unit, "manual_review_recommended",
                 "the technical check could not tell whether the channel is usable")
    return _outcome(unit)


def apply_regression(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    if answer.verdict == "holds":
        unit["regression_confirmed"] = True
        engine.finalize(unit, method=unit.get("method") or "ai_accepted")
        return _outcome(unit)
    if answer.verdict in ("too_low", "too_high"):
        if int(unit.get("rounds", 0)) >= ENGINE["t4_rounds"]:
            engine.close(unit, "manual_review_recommended",
                         "the whole-image view disagreed with the refined gate")
            return _outcome(unit)
        _to_t4(engine, unit, answer.verdict)
        return _outcome(unit)
    if answer.verdict == "artifact":
        engine.close(unit, "manual_review_recommended",
                     "the whole-image view shows an artifact driving the positives")
        return _outcome(unit)
    unit["regression_confirmed"] = False
    unit["path"] = unit.get("path") or "t2"
    unit["ai_confidence"] = min(unit.get("ai_confidence") or 0.3, 0.3)
    engine.finalize(unit, method=unit.get("method") or "ai_accepted")
    return _outcome(unit)


def apply_strip(engine, packet, answer):
    units = _units(engine, packet)
    outcomes = []
    for unit in units:
        verdict = answer.verdicts.get(unit["marker"], "cannot_tell")
        unit["strip_verdict"] = verdict
        if unit["state"] in TERMINAL:
            outcomes.append(_outcome(unit))
            continue
        if verdict == "ok":
            unit["audited"] = True
            engine.finalize(unit, method="gmm")
        else:
            unit["audited"] = False
            unit["state"] = "awaiting_t2"
            unit["reason"] = f"the audit sheet row looked {verdict.replace('_', ' ')}"
        outcomes.append(_outcome(unit))
    return {"units": outcomes}


def apply_panel(engine, packet, answer):
    from plexora.plugins.gating.server.autogate import context

    project = engine.record["images"][0]
    ds = engine.call.session.data(project)
    panel = context.for_project(ds)
    applied, refused = [], []
    for entry in answer.entries:
        fields = entry.model_dump(exclude_none=True)
        marker = fields.pop("marker")
        try:
            context.apply_entry(panel, marker, fields, source="ai")
            applied.append(marker)
        except (KeyError, ValueError) as exc:
            refused.append({"marker": marker, "reason": str(exc)})
    context.save(panel)
    engine.record["panel_pending"] = False
    engine.record["order"] = [m for m in panel["order"] if m in engine.record["order"]] + \
        [m for m in engine.record["order"] if m not in panel["order"]]
    for unit in engine.record["units"].values():
        entry = panel["entries"].get(unit["marker"])
        if entry:
            unit["context"] = {k: entry.get(k) for k in ("canonical", "role", "compartment",
                                                         "lineage", "binary", "caveats",
                                                         "expected_fraction", "source",
                                                         "partners")}
    return {"applied": applied, "refused": refused, "order": engine.record["order"]}


def apply_transfer(engine, packet, answer):
    from plexora.plugins.gating.server.autogate import transfer

    return transfer.apply_transfer(engine, packet, answer)


APPLY = {"t2_confirm": apply_t2, "t3_biological": apply_t3, "t4_candidates": apply_t4,
         "qc_confirm": apply_qc, "regression_confirm": apply_regression,
         "t1_strip": apply_strip, "panel_context": apply_panel,
         "transfer_check": apply_transfer}
