"""What each QC answer does: one handler per packet kind, all deterministic.

The rules (the full table is in the engine's docstring):

- **audit**: a `clean` row dismisses its candidates, unless one is strong
  (the force-confirm score) and of a kind an audit tile cannot show
  (`overview_blind`); `suspicious` keeps the candidates it names (and
  `elsewhere` opens a grid over the tissue); `uncertain` opens a
  whole-channel look. A candidate drawn on several rows is kept when any
  row names it (or is uncertain), and settled once all its rows are in.
- **confirm** (per candidate; a first-look packet may carry several, answered
  by label): `not_artifact` dismisses; `artifact` stores the judgment and
  moves on -- to scope when several channels could be meant, to localisation
  when the outline does not cover it, else to the decision; `need_more_evidence`
  looks again closer (three looks at most); `cannot_tell` is manual review.
- **scope / localize / grid**: the agent picks among options the packet
  offered, or names squares; anything else is refused with what is allowed.

Ids are checked against the packet: an answer naming a candidate, an option
or a square the packet did not offer is `invalid_input`, never a guess.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import polygons, schemas
from plexora.plugins.qc.server.answers import CANNOT_TELL, CURRENT, ELSEWHERE, NONE_FITS
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
    row does not settle it when its score is high. The tile is the whole
    tissue in ~256 px: a seam, shading, background or a failed stain spans it
    and is judged there (OVERVIEW_VISIBLE); aggregates (specks) and a
    misregistration (a sub-cell shift between cycles) are invisible on it at
    any size (OVERVIEW_BLIND); anything else is visible once it covers more
    than `overview_small_fraction` of the tissue. Before this rule every
    candidate scoring 1.0 was looked at on a clean row -- a round core's rim
    seam got a look in every channel the audit had already called clean."""
    klass = candidate.get("class_hint")
    if klass in schemas.OVERVIEW_BLIND:
        return True
    if klass in schemas.OVERVIEW_VISIBLE:
        return False
    fraction = (candidate.get("measurement") or {}).get("tissue_fraction")
    return fraction is None or float(fraction) <= ENGINE["overview_small_fraction"]


def _forced(candidate) -> bool:
    return float(candidate.get("score") or 0) >= ENGINE["force_confirm_score"] and \
        overview_blind(candidate)


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
    for row in packet["evidence"].get("rows") or []:
        for candidate in row.get("candidates") or []:
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
            votes[name] = "named" if candidate["id"] in named[name] else verdict.verdict
            if verdict.class_hint and candidate["id"] in named[name]:
                candidate.setdefault("agent_hints", []).append(verdict.class_hint)
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
        if verdict.verdict == "suspicious" and ELSEWHERE in verdict.where:
            _open_region(engine, channel, verdict, grid=True)
        elif verdict.verdict == "uncertain" and not mine:
            _open_region(engine, channel, verdict, grid=False)
        elif verdict.verdict == "suspicious" and not named[name] and not mine:
            _open_region(engine, channel, verdict, grid=True)
        channel["state"] = "awaiting_candidates"
        channel["reason"] = f"audited {verdict.verdict}"
        outcomes[name] = verdict.verdict
    engine.settle_channels()
    return {"state": "audited", "verdicts": outcomes}


def _unauditable(engine, project, name):
    """A member channel that will never be audited (closed without it)."""
    unit = engine.channel_unit(name, project)
    return unit is None or unit["state"] in TERMINAL or bool(unit.get("audit"))


def _open_region(engine, channel, verdict, *, grid):
    """A candidate the scan did not propose: the whole tissue of a channel the
    agent was unsure of (a look), or a grid over it (`elsewhere`)."""
    scan = engine.scan(channel["project"])
    tissue = scan.tissue()
    klass = verdict.class_hint or "other_technical"
    import hashlib

    candidate_id = "cand_" + hashlib.sha1(
        f"{channel['project']}|{channel['id']}|audit|{klass}".encode("utf-8")).hexdigest()[:10]
    key = engine.unit_key_of({"project": channel["project"], "type": "candidate",
                              "id": candidate_id})
    if key in engine.record["units"]:
        return engine.record["units"][key]
    from plexora.plugins.qc.server.scan import bbox_fullres

    unit = {"type": "candidate", "project": channel["project"], "id": candidate_id,
            "channel": channel["id"], "channels": [channel["id"]], "cycles": [],
            "audit_channel": channel["id"], "detector": "audit", "detector_version": "1",
            "class_hint": klass, "alternatives": [], "scope_hint": "channel",
            "score": 0.5, "severity": 0.5, "metrics": {}, "primary_metric": "",
            "mask": cand.encode_mask(tissue), "bbox": list(bbox_fullres(scan.grid, tissue)
                                                        or (0, 0, *scan.grid["image_size"])),
            "measurement": {"tissue_fraction": 1.0}, "level": 0,
            "state": "awaiting_grid" if grid else "awaiting_confirm",
            "origin": "audit"}
    unit["peak"] = [(unit["bbox"][0] + unit["bbox"][2]) / 2,
                    (unit["bbox"][1] + unit["bbox"][3]) / 2]
    if grid:
        unit["decision"] = {"verdict": "artifact", "artifact_class": klass,
                            "severity": "moderate", "confidence": "fairly_sure",
                            "boundary": "cannot_tell"}
    engine.record["units"][key] = unit
    engine.log(event="candidate_opened", unit=key, by="audit", grid=grid)
    return unit


# -- confirm --------------------------------------------------------------------------------


def _needs_scope(engine, unit, answer):
    if answer.scope:
        return False
    return len(unit.get("channels") or []) > 1 or unit.get("scope_hint") in (
        "cycle", "cycles", "all_channels", "channels")


def _after_judgment(engine, unit):
    """Scope, then localisation, then the decision: whichever is still owed."""
    decision = unit.get("decision") or {}
    if unit.get("needs_scope"):
        unit["state"] = "awaiting_scope"
        return
    if decision.get("boundary") not in (None, "covers") and not unit.get("localized") \
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
                        "severity": answer.severity or "moderate",
                        "confidence": answer.confidence, "boundary": answer.boundary,
                        "scope": answer.scope, "exclude_recommended": answer.exclude_recommended}
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
    allowed = sorted(letters) + [CURRENT, NONE_FITS]
    if answer.chosen not in allowed:
        raise AgentError("invalid_input", f"{answer.chosen!r} is not an outline of this packet",
                         detail={"allowed": allowed})
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
    decision.setdefault("severity", "moderate")
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
    engine.close(unit, "manual_review_recommended",
                 "the final review was not consistent: a person should look at the "
                 "regions named" if answer.concerns else "the final review could not say")
    return _outcome(unit)


def _cells(engine, packet, answer):
    from plexora.plugins.qc.server.cells import packets as cell_packets

    return cell_packets.apply(engine, packet, answer)


APPLY = {"channel_audit": apply_audit, "artifact_confirm": apply_confirm,
         "artifact_scope": apply_scope, "artifact_localize": apply_localize,
         "artifact_grid": apply_grid, "final_qc_review": apply_final,
         "cell_intensity": _cells, "cell_area": _cells, "cycle_stability": _cells,
         "channel_outlier": _cells, "cell_modules": _cells}
