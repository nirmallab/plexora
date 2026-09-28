"""What each QC answer does: one handler per packet kind, all deterministic.

The rules (the full table is in the engine's docstring):

- **audit**: a `clean` row dismisses its candidates below the force-confirm
  score (a strong one is looked at anyway); `suspicious` keeps the candidates
  it names (and `elsewhere` opens a grid over the tissue); `uncertain` opens a
  whole-channel look.
- **confirm**: `not_artifact` dismisses; `artifact` stores the judgment and
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


def apply_audit(engine, packet, answer):
    units = _units(engine, packet)
    rows = {u["id"]: u for u in units}
    missing = [name for name in rows if name not in answer.verdicts]
    unknown = [name for name in answer.verdicts if name not in rows]
    if missing or unknown:
        raise AgentError("invalid_input", "answer every channel row of this packet, by name",
                         detail={"missing": missing, "unknown": unknown,
                                 "allowed": list(rows)})
    labels = {}
    for row in packet["evidence"].get("rows") or []:
        for candidate in row.get("candidates") or []:
            labels[candidate["label"]] = (candidate["id"], row["channel"])
    bad = [f"{name}: {w}" for name, v in answer.verdicts.items() for w in v.where
           if w != ELSEWHERE and (w not in labels or labels[w][1] != name)]
    if bad:
        raise AgentError("invalid_input", "each row names only the labels drawn on its own "
                         f"tile: {bad}", detail={"allowed": {
                             name: [label for label, (_i, row) in labels.items() if row == name]
                             + [ELSEWHERE] for name in rows}})
    project = units[0]["project"]
    pending = {u["id"]: u for u in engine.units_of("candidate", project)
               if u["state"] == "awaiting_audit"}
    outcomes = {}
    for name, verdict in answer.verdicts.items():
        channel = rows[name]
        channel["audit"] = verdict.model_dump(mode="json")
        named = {labels[w][0] for w in verdict.where if w in labels}
        mine = [c for c in pending.values() if c.get("audit_channel") == name]
        for candidate in mine:
            if candidate["id"] in named or verdict.verdict == "uncertain" or \
                    float(candidate.get("score") or 0) >= ENGINE["force_confirm_score"]:
                candidate["state"] = "awaiting_confirm"
                if verdict.class_hint and candidate["id"] in named:
                    candidate.setdefault("agent_hints", []).append(verdict.class_hint)
            else:
                engine.close(candidate, "dismissed",
                             f"the channel audit called {name} {verdict.verdict}"
                             + (" without naming this region" if verdict.verdict ==
                                "suspicious" else ""))
        if verdict.verdict == "suspicious" and ELSEWHERE in verdict.where:
            _open_region(engine, channel, verdict, grid=True)
        elif verdict.verdict == "uncertain" and not mine:
            _open_region(engine, channel, verdict, grid=False)
        elif verdict.verdict == "suspicious" and not named and not mine:
            _open_region(engine, channel, verdict, grid=True)
        channel["state"] = "awaiting_candidates"
        channel["reason"] = f"audited {verdict.verdict}"
        outcomes[name] = verdict.verdict
    engine.settle_channels()
    return {"state": "audited", "verdicts": outcomes}


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
    unit = _units(engine, packet)[0]
    _note(unit, answer)
    if answer.verdict == "not_artifact":
        engine.close(unit, "dismissed", "the agent judged it real tissue or signal")
        engine.settle_channels()
        return _outcome(unit)
    if answer.verdict == "cannot_tell":
        engine.manual_review(unit, "the agent could not tell from the evidence")
        engine.settle_channels()
        return _outcome(unit)
    if answer.verdict == "need_more_evidence":
        level = int(unit.get("level") or 0)
        if level + 1 < ENGINE["confirm_levels"]:
            unit["level"] = level + 1
            return _outcome(unit, level=unit["level"])
        engine.manual_review(unit, "still unclear after the closest look")
        engine.settle_channels()
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
    engine.settle_channels()
    return _outcome(unit, artifact_class=klass)


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
    unit["geometry"] = polygons.mask_to_geometry(mask, scan.grid)
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
            # The outline is chosen again, not kept from the first decision.
            target.pop("geometry", None)
            target.pop("variant", None)
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
         "channel_outlier": _cells}
