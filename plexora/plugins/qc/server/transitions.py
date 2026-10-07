"""What each QC answer does: one handler per packet kind, all deterministic.

The rules (the full table is in the engine's docstring):

- **audit**: a `clean` row dismisses its candidates, unless one is strong
  (the force-confirm score) and of a kind an audit tile cannot show
  (`overview_blind`); `suspicious` keeps the candidates it names; `uncertain`
  keeps the candidates it names, or -- naming none -- every one on its row. A
  candidate drawn on several rows is kept when any row names it (or is
  uncertain without naming others), and settled once all its rows are in.
  Staining is settled on the row, never outlined: `suspicious` with
  `elsewhere` (or naming nothing) flags the channel with what the agent saw
  (`audit_note`), `uncertain` with nothing to look at leaves it for manual
  review; a failed channel listed on the row (`channel_level`) is decided by
  the row itself (`_decide_channel_level`) -- no confirm, no region.
- **confirm** (per candidate; a first-look packet may carry several, answered
  by label): `not_artifact` dismisses; `artifact` stores the judgment and
  moves on -- to scope when several channels could be meant, to localisation
  when the outline does not cover it, else to the decision; `need_more_evidence`
  looks again closer (three looks at most); `cannot_tell` is manual review.
- **scope / localize / grid**: the agent picks among options the packet
  offered, or names squares; anything else is refused with what is allowed.
- **score review** (one image check on one channel): `too_lenient` /
  `too_aggressive` move the bar one step and look again (`score_rounds`
  looks at most); then the rows settle the check's regions (`_plan_regions`)
  -- far-above and the edge both `artifact`: every region is decided by the
  review itself; the edge `normal`: the regions clear of the bar are
  decided, the rest are not artifacts; the edge unclear (`mixed`,
  `cannot_tell`): only the regions far above the bar are decided, a few,
  the rest confirmed (the strongest probed first); far-above not
  `artifact`: every region is confirmed; every row `cannot_tell`: the
  largest regions go to manual review; far-above `normal`: no region. The
  edge is the just-above row, or -- on a sheet without one -- the heart of
  the largest regions. `whole_tissue: artifact` is one region over the
  tissue. A bar with no region and nothing above it settles the check with
  no look (`settle_if_nothing_at_bar`).
  Every region the settle creates carries the review's `artifact_class` (as
  `class_hint`) and severity (`review_hint`). Regions to confirm are probed:
  the `check_confirm_probe` strongest are asked, the rest held; the probes'
  common verdict is carried to them (`extrapolated`), any disagreement
  releases them (`check_candidates.settle_held`, run after each confirm
  answer). The overflow past `check_confirm_per_channel` is written as
  warnings and summarised once on the check (`manual_overflow`).

Ids are checked against the packet: an answer naming a candidate, an option
or a square the packet did not offer is `invalid_input`, never a guess.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import polygons, schemas
from plexora.plugins.qc.server.answers import CANNOT_TELL, CURRENT, ELSEWHERE, NONE_FITS, REDRAW
from plexora.plugins.qc.server.engine import ENGINE, TERMINAL


def _units(engine, packet):
    return [engine.record["units"][engine.unit_key_of(ref)] for ref in packet["units"]
            if engine.unit_key_of(ref) in engine.record["units"]]


def _note(unit, answer):
    if answer.notes:
        unit.setdefault("notes", []).append(answer.notes)


def _outcome(unit, **extra):
    return {"unit": unit.get("id"), "type": unit.get("type"), "state": unit["state"], **extra}


# -- the audit ----------------------------------------------------------------------------


def overview_blind(candidate) -> bool:
    """Whether an audit tile could have missed this candidate, so a `clean`
    row does not settle it when it is very strong (`_forced`). The tile is
    the whole tissue in ~256 px (sheets.TILE_PX): a seam, shading,
    background or a failed stain spans it and is judged there
    (OVERVIEW_VISIBLE); a misregistration is invisible on one channel's tile
    at any size (OVERVIEW_BLIND_ANY_SIZE). Anything else the tile shows once
    it is more than a dot: blind only while its map cells, drawn at the
    tile's scale, cover at most `overview_blind_tile_px` tile pixels
    (`overview_fine_tile_px` for specks, OVERVIEW_BLIND, which show only as a
    patch) and never above `overview_small_fraction` of the tissue -- a
    candidate covering a large share of a channel the agent called clean was
    settled by the tile. Before the size was measured on the tile every
    aggregate and every candidate under 2% of the tissue was exempt, and on a
    large image that is all of them."""
    klass = candidate.get("class_hint")
    if klass in schemas.OVERVIEW_BLIND_ANY_SIZE:
        return True
    if klass in schemas.OVERVIEW_VISIBLE:
        return False
    measurement = candidate.get("measurement") or {}
    fraction = measurement.get("tissue_fraction")
    if fraction is not None and float(fraction) > ENGINE["overview_small_fraction"]:
        return False
    tile = _tile_pixels(candidate)
    if tile is None:
        return True
    limit = ENGINE["overview_fine_tile_px"] if klass in schemas.OVERVIEW_BLIND \
        else ENGINE["overview_blind_tile_px"]
    return tile <= limit


def _tile_pixels(candidate):
    """The candidate's area on an audit tile, in tile pixels (its map cells
    at TILE_PX across the map grid's longer side -- the tile frames the
    tissue, which is no larger, so this never overstates it); None when the
    unit carries no mask."""
    from plexora.plugins.qc.server.sheets import TILE_PX

    shape = (candidate.get("mask") or {}).get("shape")
    cells = (candidate.get("measurement") or {}).get("cells")
    if not shape:
        return None
    if cells is None:
        cells = int(cand.decode_mask(candidate["mask"]).sum())
    scale = TILE_PX / max(1, max(int(v) for v in shape))
    return float(cells) * scale * scale


def _very_strong(candidate) -> bool:
    """Strong enough to be looked at though its row was called clean: the
    force-confirm score, and -- because scores saturate at 1.0 -- in the top
    `force_confirm_top_share` of its detector's candidates in the scan by
    their unbounded strength (`candidates._rank_strength`). A unit from
    before the rank was recorded is judged on its score alone."""
    if float(candidate.get("score") or 0) < ENGINE["force_confirm_score"]:
        return False
    metrics = candidate.get("metrics") or {}
    rank, of = metrics.get("strength_rank"), metrics.get("strength_of")
    if rank is None or not of:
        return True
    import math

    return int(rank) <= max(1, math.ceil(ENGINE["force_confirm_top_share"] * int(of)))


#: An audit vote: the row was `uncertain` about other outlines it named.
UNSURE_OF_OTHERS = "uncertain about others"


def _forced(candidate) -> bool:
    return _very_strong(candidate) and overview_blind(candidate)


def apply_audit(engine, packet, answer):
    units = _units(engine, packet)
    rows = {u["id"]: u for u in units}
    missing = [name for name in rows if name not in answer.verdicts]
    unknown = [name for name in answer.verdicts if name not in rows]
    if missing or unknown:
        raise AgentError("invalid_input", "answer every channel row of this packet, by name",
                         detail={"missing": missing, "unknown": unknown,
                                 "allowed": list(rows)})
    # A label may be on several rows (a candidate merged across channels).
    labels = {}
    # Rows carry labels; each candidate is described once (`packets._audit_evidence`).
    described = packet["evidence"].get("candidates")
    described = described if isinstance(described, dict) else {}
    for row in packet["evidence"].get("rows") or []:
        for candidate in row.get("candidates") or []:
            if isinstance(candidate, str):
                candidate = {"label": candidate,
                             "id": (described.get(candidate) or {}).get("id", candidate)}
            entry = labels.setdefault(candidate["label"], (candidate["id"], set()))
            entry[1].add(row["channel"])
    bad = [f"{name}: {w}" for name, v in answer.verdicts.items() for w in v.where
           if w != ELSEWHERE and (w not in labels or name not in labels[w][1])]
    if bad:
        raise AgentError("invalid_input", "each row names only the labels drawn on its own "
                         f"tile: {bad}", detail={"allowed": {
                             name: [label for label, (_i, on) in labels.items() if name in on]
                             + [ELSEWHERE] for name in rows}})
    project = units[0]["project"]
    pending = [u for u in engine.units_of("candidate", project)
               if u["state"] == "awaiting_audit"]
    named = {name: {labels[w][0] for w in v.where if w in labels}
             for name, v in answer.verdicts.items()}
    outcomes = {}
    for name, verdict in answer.verdicts.items():
        rows[name]["audit"] = verdict.model_dump(mode="json")
    for candidate in pending:
        members = cand.audit_channels(candidate)
        here = [m for m in members if m in rows]
        if not here:
            continue
        votes = candidate.setdefault("audit_votes", {})
        for name in here:
            verdict = answer.verdicts[name]
            if candidate["id"] in named[name]:
                votes[name] = "named"
            elif verdict.verdict == "uncertain" and named[name]:
                # Unsure about the outlines it named, not this one: a row
                # unsure of its aggregates kept a seam merged over forty
                # rows that the other fifteen had called clean.
                votes[name] = UNSURE_OF_OTHERS
            else:
                votes[name] = verdict.verdict
            if verdict.class_hint and candidate["id"] in named[name]:
                candidate.setdefault("agent_hints", []).append(verdict.class_hint)
        if candidate.get("channel_level"):
            _settle_channel_level(engine, candidate, votes, answer.verdicts, rows)
            continue
        if any(v in ("named", "uncertain") for v in votes.values()):
            candidate["state"] = "awaiting_confirm"
            continue
        # Settled only when every channel it is drawn on has been audited
        # (a later audit packet may hold the rest of its rows).
        audited = [m for m in members if m in votes or _unauditable(engine, project, m)]
        if len(audited) < len(members):
            continue
        if _forced(candidate):
            candidate["state"] = "awaiting_confirm"
            candidate["forced_confirm"] = "strong, and too small or too fine for an audit tile"
            continue
        called = sorted({votes[m] for m in votes})
        engine.close(candidate, "dismissed",
                     f"the channel audit called {', '.join(sorted(votes))} "
                     f"{'/'.join(called)}"
                     + (" without naming this region" if "suspicious" in called else ""))
    for name, verdict in answer.verdicts.items():
        channel = rows[name]
        mine = [c for c in engine.units_of("candidate", project)
                if name in cand.audit_channels(c) and c.get("audit_votes", {}).get(name)]
        if verdict.verdict == "suspicious" and (ELSEWHERE in verdict.where
                                                 or not named[name]):
            channel["audit_note"] = _audit_note(engine, channel, verdict, "flagged")
        elif verdict.verdict == "uncertain" and not mine:
            channel["audit_note"] = _audit_note(engine, channel, verdict,
                                                "manual_review_recommended")
        channel["state"] = "awaiting_candidates"
        channel["reason"] = f"audited {verdict.verdict}"
        outcomes[name] = verdict.verdict
    engine.settle_channels()
    return {"state": "audited", "verdicts": outcomes}


def _unauditable(engine, project, name):
    """A member channel that will never be audited (closed without it)."""
    unit = engine.channel_unit(name, project)
    return unit is None or unit["state"] in TERMINAL or bool(unit.get("audit"))


def _audit_note(engine, channel, verdict, state):
    """What the audit row says of a channel it outlined nothing on: the
    channel's status when nothing else settles it (`engine.settle_channels`).
    A staining problem is settled here, never outlined."""
    from plexora.plugins.qc.server import class_rules

    klass = verdict.class_hint
    adjusted = None
    if klass:
        record = engine.call.session.project(channel["project"])
        klass, adjusted = class_rules.supported_class(
            klass, channels=[channel["id"]],
            image_channels=[c.get("fullname") or c.get("name")
                            for c in record.image.real_channels], hint=verdict.class_hint)
    words = schemas.CLASS_WORDS.get(klass, klass) if klass else "a problem"
    if state == "flagged":
        reason = f"the audit saw {words} with no outline to pursue"
    else:
        reason = "the audit was unsure of this channel and named nothing to look at closer"
    note = {"state": state, "class": klass, "reason": reason}
    if adjusted:
        note["class_adjusted"] = adjusted
    return note


def _settle_channel_level(engine, candidate, votes, verdicts, rows):
    """A verdict on a whole channel (a failed stain), settled by its audit
    row alone: named on a `suspicious` row it is decided; named (or left) on
    an `uncertain` row the channel is left for a person; otherwise it is
    dismissed. Never a confirm packet, never a region."""
    named_on = [m for m, v in votes.items() if v == "named"]
    if any(verdicts[m].verdict == "suspicious" for m in named_on):
        _decide_channel_level(engine, candidate)
        return
    unsure = [m for m, v in votes.items() if v == "uncertain"
              or (v == "named" and verdicts[m].verdict == "uncertain")]
    for name in unsure:
        if name in rows and not rows[name].get("audit_note"):
            rows[name]["audit_note"] = {
                "state": "manual_review_recommended", "class": candidate.get("class_hint"),
                "reason": "the audit was unsure whether this stain failed"}
    called = sorted({verdicts[m].verdict for m in votes})
    engine.close(candidate, "dismissed", f"the channel audit called it {'/'.join(called)}"
                 + (": a person should look" if unsure else ""))


def _decide_channel_level(engine, unit):
    """A failed stain decided from its audit row: severe, covering the
    channel, scoped to it. It removes no tissue -- its marker is unreliable
    in every cell (`cells.calls`) -- so the share of the tissue never caps
    its action."""
    from plexora.plugins.qc.server import strictness

    klass = unit.get("class_hint") or "empty_or_failed_channel"
    unit["decision"] = {"verdict": "artifact", "artifact_class": klass, "severity": "severe",
                        "confidence": "fairly_sure", "boundary": "covers", "scope": "channel",
                        "exclude_recommended": None, "source": "audit"}
    unit["class"] = klass
    measurement = unit.setdefault("measurement", {})
    measurement["refined_fraction"] = 0.0
    measurement["support"] = strictness.measured_support(unit)
    action = strictness.decide_artifact(unit["decision"], measurement, engine.table())["action"]
    unit["action"] = action
    state = {"exclude": "confirmed_exclude", "warn": "confirmed_warn",
             "ignore": "confirmed_noted"}[action]
    engine.close(unit, state, f"the channel audit named a {schemas.CLASS_WORDS.get(klass, klass)}"
                              f"; {action}")
    engine.write_candidate(unit, klass=klass, action=action)


# -- confirm --------------------------------------------------------------------------------


def _needs_scope(engine, unit, answer):
    if answer.scope or unit.get("origin") == "check":
        # A check knows what its region is about (a channel, a cycle, the mask).
        return False
    return len(unit.get("channels") or []) > 1 or unit.get("scope_hint") in (
        "cycle", "cycles", "all_channels", "channels")


def _after_judgment(engine, unit):
    """Scope, then localisation, then the decision: whichever is still owed."""
    decision = unit.get("decision") or {}
    if unit.get("needs_scope"):
        unit["state"] = "awaiting_scope"
        return
    # A check's region is its score map's own outline: there is nothing finer
    # to choose among, so its boundary is recorded, never re-localised.
    if decision.get("boundary") not in (None, "covers") and not unit.get("localized") \
            and not unit.get("fine_grid") \
            and int(unit.get("localize_rounds") or 0) < ENGINE["localize_rounds"]:
        unit["state"] = "awaiting_localize"
        return
    engine.decide(unit)


def apply_confirm(engine, packet, answer):
    units = _units(engine, packet)
    labels = [u.get("label") or u["id"] for u in units]
    judgments = answer.judgments(labels)
    if len(units) > 1 or answer.verdicts is not None:
        missing = [label for label in labels if label not in judgments]
        unknown = [label for label in judgments if label not in labels]
        if missing or unknown:
            raise AgentError("invalid_input", "answer every candidate of this packet, by its "
                             "label, in `verdicts`" if len(units) > 1 else
                             "this packet has one candidate: answer with `verdict`",
                             detail={"missing": missing, "unknown": unknown,
                                     "allowed": labels})
    by_label = dict(zip(labels, units))
    outcomes = [_confirm_one(engine, by_label[label], judgments[label], answer.notes)
                for label in labels]
    # A check's held regions take their probes' common verdict, or are released.
    from plexora.plugins.qc.server import check_candidates

    check_candidates.settle_held(engine, units[0]["project"])
    engine.settle_channels()
    if len(units) == 1:
        return outcomes[0]
    return {"state": "judged", "candidates": {o["unit"]: o for o in outcomes}}


def _confirm_one(engine, unit, answer, notes=""):
    """One candidate's judgment (`answer` is a ConfirmVerdict)."""
    for text in dict.fromkeys(t for t in (notes, answer.notes) if t):
        unit.setdefault("notes", []).append(text)
    if unit["state"] in TERMINAL:
        return _outcome(unit)
    if answer.verdict == "not_artifact":
        engine.close(unit, "dismissed", "the agent judged it real tissue or signal")
        return _outcome(unit)
    if answer.verdict == "cannot_tell":
        engine.manual_review(unit, "the agent could not tell from the evidence")
        return _outcome(unit)
    if answer.verdict == "need_more_evidence":
        level = int(unit.get("level") or 0)
        if level + 1 < ENGINE["confirm_levels"]:
            unit["level"] = level + 1
            return _outcome(unit, level=unit["level"])
        engine.manual_review(unit, "still unclear after the closest look")
        return _outcome(unit)
    klass = answer.artifact_class or unit.get("class_hint") or "other_technical"
    unit["decision"] = {"verdict": "artifact", "artifact_class": klass,
                        "severity": answer.severity or (unit.get("review_hint") or {}).get(
                            "severity") or "moderate",
                        "confidence": answer.confidence, "boundary": answer.boundary,
                        "scope": answer.scope or (unit.get("scope_hint")
                                                  if unit.get("origin") == "check" else None),
                        "exclude_recommended": answer.exclude_recommended}
    if answer.scope:
        _apply_scope(engine, unit, answer.scope)
    unit["needs_scope"] = _needs_scope(engine, unit, answer)
    _after_judgment(engine, unit)
    # The class the region was recorded as: `engine.decide` keeps a class only
    # with its evidence (class_rules), and the agent is told when it did not.
    decision = unit.get("decision") or {}
    adjusted = decision.get("class_adjusted")
    return _outcome(unit, artifact_class=unit.get("class") or decision.get("artifact_class")
                    or klass, **({"class_adjusted": adjusted} if adjusted else {}))


def _apply_scope(engine, unit, scope, option=None):
    from plexora.plugins.qc.server import packets

    decision = unit.setdefault("decision", {})
    decision["scope"] = scope
    if option is None:
        options = packets.scope_options(engine, unit)
        option = next((o for o in options if o["scope"] == scope), None)
    if option is not None:
        unit["channels"] = list(option["channels"])
        if option.get("cycle"):
            unit["cycles"] = [option["cycle"]]
        if option.get("cycles"):
            unit["cycles"] = list(option["cycles"])


def apply_scope(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    options = {o["id"]: o for o in packet["evidence"].get("options") or []}
    if answer.chosen != CANNOT_TELL and answer.chosen not in options:
        raise AgentError("invalid_input", f"{answer.chosen!r} is not an option of this packet",
                         detail={"allowed": sorted(options) + [CANNOT_TELL]})
    unit["needs_scope"] = False
    if answer.chosen == CANNOT_TELL:
        unit.setdefault("decision", {})["scope"] = unit.get("scope_hint")
        unit["decision"]["confidence"] = "unsure"
    else:
        option = options[answer.chosen]
        _apply_scope(engine, unit, option["scope"], option)
    _after_judgment(engine, unit)
    engine.settle_channels()
    return _outcome(unit, scope=(unit.get("decision") or {}).get("scope"))


def apply_localize(engine, packet, answer):
    from plexora.plugins.qc.server import sheets

    unit = _units(engine, packet)[0]
    _note(unit, answer)
    letters = {sheets.VARIANT_LETTERS[name]: name for name in (unit.get("variants") or {})
               if name in sheets.VARIANT_LETTERS}
    from plexora.plugins.qc.server import packets

    allowed = sorted(letters) + [CURRENT, NONE_FITS]
    if REDRAW in (packet.get("allowed") or ()) and packets.can_redraw(engine, unit):
        allowed.append(REDRAW)
    if answer.chosen not in allowed:
        raise AgentError("invalid_input", f"{answer.chosen!r} is not an outline of this packet",
                         detail={"allowed": allowed})
    if answer.chosen == REDRAW:
        # The segmentation model outlines it again; the unit stays awaiting
        # its outline, and the next localize packet draws the new trace.
        outcome = packets.redraw_with_sam(engine, unit)
        return _outcome(unit, redraw=outcome["status"])
    unit["localize_rounds"] = int(unit.get("localize_rounds") or 0) + 1
    if answer.chosen == NONE_FITS:
        unit["state"] = "awaiting_grid"
        return _outcome(unit)
    unit["variant"] = "standard" if answer.chosen == CURRENT else letters[answer.chosen]
    unit["localized"] = True
    engine.decide(unit)
    engine.settle_channels()
    return _outcome(unit, variant=unit["variant"])


def apply_grid(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    spec = unit.get("grid_spec") or {}
    ids = {s["id"] for s in spec.get("squares") or []}
    bad = [c for c in answer.cells if c not in ids]
    if bad:
        raise AgentError("invalid_input", f"not squares of this grid: {bad}",
                         detail={"allowed": sorted(ids)})
    if not answer.cells:
        engine.manual_review(unit, "no grid square was named")
        engine.settle_channels()
        return _outcome(unit)
    scan = engine.scan(unit["project"])
    mask = polygons.rebuild_from_grid(spec, answer.cells, scan.grid["shape"],
                                      tissue=scan.tissue())
    if not mask.any():
        engine.manual_review(unit, "the squares named hold no tissue")
        engine.settle_channels()
        return _outcome(unit)
    unit["grid_rounds"] = int(unit.get("grid_rounds") or 0) + 1
    if answer.refine and unit["grid_rounds"] < ENGINE["grid_rounds"] and mask.sum() > 4:
        unit["grid_region"] = cand.encode_mask(mask)
        unit["grid_spec"] = None
        return _outcome(unit, refine=True)
    unit["mask"] = cand.encode_mask(mask)
    unit["variants"] = None
    unit["variant"] = "tight"
    # The squares named are the envelope; the artifact is traced inside them.
    unit["envelope_geometry"] = polygons.mask_to_geometry(mask, scan.grid)
    unit.pop("geometry", None)
    unit.pop("localize_trace", None)
    from plexora.plugins.qc.server.candidates import area_fraction
    from plexora.plugins.qc.server.scan import bbox_fullres

    unit["bbox"] = list(bbox_fullres(scan.grid, mask))
    unit.setdefault("measurement", {})["tissue_fraction"] = area_fraction(mask, scan)
    unit["localized"] = True
    decision = unit.setdefault("decision", {})
    decision.setdefault("verdict", "artifact")
    decision.setdefault("artifact_class", unit.get("class_hint") or "other_technical")
    if answer.artifact_class:
        decision["artifact_class"] = answer.artifact_class
    decision["severity"] = answer.severity
    decision["confidence"] = answer.confidence
    decision["boundary"] = "covers"
    engine.decide(unit)
    engine.settle_channels()
    return _outcome(unit, squares=len(answer.cells))


# -- the final review -----------------------------------------------------------------------


def apply_final(engine, packet, answer):
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    labels = {r["label"]: r["candidate"] for r in unit.get("regions") or []}
    unit["review"] = answer.model_dump(mode="json")
    if answer.verdict == "consistent" and not answer.recommend_manual_review:
        engine.close(unit, "reviewed", "the whole picture was judged consistent")
        return _outcome(unit)
    reopened = []
    if answer.verdict == "inconsistent" and int(unit.get("reopened") or 0) < \
            ENGINE["final_reopens"]:
        for concern in answer.concerns:
            candidate_id = labels.get(concern.target)
            if not candidate_id:
                continue
            key = engine.unit_key_of({"project": unit["project"], "type": "candidate",
                                      "id": candidate_id})
            target = engine.record["units"].get(key)
            if target is None or target["state"] not in TERMINAL:
                continue
            target.setdefault("concerns", []).append(concern.model_dump(mode="json"))
            target["state"] = "awaiting_confirm"
            target["level"] = 2
            target["reopened"] = True
            target["localized"] = False
            target["localize_rounds"] = 0
            # The outline is chosen again, not kept from the first decision,
            # and traced afresh inside the new one.
            for key in ("geometry", "variant", "envelope_geometry", "localize_trace"):
                target.pop(key, None)
            reopened.append(candidate_id)
        if reopened:
            unit["reopened"] = int(unit.get("reopened") or 0) + 1
            unit["state"] = "pending"
            return _outcome(unit, reopened=reopened)
    if answer.verdict == "consistent":
        # recommend_manual_review though the picture itself checked out: the
        # review stands (it was consistent) -- a person is just asked to look,
        # not told the review could not say.
        n = len(unit.get("regions") or [])
        reason = (f"the agent recommends a person check {n} region{'s' if n != 1 else ''}"
                 if n else "the agent recommends a person check this image")
        engine.close(unit, "reviewed", reason)
        return _outcome(unit)
    engine.close(unit, "manual_review_recommended",
                 "the final review was not consistent: a person should look at the "
                 "regions named" if answer.concerns else "the final review could not say")
    return _outcome(unit)


# -- the image checks ---------------------------------------------------------------------


def apply_score_review(engine, packet, answer):
    from plexora.plugins.qc.server import checks_result, score_fields

    unit = _units(engine, packet)[0]
    _note(unit, answer)
    shown = unit.get("shown") or {}
    allowed = list(shown.get("strata") or [])
    missing = [k for k in allowed if k not in answer.strata]
    unknown = [k for k in answer.strata if k not in allowed]
    if missing or unknown:
        raise AgentError("invalid_input", "judge every row of places shown, by its stratum",
                         detail={"missing": missing, "unknown": unknown, "allowed": allowed})
    rounds = int(unit.get("rounds") or 0) + 1
    unit["rounds"] = rounds
    offset = int(unit.get("offset_steps") or 0)
    unit.setdefault("strata_verdicts", []).append({
        "round": rounds, "threshold": shown.get("threshold"), "offset_steps": offset,
        "strata": dict(answer.strata), "threshold_verdict": answer.threshold,
        "whole_tissue": answer.whole_tissue, "confidence": answer.confidence})
    moved = 0
    if answer.threshold in ("too_lenient", "too_aggressive"):
        wanted = score_fields.clamp_steps(offset + (1 if answer.threshold == "too_lenient"
                                                    else -1))
        moved = wanted - offset
        if moved:
            unit["offset_steps"] = wanted
            unit["threshold_source"] = "agent_refined"
            if rounds < int(ENGINE["score_rounds"]) and not _nothing_at_bar(engine, unit):
                checks_result.record(engine, unit)
                return _outcome(unit, offset_steps=wanted, moved=moved)
    return _settle_check(engine, unit, answer, moved_unseen=bool(moved))


def _nothing_at_bar(engine, unit, field=None) -> bool:
    """The bar flags no region and no evaluable place scores at or above it:
    a look would show only tissue below the bar, nothing to judge."""
    import numpy as np

    from plexora.plugins.qc.server import checks_bulk, score_fields

    field = field if field is not None else checks_bulk.field_of(engine, unit)
    if field is None:
        return False
    bar = score_fields.bar(field, unit.get("offset_steps") or 0)
    if score_fields.regions(field, bar["value"], geometry=False)["n_regions"]:
        return False
    values = field.values[field.sampleable()]
    return not int((values >= bar["value"]).sum())


def settle_if_nothing_at_bar(engine, unit, field=None) -> bool:
    """Close a check unit `decided` with no look when its bar holds nothing
    to judge (`_nothing_at_bar`); True when it did."""
    from plexora.plugins.qc.server import checks_bulk, checks_result, score_fields

    field = field if field is not None else checks_bulk.field_of(engine, unit)
    if field is None or not _nothing_at_bar(engine, unit, field) \
            or score_fields.global_possible(field).get("possible"):
        # (A problem that may be everywhere is still shown, as the tissue.)
        return False
    bar = score_fields.bar(field, unit.get("offset_steps") or 0)
    found = score_fields.regions(field, bar["value"], geometry=False)
    unit["threshold"] = bar["value"]
    unit.setdefault("threshold_source", "auto")
    unit["regions"] = {"decided": 0, "to_confirm": 0, "dismissed": 0, "manual_review": 0,
                       "residual": 0}
    unit["n_regions"] = 0
    unit["flagged_pct"] = 0.0
    unit["denominator"] = found["denominator"]
    words = schemas.CHECK_WORDS.get(unit["check"], unit["check"])
    engine.close(unit, "decided", f"{words} at {bar['value']:.3g}: no region and no place "
                                  "at or above the bar to judge")
    checks_result.record(engine, unit)
    engine.settle_channels()
    return True


def _plan_regions(verdicts, regions, bar, *, moved_unseen=False):
    """{decided, confirm, dismissed, review}: indices into `regions` (a
    check's regions at `bar`, each with `mean` and `cells`), from the rows'
    verdicts. Pure.

    The edge is the just-above row; a sheet without one (an artifact
    category's) has the heart of the largest regions stand in for it. An
    edge that looks like the artifact decides every region; `normal` decides
    those clear of the bar and dismisses the rest; unclear (`mixed`,
    `cannot_tell`) decides only regions `score_unclear_edge_margin_steps`
    above the bar -- at most `check_max_manual_regions`, largest first --
    and confirms the rest, the strongest first."""
    top = verdicts.get("strongly_abnormal") or verdicts.get("clustered") \
        or verdicts.get("borderline_above")
    edge = verdicts["borderline_above"] if "borderline_above" in verdicts \
        else verdicts.get("clustered")
    if moved_unseen and edge == "artifact":
        # The bar moved on the last look: the places now just above it were
        # not seen at this bar, so they are confirmed rather than decided.
        edge = "mixed"
    out = {"decided": [], "confirm": [], "dismissed": [], "review": []}
    n = len(regions)
    if verdicts and all(v == "cannot_tell" for v in verdicts.values()):
        out["review"] = list(range(min(n, int(ENGINE["check_max_manual_regions"]))))
        return out
    if top == "artifact":
        if edge in (None, "artifact"):
            out["decided"] = list(range(n))
        elif edge == "normal":
            margin = float(ENGINE["score_direct_confirm_margin_steps"]) * bar["step"]
            for index, region in enumerate(regions):
                clear = region["mean"] >= bar["value"] + margin
                out["decided" if clear else "dismissed"].append(index)
        else:
            margin = float(ENGINE["score_unclear_edge_margin_steps"]) * bar["step"]
            far = sorted((i for i, r in enumerate(regions) if r["mean"] >= bar["value"] + margin),
                         key=lambda i: (-int(regions[i].get("cells") or 0), i))
            cap = int(ENGINE["check_max_manual_regions"])
            out["decided"] = sorted(far[:cap])
            rest = [i for i in range(n) if i not in set(out["decided"])]
            # Strongest first, so the probes are the most likely artifacts.
            out["confirm"] = sorted(rest, key=lambda i: (-float(regions[i].get("mean") or 0), i))
    elif top in ("mixed", "cannot_tell"):
        out["confirm"] = list(range(n))
    elif top == "normal" and edge in ("artifact", "mixed"):
        # Far above looks normal, just above does not: an odd picture, so
        # every region is looked at on its own.
        out["confirm"] = list(range(n))
    return out


def _settle_check(engine, unit, answer, *, moved_unseen=False):
    """Turn a check's regions at its bar into decisions, confirms or nothing."""
    from plexora.plugins.qc.server import check_candidates, checks_bulk, checks_result
    from plexora.plugins.qc.server import score_fields

    field = checks_bulk.field_of(engine, unit)
    if field is None:
        engine.close(unit, "manual_review_recommended", "the check's scores are no longer "
                                                        "cached; run the check again")
        checks_result.record(engine, unit)
        engine.settle_channels()
        return _outcome(unit)
    bar = score_fields.bar(field, unit.get("offset_steps") or 0)
    unit["threshold"] = bar["value"]
    unit.setdefault("threshold_source", "auto")
    found = score_fields.regions(field, bar["value"])
    # Regions on the glass are no finding: dropped before they are planned.
    kept, on_glass = check_candidates.on_tissue(engine, unit["project"], found["regions"])
    found = {**found, "regions": kept}
    verdicts = dict(answer.strata)
    # What the review saw (its class and severity) goes with every region it
    # creates: decided, to confirm, or left for manual review.
    hint = check_candidates.review_hint(answer, unit["check"])
    everything_unclear = verdicts and all(v == "cannot_tell" for v in verdicts.values())
    whole = answer.whole_tissue if (unit.get("shown") or {}).get("global") else None
    if whole == "artifact":
        decided = [check_candidates.global_unit(engine, unit, field, bar)]
        confirm, dismissed, review = [], [], []
    else:
        plan = _plan_regions(verdicts, found["regions"], bar, moved_unseen=moved_unseen)
        decided, confirm = plan["decided"], plan["confirm"]
        dismissed, review = plan["dismissed"], plan["review"]
    # A check's regions come four to a sheet: more of them get a look before
    # the rest are left for a person.
    limit = max(int(ENGINE["candidates_per_channel"]), int(ENGINE["check_confirm_per_channel"]))
    overflow = confirm[limit:]
    confirm = confirm[:limit]
    added = overflow[:max(0, int(ENGINE["check_max_manual_regions"]) - len(review))]
    review += added
    residual = len(overflow) - len(added)
    counts = {"decided": 0, "to_confirm": 0, "dismissed": len(dismissed),
              "manual_review": 0, "residual": residual,
              "dismissed_borderline": len(dismissed), "on_glass": on_glass}
    decision = {"verdict": "artifact",
                "artifact_class": answer.artifact_class or schemas.CHECK_CLASS[unit["check"]],
                "severity": answer.severity or "moderate", "confidence": answer.confidence,
                "boundary": "covers", "scope": None, "source": "score_review"}

    def place(candidate):
        key = engine.unit_key_of({"project": candidate["project"], "type": "candidate",
                                  "id": candidate["id"]})
        held = engine.record["units"].get(key)
        if held is not None:
            return held, False
        engine.record["units"][key] = candidate
        return candidate, True

    for item in decided:
        candidate = item if isinstance(item, dict) else check_candidates.unit_for(
            engine, unit, field, found, item, bar, hint=hint)
        candidate, fresh = place(candidate)
        if not fresh and candidate["state"] in TERMINAL:
            continue
        candidate["decision"] = {**decision, "scope": candidate.get("scope_hint")}
        if not answer.artifact_class and candidate.get("class_hint") in schemas.CLASS_WORDS:
            # No class named: the region's own (an Artifact Detector category's).
            candidate["decision"]["artifact_class"] = candidate["class_hint"]
        engine.decide(candidate)
        counts["decided"] += 1
        _absorb(engine, candidate)
    fresh_confirm = []
    for index in confirm:
        candidate, fresh = place(check_candidates.unit_for(engine, unit, field, found, index,
                                                           bar, hint=hint))
        if fresh:
            counts["to_confirm"] += 1
            fresh_confirm.append(candidate)
    # The strongest few are looked at first; their common verdict carries to
    # the rest (`check_candidates.hold_for_probes`).
    group = check_candidates.hold_for_probes(engine, unit, fresh_confirm)
    if group is not None:
        counts["probes"] = len(group["probes"])
        counts["held"] = len(group["held"])
    for index in review:
        candidate, fresh = place(check_candidates.unit_for(engine, unit, field, found, index,
                                                           bar, hint=hint))
        if fresh:
            engine.manual_review(candidate, "the review of this check's places could not "
                                            "tell artifact from normal tissue")
            counts["manual_review"] += 1
    if added or residual:
        # The overflow is said once, for the check: counts and the largest few
        # (each written region stays a warning; none needs a look).
        unit["manual_overflow"] = {
            "manual_review": len(added), "residual": residual,
            "largest": [{k: found["regions"][i].get(k) for k in ("id", "cells", "area_um2",
                                                                 "max", "bbox")}
                        for i in overflow[:3] if i < len(found["regions"])],
            "why": "more regions to confirm than this check's looks "
                   "(check_confirm_per_channel)"}
    unit["regions"] = counts
    # What the bar flagged, kept apart from what the review wrote.
    unit["at_threshold"] = {"n_regions": found["n_regions"],
                            "flagged_pct": found["flagged_pct"],
                            "denominator": found["denominator"]}
    kept = [found["regions"][i] for i in [*decided, *confirm, *review] if isinstance(i, int)
            and i < len(found["regions"])]
    unit["n_regions"] = len(kept) + sum(1 for i in decided if isinstance(i, dict))
    unit["flagged_pct"] = round(sum(float(r.get("weight_pct") or 0.0) for r in kept), 3) \
        if not any(isinstance(i, dict) for i in decided) else found["flagged_pct"]
    unit["denominator"] = found["denominator"]
    unit["whole_tissue"] = answer.whole_tissue
    if everything_unclear and not whole:
        engine.close(unit, "manual_review_recommended",
                     "the places sampled could not be told apart from normal tissue")
    else:
        words = schemas.CHECK_WORDS.get(unit["check"], unit["check"])
        engine.close(unit, "decided", f"{words} at {bar['value']:.3g} "
                                      f"({unit.get('threshold_source') or 'auto'}): "
                                      f"{counts['decided']} decided, {counts['to_confirm']} to "
                                      f"confirm, {counts['dismissed']} not artifacts")
    checks_result.record(engine, unit)
    engine.settle_channels()
    return _outcome(unit, regions=counts, threshold=bar["value"],
                    offset_steps=unit.get("offset_steps") or 0,
                    **({"manual_overflow": unit["manual_overflow"]}
                       if unit.get("manual_overflow") else {}))


def apply_visual_scan(engine, packet, answer):
    """The pass is over: the regions the agent wrote while the packet was out
    (`unit["written"]`, by `segment_qc_roi`) take in the detector candidates
    inside them, and the unit closes with what was written and what was left
    for a person."""
    from plexora.plugins.qc.server import checks_result

    unit = _units(engine, packet)[0]
    _note(unit, answer)
    unit["left"] = [p.model_dump(mode="json") for p in answer.left]
    unit["status"] = answer.status
    written = []
    for candidate_id in unit.get("written") or []:
        held = engine.record["units"].get(engine.unit_key_of(
            {"project": unit["project"], "type": "candidate", "id": candidate_id}))
        if held is not None:
            written.append(held)
    for region in written:
        _absorb(engine, region, because="which the agent outlined on the overview")
    kept = [u for u in written if u["state"] in schemas.WRITTEN_STATES]
    unit["regions"] = {"written": len(kept),
                       "merged": sum(1 for u in written if u["state"] == "merged"),
                       "left_for_user": len(unit["left"])}
    n = len(kept)
    reason = ("nothing clearly abnormal on the overview" if answer.status == "nothing_found"
              and not n else f"{n} region{'s' if n != 1 else ''} outlined by the agent")
    engine.close(unit, "decided", reason)
    checks_result.record(engine, unit)
    return _outcome(unit, regions=unit["regions"], status=answer.status)


def _absorb(engine, decided, *, because=None):
    """A region a check decided takes in the detector candidates of the same
    class lying inside it that no one has looked at yet: the same artifact,
    so no look is spent on it twice."""
    import numpy as np

    if decided.get("state") not in schemas.CONFIRMED_STATES:
        return
    because = because or f"which the {decided.get('detector')} check decided"
    mask = engine.mask_of(decided)
    klass = decided.get("class")
    for other in engine.units_of("candidate", decided["project"]):
        if other is decided or other.get("origin") == "check" or \
                other["state"] not in ("awaiting_audit", "awaiting_confirm") or \
                int(other.get("level") or 0) or other.get("class_hint") != klass:
            continue
        theirs = engine.mask_of(other)
        inside = np.logical_and(mask, theirs).sum()
        if theirs.sum() and inside / theirs.sum() >= ENGINE["merge_contain"]:
            decided.setdefault("merged", []).append(other["id"])
            decided["channels"] = list(dict.fromkeys([*decided.get("channels", []),
                                                      *other.get("channels", [])]))
            engine.close(other, "merged", f"inside {decided['id']}, {because}")


APPLY = {"channel_audit": apply_audit, "artifact_confirm": apply_confirm,
         "artifact_scope": apply_scope, "artifact_localize": apply_localize,
         "artifact_grid": apply_grid, "final_qc_review": apply_final,
         "score_review": apply_score_review, "visual_scan": apply_visual_scan}
