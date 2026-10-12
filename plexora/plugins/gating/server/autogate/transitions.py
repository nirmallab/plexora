"""What each answer does to a unit -- the transition table, as code.

Every handler takes the engine, the packet the answer is for, and the
validated answer, moves the unit(s) on and returns a small outcome dict. The
rules the agent cannot talk its way past:

- a look never says which way the gate goes: its row words do
  (`verdicts.direction_from_rows`), the same words always the same way;
- a direction that contradicts the distribution -- "too low" for a gate already
  at the positive population's centre of a clean mixture, or at the edge of the
  admissible band of an overlapping one (`candidates.contradicts`) -- goes to
  manual review, not to the candidates;
- candidates move one way per look; a look at candidates whose first row
  says the opposite turns once (`lattice.place`'s `reverse`), and two looks
  pointing opposite ways otherwise is oscillation, ending in manual review;
- a gate is never ACCEPTED against a direction on record: `unit["direction"]`
  holds the way the last look said the gate is wrong until a candidate (or
  `keep`, or an about-right look) replaces it, and a unit closed meanwhile is
  written at the last gate and tagged `needs_review` (every gated marker ends
  with a value; a person checks the ones the evidence could not settle);
- a user's edit in the viewer always wins (checked before any write);
- an empty gate (at the column's maximum: no cell positive) is written only on
  an explicit statement -- a technical check's `technical_failure` or
  `no_positive_population`, or a look's `no_positives` confirmed on the whole
  image -- never because no candidate separated the cells, which is also what
  a continuous or badly segmented marker says.

Every loop is bounded, so a unit's looks are finite whatever the answers:

    look                bound
    T2                  1 (a second only after a technical check or a
                        conditional refit)
    technical check     1 per unit (`qc_done`)
    T3                  1 (`t3_done`)
    T4                  ENGINE["t4_rounds"] rounds, times (1 + extensions);
                        one turn back (`t4_reversed`)
    whole-image check   one per T4 round, plus one

A marker that reaches a limit while the evidence still says to go on is
handled by the session's limit policy (`Engine.limit_reached`): another
allowance (asked of the user, or granted), or manual review. Never accepted.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import schemas
from plexora.plugins.gating.server.autogate.engine import ENGINE, TERMINAL, unit_key



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


def _confidence(answer) -> float:
    """The number a confidence word stands for (`schemas.AI_CONFIDENCE`)."""
    return float(schemas.AI_CONFIDENCE[answer.confidence])


def _outcome(unit, **extra):
    return {"unit": unit_key(unit["project"], unit["marker"]), "state": unit["state"],
            "candidate": unit.get("candidate"), "final": unit.get("final"),
            "confidence": unit.get("confidence"), **extra}


def _contradicts(engine, unit, direction):
    """None, or why the distribution says the direction cannot be right."""
    from plexora.plugins.gating.server.autogate import candidates
    from plexora.plugins.gating.server.autogate import profile as profmod

    if direction not in ("too_low", "too_high") or unit.get("candidate") is None:
        return None
    if (unit.get("start") or {}).get("source") == "scoring":
        # The mixture's band says nothing about a start chosen because the
        # mixture missed; the lattice (scored candidates and steps around the
        # start) bounds the walk instead, and an empty direction ends in
        # review there (`packets.t4_candidates`).
        return None
    ds = engine.call.session.data(unit["project"])
    fit = profmod.fit_for(ds, unit["marker"])
    if fit is None:
        return None
    g = float(profmod.column(ds, unit["marker"]).to_fit(unit["candidate"]))
    d = (unit.get("metrics") or {}).get("d")
    if not candidates.contradicts(fit, g, "up" if direction == "too_low" else "down", d):
        return None
    if d is not None and d >= profmod.THRESHOLDS["bimodal_d"]:
        return ("the gate already sits " + ("at the positive population"
                                            if direction == "too_low" else "at the background"))
    return ("the gate is already at the " + ("upper" if direction == "too_low" else "lower")
            + " edge of the admissible band")


def _to_t4(engine, unit, direction, magnitude=None):
    step = "up" if direction == "too_low" else "down"
    history = unit.setdefault("directions", [])
    if history and history[-1] != step:
        engine.close(unit, "manual_review_recommended",
                     "the looks disagreed about which way the gate is wrong (oscillation)")
        return
    history.append(step)
    unit["direction"] = step
    if int(engine.options["max_tier"]) < 4:
        engine.close(unit, "insufficient_information",
                     f"the look said the gate is too {'low' if step == 'up' else 'high'}, but "
                     "refinement is not allowed in this run (max_tier < 4); written for manual "
                     "review",
                     proposed=unit["candidate"])
        return
    unit["magnitude"] = magnitude
    unit["span_from"] = unit["candidate"]
    unit["state"] = "awaiting_t4"


def _clear_direction(unit):
    """A candidate, `keep` or an about-right look replaces the direction on
    record (see the module docstring)."""
    unit.pop("direction", None)


def _no_positives(engine, unit, where):
    """A look said no cell is positive: confirmed on the whole image first
    (a collage shows near-gate cells, not the tissue), unless it was."""
    _clear_direction(unit)
    if unit.get("qc_done"):
        engine.close(unit, "no_positive_population",
                     f"{where} said no cell is positive, after the whole-image check; the gate "
                     "is put at the maximum")
        return _outcome(unit)
    unit["qc_reason"] = [f"{where} said no cell is positive"]
    unit["state"] = "qc_confirm"
    return _outcome(unit, direction_discarded=True)


def _within(engine, unit, packet, answer):
    """A look's `within`: the stain is real only inside a subset partner's
    positives. The gate is refitted among them (`bivariate.within_partner`),
    recorded as the unit's condition, and shown again in a conditional look;
    the same answer to that look accepts it. Refused (`invalid_input`) unless
    the partner is a `subset` partner of the packet gated at moderate or
    better -- the partner's gate is what the condition stands on."""
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import packets, tableops

    partner = (answer.within or "").strip()
    condition = unit.get("condition")
    if condition:
        if partner and partner != condition["within"]:
            raise AgentError("invalid_input", f"this gate is already conditional on "
                             f"{condition['within']}+; judge its rows",
                             detail={"condition": condition["within"]})
        _clear_direction(unit)
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, condition=condition["within"])
    evidence = packet.get("evidence") or {}
    allowed = evidence.get("within_allowed") or packets.within_eligible(evidence.get("partners"))
    if partner not in allowed:
        raise AgentError("invalid_input", "`within` must name a subset partner of this "
                         "packet gated at moderate confidence or better (`allowed.within`)",
                         detail={"allowed": allowed, "given": partner or None})
    ref = next((r for r in engine.references_ready(unit) if r["marker"] == partner), None)
    if ref is None:
        raise AgentError("invalid_input", f"{partner} is no longer a gated partner",
                         detail={"allowed": allowed})
    ds = engine.call.session.data(unit["project"])
    result = tableops.local_or_node(ds, "gating.autogate.within", {
        "marker": unit["marker"], "partner": partner, "partner_gate": ref["gate"],
        "current": unit.get("candidate"), "seed": int(engine.options["seed"])})
    if not result.get("ok"):
        engine.close(unit, "manual_review_recommended",
                     f"the look said {unit['marker']} is real only within {partner}+ cells, "
                     f"but no conditional gate could be fitted: {result.get('reason')}",
                     proposed=unit.get("candidate"))
        return _outcome(unit, condition=None)
    low = engine.clamp_to_judged(unit, ds, float(result["low"]), why="within")
    unit["condition"] = {"within": partner, "partner_gate": float(ref["gate"]),
                         "relation": ref["relation"], "method": result["method"],
                         "plain_low": unit.get("candidate"),
                         "n_partner_positive": result["n_partner_positive"],
                         "n_positive_within": result["n_positive_within"],
                         "n_positive_outside": result["n_positive_outside"],
                         "separation_d": result.get("separation_d"),
                         **({"refit_low": float(result["low"])}
                            if low != float(result["low"]) else {})}
    unit["candidate"] = float(low)
    unit["method"] = "ai_conditional"
    _clear_direction(unit)
    unit.pop("directions", None)
    unit["state"] = "awaiting_t2"
    return _outcome(unit, condition=partner)


def _honour_request(engine, unit, answer, direction):
    """A look that asked for a reference channel (or a bivariate view) gets
    one when a reference is gated -- but only when the answer would have gone
    to a reference look anyway (its rows said `cannot_tell`, or it was
    `unsure`): a request adds evidence, it never skips a step of the path a
    decisive answer takes. Returns the outcome, or None."""
    request = answer.request
    if request is None or request.kind not in ("reference_channel", "bivariate"):
        return None
    undecided = direction == "cannot_tell" or \
        _confidence(answer) < ENGINE["t2_min_confidence"]
    if not undecided:
        unit.setdefault("requests", []).append({"kind": request.kind, "marker": request.marker,
                                                "reason": request.reason, "served": "plot"})
        return None
    record = {"kind": request.kind, "marker": request.marker, "reason": request.reason}
    if int(engine.options["max_tier"]) < 3 or unit.get("t3_done"):
        unit.setdefault("requests", []).append({**record, "served": False})
        return None
    refs = engine.references_ready(unit)
    if request.marker:
        refs.sort(key=lambda r: r["marker"] != request.marker)
    if not refs:
        unit.setdefault("requests", []).append({**record, "served": False})
        return None
    unit.setdefault("requests", []).append({**record, "served": True})
    unit["state"] = "awaiting_t3"
    return _outcome(unit, request_honoured=True, references=[r["marker"] for r in refs])


# -- T2 / T3 ------------------------------------------------------------------------


def _judge_rows(unit, packet, answer):
    """What a confident first look (or look beside a reference) judged, by
    value: each row's word over the range of values its cells showed
    (`packets._row_spans`), in `unit["judged_rows"]` -- no refit code makes
    afterwards crosses it (`Engine.clamp_to_judged`, beside the candidate
    intervals' `judged`). The cell model's correction keeps to the candidate
    intervals alone (`hybrid.judged_limits`): its per-cell calls are evidence
    of their own, and on the PCA benchmark it moved FOXP3 toward the user's
    gate across a first look's row. And whether a look
    has ever seen the marker's own stain with confidence (`own_seen`: a
    population no look saw is what `replan` may call absent)."""
    if _confidence(answer) < ENGINE["t2_min_confidence"]:
        return
    rows = dict(answer.rows or {})
    if "own_stain" in rows.values():
        unit["own_seen"] = True
    spans = packet.get("_row_spans") or {}
    unit.setdefault("judged_rows", []).extend(
        [spans[row][0], spans[row][1], word] for row, word in rows.items()
        if row in spans and word in ("own_stain", "unstained", "neighbour_only"))


def _read_look(unit, answer, direction, why):
    """What a look's words say beside the direction, kept on the unit: the
    rows, the pattern, and two flags -- `pattern_mismatch` (a stain pattern
    the compartment does not have: caps confidence, `engine.confidence_for`)
    and `neighbour_spill` (a row of cells lit by a neighbour: recorded)."""
    unit["look"] = {"rows": dict(answer.rows), "pattern": answer.pattern,
                    "direction": direction, "why": why}
    compartment = (unit.get("context") or {}).get("compartment")
    fits = (schemas.COMPARTMENT_POLICY.get(compartment) or {}).get("patterns")
    flags = set(unit.get("look_flags") or [])
    if fits and answer.pattern not in fits + ("none", "cannot_tell"):
        flags.add("pattern_mismatch")
    if "neighbour_only" in answer.rows.values():
        flags.add("neighbour_spill")
    unit["look_flags"] = sorted(flags)


def _look(engine, packet, answer, *, where):
    """A first look (`t2_confirm`) or a look beside a reference
    (`t3_biological`): the rows decide (`verdicts.direction_from_rows`).

    up / down -> the candidates that way (unless the distribution says the
    gate cannot be wrong that way); about_right at a confident look -> the
    whole-image checks; no_positives -> confirmed on the whole image; an
    unsure or undecided look -> a reference look when one is gated and none
    was had, else written for manual review (an uncertain look never
    accepts a gate)."""
    from plexora.plugins.gating.server.autogate import verdicts as words

    unit = _units(engine, packet)[0]
    reference = answer.kind == "t3_biological"
    _note(unit, answer)
    _judge_rows(unit, packet, answer)
    unit["ai_confidence"] = _confidence(answer)
    unit["path"] = "t3" if reference else "t2"
    if reference:
        unit["t3_done"] = True
        unit["references"] = [r["marker"] for r in unit.get("reference_gates") or []]
        if answer.request is not None:
            # Recorded; a T3 is the one reference look a unit gets.
            unit.setdefault("requests", []).append({
                "kind": answer.request.kind, "marker": answer.request.marker,
                "reason": answer.request.reason, "served": False})
    if answer.ask_user and engine.ask(unit, answer.ask_user):
        return _outcome(unit, asked=True)
    if answer.within:
        return _within(engine, unit, packet, answer)
    direction, why = words.direction_from_rows(answer.rows, answer.pattern)
    _read_look(unit, answer, direction, why)
    if direction == "no_positives":
        return _no_positives(engine, unit, where)
    if direction in ("up", "down"):
        legacy = words.legacy_direction(direction)
        why_not = _contradicts(engine, unit, legacy)
        if why_not:
            engine.close(unit, "manual_review_recommended",
                         f"{where}'s rows said the gate should go {direction} ({why}), but "
                         f"{why_not}; a person should judge this one")
            return _outcome(unit, contradiction=True)
        if answer.request is not None and not reference:
            # Beside a decisive direction a request only chooses the plot.
            unit.setdefault("requests", []).append({
                "kind": answer.request.kind, "marker": answer.request.marker,
                "reason": answer.request.reason, "served": "plot"})
        _to_t4(engine, unit, legacy)
        return _outcome(unit, direction=direction)
    confident = _confidence(answer) >= ENGINE["t2_min_confidence"]
    if direction == "about_right" and confident:
        _clear_direction(unit)
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, direction=direction)
    if not reference:
        honoured = _honour_request(engine, unit, answer, direction)
        if honoured:
            return honoured
        refs = engine.references_ready(unit)
        if refs and int(engine.options["max_tier"]) >= 3 and not unit.get("t3_done"):
            unit["state"] = "awaiting_t3"
            return _outcome(unit, references=[r["marker"] for r in refs])
    engine.close(unit, "manual_review_recommended",
                 f"{where} could not settle the gate ({why}) and no usable reference was "
                 "left to ask; the current gate is written for manual review",
                 proposed=unit.get("candidate"))
    return _outcome(unit, direction=direction)


def apply_t2(engine, packet, answer):
    return _look(engine, packet, answer, where="the look")


def apply_t3(engine, packet, answer):
    return _look(engine, packet, answer, where="the look beside the reference")


# -- T4 -----------------------------------------------------------------------------


def apply_t4(engine, packet, answer):
    """The rows place the gate (`lattice.place`): it moves past every row
    whose cells the words say it should and stops at the first that does
    not -- always on a lattice point. When it does not move:

    - `reverse` (the first row says the opposite: going up, the cells just
      above the gate are positive): the look that sent it this way may have
      been wrong, so it looks the other way, once (`t4_reversed`);
    - `mixed` (the first row holds both kinds): the gate is at the boundary
      and stays, at no more than moderate confidence (`t4_stopped_mixed`) --
      with overlapping populations "mixed" is the honest answer, not a
      failure;
    - `keep`: the gate stays."""
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import lattice as latmod

    unit = _units(engine, packet)[0]
    candidates = unit.get("candidates") or {}
    rows = [f"i{index + 1}" for index in range(len(candidates))]
    missing = [r for r in rows if r not in answer.intervals]
    if missing:
        raise AgentError("invalid_input", f"judge every interval row; missing {missing}",
                         detail={"rows": rows})
    _note(unit, answer)
    unit["ai_confidence"] = _confidence(answer)
    unit["path"] = "t4"
    unit["rounds"] = int(unit.get("rounds", 0)) + 1
    # What the rows judged, by value: a later correction by the cell model
    # never crosses it (`hybrid.judged_limits`).
    spans = {row["row"]: (row["from"], row["to"])
             for row in (packet.get("evidence") or {}).get("intervals") or ()
             if row.get("from") is not None and row.get("to") is not None}
    unit.setdefault("judged", []).extend(
        [min(spans[row]), max(spans[row]), word] for row, word in answer.intervals.items()
        if row in spans)
    conditional = bool(unit.get("condition"))
    unit["method"] = "ai_conditional" if conditional else "ai_refined"
    if answer.ask_user and engine.ask(unit, answer.ask_user):
        return _outcome(unit, asked=True)
    if "neighbour_only" in answer.intervals.values():
        unit["look_flags"] = sorted(set(unit.get("look_flags") or []) | {"neighbour_spill"})
    if "own_stain" in answer.intervals.values() \
            and unit["ai_confidence"] >= ENGINE["t2_min_confidence"]:
        unit["own_seen"] = True
    ids = list(candidates)
    chain = [{"id": cid, "low": candidates[cid]} for cid in ids]
    direction = unit.get("direction")
    placed, passed, why = latmod.place(chain, answer.intervals, direction)
    if placed is not None:
        unit["candidate"] = float(placed["low"])
        unit["chosen_step"] = (unit.get("candidate_steps") or {}).get(placed["id"])
        if passed == len(chain) and latmod.chain(engine.lattice_for(unit), placed["low"],
                                                 direction):
            # Every row said move and the look ran out of rows -- it stopped at
            # a partner's anchor or at MAX_CHAIN, not at a row that said stop.
            # Where the gate belongs is not known yet: another look, from here
            # (the round limit applies: `Engine.rounds_left`).
            unit.setdefault("t4_continued", []).append(unit["chosen_step"] or placed["id"])
            unit["span_from"] = unit["candidate"]
            unit["state"] = "awaiting_t4"
            return _outcome(unit, chosen=placed["id"], point=unit["chosen_step"],
                            rows_passed=passed, continues=True)
        _clear_direction(unit)
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return _outcome(unit, chosen=placed["id"], point=unit["chosen_step"],
                        rows_passed=passed)
    if why == "reverse" and not unit.get("t4_reversed") and not unit.get("t4_continued") \
            and direction in ("up", "down") and engine.rounds_left(unit):
        # Code's turn, not a look's: the oscillation guard does not count it.
        turned = "down" if direction == "up" else "up"
        unit["t4_reversed"] = {"from": direction, "at": float(unit["candidate"])}
        unit.setdefault("directions", []).append(turned)
        unit["direction"] = turned
        unit["span_from"] = unit["candidate"]
        unit["state"] = "awaiting_t4"
        return _outcome(unit, chosen="reverse", direction=turned)
    if why == "mixed":
        unit["t4_stopped_mixed"] = True
    unit["method"] = "ai_conditional" if conditional else "ai_accepted"
    # Remembered so a whole-image "still too low" does not re-serve the very
    # rows this look just declined (`apply_regression`).
    unit["t4_kept"] = {"direction": direction, "at": float(unit["candidate"])}
    _clear_direction(unit)
    unit["state"] = "awaiting_regression"
    engine.settle(unit)
    return _outcome(unit, chosen="keep", why=why)


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
    if answer.verdict == "no_positive_population":
        engine.close(unit, "no_positive_population",
                     "confirmed on the whole image: the stain worked and no cell is positive ("
                     + ", ".join(unit.get("qc_reason") or unit.get("flags") or ["no reason"])
                     + "); the gate is put at the maximum")
        return _outcome(unit)
    if answer.verdict == "real_signal":
        unit["flags"] = [f for f in unit.get("flags") or [] if f not in schemas.HARD_FLAGS] + \
            [f"{f}_downgraded" for f in unit.get("flags") or [] if f in schemas.HARD_FLAGS]
        came_from_look = bool(unit.get("qc_reason")) and unit.get("path") in ("t2", "t3")
        if came_from_look and int(engine.options["max_tier"]) >= 3 \
                and not unit.get("t3_done") and engine.references_ready(unit):
            # The channel is real; beside a reference is the look most
            # likely to settle what the first look could not.
            unit["state"] = "awaiting_t3"
            return _outcome(unit, references=[r["marker"] for r in unit["reference_gates"]])
        # A second look; a second plausibility failure closes the unit
        # (`qc_done`).
        unit["state"] = "awaiting_t2"
        return _outcome(unit)
    if unit.get("qc_verdict") == "usable" and not unit.get("t2_after_qc"):
        # QC reviewed this channel and found a working stain: an unsure
        # technical look does not close it; the marker gets its ordinary look.
        unit["t2_after_qc"] = True
        unit["state"] = "awaiting_t2"
        return _outcome(unit, qc_verdict="usable")
    engine.close(unit, "manual_review_recommended",
                 "the technical check could not tell whether the channel is usable")
    return _outcome(unit)


def apply_regression(engine, packet, answer):
    from plexora.agent.errors import AgentError

    unit = _units(engine, packet)[0]
    if answer.verdict in ("too_low", "too_high") \
            and (unit.get("regression") or {}).get("spatial_only"):
        raise AgentError("invalid_input",
                         "only the spatial checks failed: answer holds, artifact or "
                         "cannot_tell -- the gate is not moved to chase where positives are",
                         detail={"allowed": ["holds", "artifact", "cannot_tell"]})
    _note(unit, answer)
    if answer.verdict == "holds":
        _clear_direction(unit)
        unit["regression_confirmed"] = True
        engine.finalize(unit, method=unit.get("method") or "ai_accepted")
        return _outcome(unit)
    if answer.verdict in ("too_low", "too_high"):
        step = "up" if answer.verdict == "too_low" else "down"
        kept = unit.get("t4_kept") or {}
        if kept.get("direction") == step and kept.get("at") == float(unit["candidate"]):
            # The last look at candidates from this very gate, this way, said
            # its rows were mixed: the lattice has nothing else to show, so
            # the same packet would come back (live run lsp11385, LAG3).
            engine.close(unit, "manual_review_recommended",
                         f"the whole-image view says the gate is {answer.verdict.replace('_', ' ')}"
                         ", but the candidates that way were just judged mixed; a person "
                         "should place it", proposed=unit.get("candidate"))
            return _outcome(unit)
        # Back to candidates; past the round limit the session's limit
        # policy decides (`Engine.limit_reached`), never an acceptance.
        _to_t4(engine, unit, answer.verdict)
        return _outcome(unit)
    if answer.verdict == "artifact":
        unit["artifact_verdict"] = True
        engine.close(unit, "manual_review_recommended",
                     "the whole-image view shows an artifact driving the positives")
        return _outcome(unit)
    # cannot_tell: a gate that failed a whole-image check and that the image
    # does not vouch for is not accepted.
    unit["regression_confirmed"] = False
    engine.close(unit, "manual_review_recommended",
                 "the gate failed a whole-image check and the whole-image view could not "
                 "settle it; it is written for manual review", proposed=unit.get("candidate"))
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


def apply_replan(engine, packet, answer):
    """The planning task's verdicts on what failed (`replan.apply`)."""
    from plexora.plugins.gating.server.autogate import replan

    return replan.apply(engine, packet, answer)


def apply_expression(engine, packet, answer):
    """Set the matrix (and log1p) the session's images are read from, as
    `set_expression_source` calls, receipted into the session (a rollback
    restores them last)."""
    from plexora.agent import registry
    from plexora.agent.errors import AgentError

    allowed = packet.get("allowed") or []
    if answer.features_layer not in allowed:
        raise AgentError("invalid_input", f"{answer.features_layer!r} is not one of this "
                         "packet's matrices", detail={"allowed": allowed})
    kinds = {o["value"]: o.get("kind")
             for o in (packet.get("evidence") or {}).get("options") or []}
    if answer.features_log and kinds.get(answer.features_layer) == "log_like":
        raise AgentError("invalid_input", f"{answer.features_layer} already looks "
                         "log-transformed; log1p on it would transform it twice",
                         detail={"hint": "answer with features_log: false"})
    record = engine.record
    pending = (record.get("expression") or {}).get("projects") or record["images"]
    receipts, applied = [], []
    call = engine.call
    for index, project in enumerate(pending):
        result = registry.invoke(call.session, "set_expression_source", {
            "project": project, "features_layer": answer.features_layer,
            "features_log": bool(answer.features_log), "confirm": True},
            policy=call.policy, audit=call.audit, link=call.link, notify=call.notify,
            operation_id=f"{record['operation_id']}.expr{index + 1}")
        if not result["ok"]:
            error = result["error"]
            raise AgentError(error["code"], f"{project}: {error['message']}",
                             detail=error.get("detail"))
        receipt = (result["result"] or {}).get("receipt") or {}
        if receipt.get("operation_id") and receipt.get("changed"):
            receipts.append(receipt["operation_id"])
        applied.append(project)
    # Earliest in the list, so a rollback (newest first) restores them last.
    record["receipts"] = receipts + list(record.get("receipts") or [])
    choice = {"features_layer": answer.features_layer,
              "features_log": bool(answer.features_log)}
    record["expression"] = {**(record.get("expression") or {}), "status": "applied",
                            "choice": choice, "why": "answered by the agent",
                            "applied": applied}
    return {"applied": applied, **choice, "receipts": receipts}


def apply_pixel(engine, packet, answer):
    """The pixel size the session draws its pictures with, for the images the
    packet names (`applies_to`: every image still waiting, one scanner being
    the usual case). A `user_stated` value is also written to each project
    (`set_pixel_size`, receipted into the session; a rollback restores it
    last); a confirmed or adjusted one stays with the session."""
    from plexora.agent import registry
    from plexora.agent.errors import AgentError

    record = engine.record
    pixel = record.setdefault("pixel", {})
    projects = pixel.setdefault("projects", {})
    evidence = packet.get("evidence") or {}
    targets = [p for p in evidence.get("applies_to") or [evidence.get("project")]
               if p in projects]
    estimate = (evidence.get("estimate") or {}).get("microns_per_pixel")
    value = answer.microns_per_pixel
    if answer.basis == "estimate_confirmed" and value is None:
        value = estimate
    if value is None and answer.ask_user is None:
        raise AgentError("invalid_input", f"{answer.basis} needs `microns_per_pixel`"
                         + ("" if estimate else " (there is no estimate to confirm)"),
                         detail={"estimate": estimate})
    receipts = []
    if value is None:
        # Put to the user; the estimate draws the pictures meanwhile ("≈").
        question = {"unit": None, "question": answer.ask_user.question,
                    "options": answer.ask_user.options, "why": answer.ask_user.why,
                    "about": "pixel_size", "asked_at": _now()}
        record.setdefault("questions", []).append(question)
        for name in targets:
            projects[name].update(status="asked", value=estimate, basis="estimate")
    else:
        call = engine.call
        for index, name in enumerate(targets):
            projects[name].update(status="applied", value=float(value), basis=answer.basis)
            if answer.basis != "user_stated":
                continue
            result = registry.invoke(call.session, "set_pixel_size", {
                "project": name, "microns_per_pixel": float(value)},
                policy=call.policy, audit=call.audit, link=call.link, notify=call.notify,
                operation_id=f"{record['operation_id']}.px{index + 1}")
            if not result["ok"]:
                error = result["error"]
                raise AgentError(error["code"], f"{name}: {error['message']}",
                                 detail=error.get("detail"))
            receipt = (result["result"] or {}).get("receipt") or {}
            if receipt.get("operation_id") and receipt.get("changed"):
                receipts.append(receipt["operation_id"])
        # Earliest in the list, so a rollback (newest first) restores them last.
        record["receipts"] = receipts + list(record.get("receipts") or [])
    pixel["status"] = ("pending" if any(e.get("status") == "pending"
                                        for e in projects.values()) else "applied")
    return {"applied": targets, "microns_per_pixel": value, "basis": answer.basis,
            "written": answer.basis == "user_stated" and value is not None,
            "receipts": receipts}


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def apply_transfer(engine, packet, answer):
    from plexora.plugins.gating.server.autogate import transfer

    return transfer.apply_transfer(engine, packet, answer)




def apply_correction(engine, packet, answer):
    """The cells a corrected gate would change, one word each: the move is
    taken when enough of the answered cells agree with it
    (`hybrid.apply_answer`), else the approved gate stays (and the doubt is
    recorded); either way the gate goes on to the whole-image checks."""
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import hybrid

    unit = _units(engine, packet)[0]
    shown = (packet.get("evidence") or {}).get("cells") or []
    missing = [key for key in shown if key not in answer.cells]
    if missing:
        raise AgentError("invalid_input", f"judge every numbered cell; missing {missing}",
                         detail={"cells": shown})
    _note(unit, answer)
    words = {key: word for key, word in answer.cells.items() if key in shown}
    record = hybrid.apply_answer(engine, unit, words)
    unit["state"] = "awaiting_regression"
    engine.settle(unit)
    return _outcome(unit, correction=record["status"], share=record.get("share"))


APPLY = {"t2_confirm": apply_t2, "t3_biological": apply_t3, "t4_candidates": apply_t4,
         "qc_confirm": apply_qc, "regression_confirm": apply_regression,
         "t1_strip": apply_strip, "panel_context": apply_panel, "replan": apply_replan,
         "transfer_check": apply_transfer, "expression_setup": apply_expression,
         "pixel_setup": apply_pixel, "correction_check": apply_correction}
