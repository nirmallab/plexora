"""Re-planning when a marker fails: the plan is redone for what is left, in a
loop, until a round adds no new failure.

Planning runs once, at the start (`context.order`, the panel context). A
gate that fails (`hierarchy.grade` failed: closed for review, no positive
population, failed QC) changes what the rest of the plan stands on, and an
experiment often has several. So every failure runs `after_close`.

Deterministic, by code, every time:

1. **Order.** The markers no image has looked at yet are reordered from the
   panel without the failed markers (`context.order`), in the slots they
   held; started and closed markers keep their place.
2. **Dependents.** A marker not looked at yet is rescored without the failed
   gate (`Engine.reference_failed`); an open one past a look gets its
   references refreshed (`Engine.references_ready`) -- a packet already out
   is stale by its ledger fingerprint; an accepted one whose scored start
   the failed gate led, and that no look walked from, is looked at again
   from a start scored without it: nothing but the failed gate placed it.

Nothing here closes a marker on numbers: a close needs a picture behind it
(`seen_failing`, `Engine.close`).
3. **Patterns.** Failures are read by scope: one marker of one image; an
   image -- `image_flag_min` of its markers whose looks named the same
   artifact (`IMAGE_FLAGS`); the panel -- the same marker failing the same
   way in two images of the session.

The agent, through one `replan` packet (the planning task's, beside
`panel_context`), is asked only what code cannot call:

- a marker whose population may not exist: the whole-image view called its
  positives an artifact, or even its brightest positives sit on an
  exclusive lineage (`Engine.lineage_purity`), and no look ever saw its own
  stain with confidence (`own_seen`);
- an image whose looks keep naming the same artifact.

A marker about to close for review on looks that could not read its stain
-- most cells uncertain or artifact, a stain artifact named (`unreadable`)
-- gets the same whole-image check first (`check_unreadable`): its failed
or empty answer closes it as failed or empty, any other keeps the review
close it was about to make (lsp35766_p193 panCK and NKG2A, 2026-11-22:
both closed for review at gates with 22-80 % of cells positive).

A lineage suspect the whole-image view has not judged gets that check
first (`check_whole_image`, a `qc_confirm` packet): the planner calls a
population absent on a picture's verdict and keeps it on numbers alone
(LSP11761 PD1: absent 6 of 6 after a whole-image look said artifact, 0 of 3
on the lineage numbers, live 2026-10-11). Its no-positive-population or
failed answer closes the marker; any other answer puts it to the planner
with that verdict beside the numbers (`whole_image`).

Its answer is closed words (`answers.ReplanAnswer`); `apply` acts on them and
the closes it makes run the deterministic part again -- the loop. Each marker
and each image flag is asked about at most once, so the rounds are bounded
by the panel. Context never moves a gate: every outcome here routes a
marker (closes it, reopens it for looks, or notes an image finding on
it, which leaves an accepted gate accepted), and every threshold
is still the data's and the looks'.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import hierarchy, schemas

#: [cal] `image_flag_min`: an image is suspect when this many of its markers'
#: looks named the same artifact.
#: The stain its looks could not read (`unreadable`), all [cal] on
#: lsp35766_p193 (2026-11-22), where the owner called panCK and NKG2A failed:
#: `unreadable_min_cells` numbered cells read before the reads say anything
#: (a look is about 20); `artifact_look_share` of one look's cells, of at
#: least `artifact_look_min`, called `artifact` (NKG2A 0.93, CD19 0.75; no
#: working stain there above 0.25); otherwise own stain at most
#: `unreadable_own_max` of all the cells read (panCK 0.23 -- but CD8a, a
#: working stain, 0.26: the stain flag tells them apart, not this) with
#: uncertain and artifact at least `unreadable_other_min` (panCK 0.68).
PARAMS = {"image_flag_min": 3, "unreadable_min_cells": 12, "artifact_look_share": 0.5,
          "artifact_look_min": 6, "unreadable_own_max": 0.25, "unreadable_other_min": 0.5}

#: The look flags that say a stain may be at fault (`schemas.ARTIFACTS` but
#: `segmentation`, an outline's fault, named on any crowded tissue).
STAIN_FLAGS = tuple(f for f in schemas.ARTIFACTS if f != "segmentation")

#: The look flags that describe the image rather than one channel: an edge
#: band, a fold, a bad region or tissue autofluorescence reach every marker
#: there. (Bleed-through and saturation are a channel's; uneven staining and
#: segmentation are named on most crowded tissue.)
IMAGE_FLAGS = ("edge", "tissue_fold", "image_quality", "autofluorescence")

#: The marker verdicts a `replan` answer may give, and the image verdicts.
MARKER_VERDICTS = ("absent", "bleed_through", "technical_failure", "keep_for_review")
IMAGE_VERDICTS = ("image_artifact", "not_image_level")

#: States a suspected-absent marker may be in when it is put to the agent:
#: closed for review, not yet called empty or failed.
_REVIEW = ("manual_review_recommended", "insufficient_information")

#: A unit held for the `replan` answer: not terminal, so the run is not
#: decided (and nothing leans on it) until the question is answered; the
#: state it came from is kept in `replan_from`.
HELD = "awaiting_replan"


def state(record) -> dict:
    """The session's re-planning record, created on first use."""
    rp = record.setdefault("replan", {})
    rp.setdefault("pending", {"markers": {}, "images": {}})
    rp.setdefault("asked", [])
    rp.setdefault("image_asked", [])
    rp.setdefault("failed", {})
    rp.setdefault("rounds", [])
    return rp


def pending(record) -> bool:
    """Whether a `replan` packet is waiting to go out."""
    rp = record.get("replan") or {}
    p = rp.get("pending") or {}
    return bool(p.get("markers") or p.get("images"))


def enabled(engine) -> bool:
    return bool(engine.options["replan"])


def _key(unit):
    return f"{unit['project']}::{unit['marker']}"


# -- after a close ---------------------------------------------------------------


def after_close(engine, unit):
    """Run once a unit closed (`Engine._close_tail`): the deterministic part,
    and whatever the agent must be asked queued for the next `replan`
    packet."""
    if not enabled(engine):
        return
    rp = state(engine.record)
    grade, _why = hierarchy.grade(unit)
    if grade == "failed":
        key = _key(unit)
        if unit.get("state") in _REVIEW:
            screen_at_close(engine, unit)
        rp["failed"][key] = suspicion(unit) or {"why": unit.get("state")}
        reorder(engine)
        demote_led(engine, unit)
        why = suspicion(unit)
        if why and key not in rp["asked"] and unit.get("state") in _REVIEW:
            queue(engine, unit, why)
    image_patterns(engine, unit["project"])


def after_accept(engine, unit):
    """An accepted unit's looks may complete an image's pattern -- or fall
    under one the agent already called the image's."""
    if not enabled(engine):
        return
    finding = (engine.record.get("image_findings") or {}).get(unit["project"])
    if finding and set(unit.get("artifact_flags") or ()) & set(finding["flags"]):
        tag_image_finding(unit, finding)
    else:
        image_patterns(engine, unit["project"])
    rescreen(engine, unit)


def excluded_by(engine, unit) -> set:
    """The markers the panel's tree says should exclude `unit`'s positives
    (exclusive relations, stated or derived: `hierarchy.relations`)."""
    panel = engine.panel_for(unit["project"])
    return {r["marker"] for r in hierarchy.relations(panel, unit["marker"])
            if r["relation"] == "exclusive" and r["direction"] == "forward"}


def rescreen(engine, accepted):
    """A partner just gated: the lineage check once more for every marker
    it should exclude that already closed for review without it (a look
    that settled nothing can close a marker before its partner is gated:
    LSP11761 PD1, closed before SOX10, live 2026-10-11). Queued for the
    `replan` packet as at a close; each marker is asked once."""
    rp = state(engine.record)
    for other in engine.units_of(accepted["project"]):
        key = _key(other)
        if other is accepted or other.get("state") not in _REVIEW or key in rp["asked"] \
                or key in rp["pending"]["markers"] or other.get("lineage_contamination") \
                or accepted["marker"] not in excluded_by(engine, other):
            continue
        screen_at_close(engine, other)
        why = suspicion(other)
        if why:
            rp["failed"][key] = why
            queue(engine, other, why)


def queue(engine, unit, why):
    """Put a suspect marker to the planner -- after the whole-image check,
    for a lineage suspect no whole-image view has judged. With replan off
    nothing is queued: no `replan` packet goes out (`Engine._replan_due`),
    so a held unit would wait for ever; it carries on where it was."""
    if not enabled(engine):
        return
    if why["why"] == "lineage_contamination" and not unit.get("qc_done") \
            and not unit.get("whole_image") and check_whole_image(engine, unit):
        return
    state(engine.record)["pending"]["markers"][_key(unit)] = why
    hold(engine, unit)


def check_whole_image(engine, unit) -> bool:
    """Reopen a lineage suspect for one whole-image check (`qc_confirm`);
    `after_whole_image` routes its answer. False when there is no gate to
    show it at."""
    if unit.get("candidate") is None and not unit.get("seen"):
        return False
    c = unit["lineage_contamination"]
    unit["replan_from"] = unit["state"]
    unit["lineage_check"] = True
    unit["qc_reason"] = [f"{c['at_top']:.0%} of its brightest positives are {c['partner']}+, "
                         f"which should exclude them ({c['prevalence']:.0%} of all cells are)"]
    unit["state"] = "qc_confirm"
    engine.log(event="replan_whole_image", unit=_key(unit), partner=c["partner"])
    return True


def after_whole_image(engine, unit, verdict) -> bool:
    """Route a lineage suspect's whole-image answer: `no_positive_population`
    and `technical_failure` close it as for any marker (False: the caller
    closes); `real_signal` and `cannot_tell` put it to the planner with the
    verdict beside the numbers (True)."""
    unit.pop("lineage_check", None)
    unit["whole_image"] = verdict
    if verdict in ("no_positive_population", "technical_failure"):
        unit.pop("replan_from", None)
        return False
    unit["state"] = unit.pop("replan_from", "manual_review_recommended")
    if verdict == "real_signal" and unit["state"] == "awaiting_regression":
        # Still open (`Engine.lineage_by_eye`): the picture says the stain is
        # real, so the gate its looks reached stands, its confidence capped.
        unit["lineage_ack"] = True
        unit["lineage_seen_real"] = True
        engine.log(event="lineage_seen_real", unit=_key(unit))
        return True
    queue(engine, unit, suspicion(unit) or {"why": "lineage_contamination"})
    return True


def tag_image_finding(unit, finding):
    """Note an image finding on an accepted unit its looks flagged. The gate
    and its acceptance stand: the looks judged its cells and accepted it, and
    the finding is a recommendation (run QC, then gate again), not evidence
    against this gate. Closing such units for review turned good gates into
    review work in the live test (LSP11761 SOX10 and Ki67)."""
    if unit.get("image_finding"):
        return
    unit["image_finding"] = {"flags": finding["flags"], "recommend": finding["recommend"]}
    note = (f"its looks name an image artifact ({', '.join(finding['flags'])}) also "
            f"named on {len(finding['markers'])} markers; {finding['recommend']}")
    unit["reason"] = f"{unit['reason']}; {note}" if unit.get("reason") else note


def hold(engine, unit):
    """Hold a unit for the `replan` answer (`HELD`)."""
    if unit["state"] == HELD:
        return
    unit["replan_from"] = unit["state"]
    unit["state"] = HELD
    engine.log(event="replan_held", unit=_key(unit), was=unit["replan_from"])


def release(unit):
    """Back to the state a held unit came from."""
    if unit["state"] == HELD:
        unit["state"] = unit.pop("replan_from", "manual_review_recommended")


def screen_at_close(engine, unit):
    """The lineage check (`Engine.lineage_purity`) once more at the close,
    when the partners it needs may be gated by now: a look that went out
    before them, or a unit closed without the whole-image checks, is still
    caught here."""
    if unit.get("lineage_contamination") or unit.get("lineage_ack"):
        return
    try:
        rows = engine.lineage_purity(unit)
    except Exception as exc:
        engine.log(event="lineage_error", unit=_key(unit), error=str(exc)[:200])
        return
    contaminated = [r for r in rows if r["contaminated"]]
    if rows:
        unit["lineage"] = rows
    if contaminated:
        unit["lineage_contamination"] = max(contaminated, key=lambda r: r["at_top"])
        engine.log(event="lineage_contaminated", unit=_key(unit), at_close=True,
                   **unit["lineage_contamination"])


def suspicion(unit) -> dict | None:
    """Why a failed unit's population may not exist, or None: its brightest
    positives sit on an exclusive lineage, or the whole-image view called
    its positives an artifact while no look ever saw its own stain."""
    contamination = unit.get("lineage_contamination")
    if contamination and not unit.get("lineage_ack"):
        return {"why": "lineage_contamination", **contamination}
    if unit.get("artifact_verdict") and not unit.get("own_seen"):
        return {"why": "artifact"}
    return None


def reorder(engine):
    """Reorder the markers no image has looked at yet, from the panel
    without the failed markers, in the slots they hold."""
    from plexora.plugins.gating.server.autogate import context

    record = engine.record
    order = list(record.get("order") or [])
    if not order:
        return
    units = list(record["units"].values())
    started = {u["marker"] for u in units if (u.get("used") or {}).get("packets")
               or u["state"] in engine.TERMINAL}
    fresh = [m for m in order if m not in started]
    if len(fresh) < 2:
        return
    project = record["images"][0]
    panel = engine.panel_for(project)
    failed = {m for m, e in engine.ledger(project).items() if e["grade"] == "failed"}
    entries = panel.get("entries") or {}
    pruned = {**panel, "markers": [m for m in panel.get("markers") or entries if m not in failed],
              "entries": {m: {**e, "partners": [p for p in e.get("partners") or []
                                                if p.get("marker") not in failed]}
                          for m, e in entries.items() if m not in failed}}
    try:
        new, _basis = context.order(pruned)
    except Exception:  # the order is a guide; a failure keeps the old one
        return
    rank = {m: i for i, m in enumerate(new)}
    reordered = sorted(fresh, key=lambda m: (rank.get(m, len(rank)), order.index(m)))
    if reordered == fresh:
        return
    slots, fresh_set = iter(reordered), set(fresh)
    record["order"] = [next(slots) if m in fresh_set else m for m in order]
    engine.log(event="replan_order", moved=[m for m, n in zip(fresh, reordered) if m != n],
               failed=sorted(failed))


def demote_led(engine, failed_unit):
    """Accepted units whose scored start the failed gate led, and that were
    accepted where they started (no look walked them), are looked at again
    from a start scored without it: nothing but the failed gate placed them,
    and a look, not the failure, says where they go. Once per unit; a session
    that asks for no looks closes them for review instead. Open units past a
    look get their references refreshed."""
    marker = failed_unit["marker"]
    for other in engine.units_of(failed_unit["project"]):
        if other is failed_unit:
            continue
        if other["state"] in engine.ASKS and (other.get("used") or {}).get("packets"):
            leaned = {r["marker"] for r in other.get("reference_gates") or []}
            if marker in leaned:
                engine.references_ready(other)
            continue
        if other["state"] not in hierarchy.ACCEPTED or other.get("needs_review"):
            continue
        if (other.get("start") or {}).get("leading") != marker:
            continue
        start = (other.get("start") or {}).get("low")
        final = other.get("final")
        if start is None or final is None or abs(float(final) - float(start)) > 1e-6:
            continue                      # a look moved it: its own evidence placed it
        if int(engine.options["max_tier"]) >= 2 and not other.get("relooked"):
            other["relooked"] = marker
            other.pop("reference_gates", None)
            other["state"] = "awaiting_t2"
            engine.propose(other, rescore=True)
            engine.log(event="replan_relook", unit=_key(other), because=marker,
                       start=(other.get("start") or {}).get("low"))
            continue
        other["state"] = "replanning"     # not terminal, so the close below takes
        engine.close(other, "manual_review_recommended",
                     f"accepted where its scored start was, and {marker} led that start; "
                     f"{marker}'s gate failed, so nothing left places this one",
                     proposed=final)
        engine.log(event="replan_demoted", unit=_key(other), because=marker)


def image_patterns(engine, project):
    """Queue an image whose looks named the same image-level artifact on
    `image_flag_min` markers, unless QC already left those cells out or the
    agent was asked about that flag there."""
    rp = state(engine.record)
    units = engine.units_of(project)
    if any(((u.get("qc_exclusion") or {}).get("applied")) for u in units):
        return
    by_flag = {}
    for u in units:
        for flag in u.get("artifact_flags") or ():
            if flag in IMAGE_FLAGS:
                by_flag.setdefault(flag, []).append(u["marker"])
    asked = {tuple(x) for x in rp["image_asked"]}
    found = {flag: sorted(markers) for flag, markers in by_flag.items()
             if len(markers) >= PARAMS["image_flag_min"] and (project, flag) not in asked}
    if found:
        rp["pending"]["images"][project] = {"flags": found}
        flagged = {m for ms in found.values() for m in ms}
        for u in units:
            if u["marker"] in flagged and u["state"] in hierarchy.ACCEPTED:
                hold(engine, u)


def panel_repeats(engine) -> dict:
    """{marker: {why: [images]}} for markers that failed the same way in two
    or more images of the session (what the packet shows beside the rest)."""
    rp = state(engine.record)
    seen = {}
    for key, why in rp["failed"].items():
        project, marker = key.split("::", 1)
        seen.setdefault(marker, {}).setdefault((why or {}).get("why"), []).append(project)
    return {m: {w: sorted(p) for w, p in by.items() if len(p) >= 2}
            for m, by in seen.items() if any(len(p) >= 2 for p in by.values())}


# -- the packet ------------------------------------------------------------------


def packet(engine, units):
    """The `replan` packet: the markers and images queued, what each one's
    looks said, and what is left of the plan."""
    rp = state(engine.record)
    queued = rp["pending"]
    if not queued["markers"] and not queued["images"]:
        return None
    markers = []
    for key, why in queued["markers"].items():
        unit = engine.record["units"].get(key)
        if unit is None:
            continue
        context = unit.get("context") or {}
        dependents = sorted({u["marker"] for u in engine.units_of(unit["project"])
                             if unit["marker"] in {r["marker"] for r in
                                                   u.get("reference_gates") or []}
                             or (u.get("start") or {}).get("leading") == unit["marker"]})
        markers.append({
            "key": key, "marker": unit["marker"], "project": unit["project"],
            "state": unit.get("replan_from") or unit["state"], "reason": unit.get("reason"),
            "suspected": why,
            "role": context.get("role"), "lineage": context.get("lineage"),
            "look_notes": (unit.get("notes") or [])[-3:],
            "artifact_flags": unit.get("artifact_flags") or [],
            "own_stain_seen": bool(unit.get("own_seen")),
            "whole_image": unit.get("whole_image"),
            "gate": unit.get("final") if unit.get("final") is not None else unit.get("candidate"),
            "dependents": dependents})
    images = [{"project": project, "flags": entry["flags"],
               "qc": "no QC result: nothing was left out"}
              for project, entry in queued["images"].items()]
    left = sorted({u["marker"] for u in engine.record["units"].values()
                   if u["state"] not in engine.TERMINAL})
    question = (
        "Some markers failed in a way only biology can settle, or an image's looks keep "
        "naming the same artifact. For each marker: `absent` (the stain worked and this "
        "tissue has no positive population: the positives are artifact), `bleed_through` "
        "(a real population, contaminated by another channel's light: the gate is walked up "
        "by looks), `technical_failure` (the stain did not work), or `keep_for_review`. For "
        "each image: `image_artifact` (the artifact is the image's -- an edge band, a fold, "
        "tissue autofluorescence -- and reaches every marker there) or `not_image_level`. "
        "Your words route markers; they never move a gate. Say `keep_for_review` rather "
        "than guess.")
    out = {
        "question": question,
        "evidence": {"markers": markers, "images": images,
                     "panel_repeats": panel_repeats(engine), "not_decided_yet": left,
                     "round": len(rp["rounds"]) + 1,
                     "rule": "lineage_contamination: the share of the marker's brightest "
                             "positives that are positive for a marker that should exclude "
                             "them (at_top), beside that marker's share of all cells "
                             "(prevalence)"},
        "allowed": {"markers": [m["key"] for m in markers],
                    "marker_verdicts": list(MARKER_VERDICTS),
                    "images": [i["project"] for i in images],
                    "image_verdicts": list(IMAGE_VERDICTS)},
        "_image_meta": [],
        "_queued": {"markers": sorted(queued["markers"]), "images": sorted(queued["images"])},
    }
    return out, []


def apply(engine, packet_, answer):
    """Act on a `replan` answer, then clear what it settled. A close made
    here runs `after_close` again (the loop)."""
    from plexora.agent.errors import AgentError

    rp = state(engine.record)
    queued = packet_.get("_queued") or {"markers": [], "images": []}
    missing = [k for k in queued["markers"] if k not in answer.markers] + \
        [p for p in queued["images"] if p not in answer.images]
    if missing:
        raise AgentError("invalid_input", f"answer every marker and image; missing {missing}",
                         detail={"markers": queued["markers"], "images": queued["images"]})
    applied = {}
    for key in queued["markers"]:
        verdict = answer.markers[key]
        rp["pending"]["markers"].pop(key, None)
        rp["asked"].append(key)
        unit = engine.record["units"].get(key)
        if unit is None:
            continue
        unit["replan_verdict"] = verdict
        applied[key] = _apply_marker(engine, unit, verdict)
    for project in queued["images"]:
        verdict = answer.images[project]
        entry = rp["pending"]["images"].pop(project, None) or {"flags": {}}
        rp["image_asked"].extend([project, flag] for flag in entry["flags"])
        applied[project] = _apply_image(engine, project, entry["flags"], verdict)
    rp["rounds"].append({"markers": {k: answer.markers[k] for k in queued["markers"]},
                         "images": {p: answer.images[p] for p in queued["images"]},
                         "applied": applied})
    engine.log(event="replanned", round=len(rp["rounds"]), applied=applied)
    return {"applied": applied, "round": len(rp["rounds"]), "pending_again": pending(engine.record)}


def seen_failing(unit) -> bool:
    """Whether a picture stands behind calling the marker absent or failed:
    the whole-image view called its positives an artifact, or its
    whole-image check said empty or failed. The planner reads numbers and
    words; it never closes a marker on them alone."""
    return bool(unit.get("artifact_verdict")) or \
        unit.get("whole_image") in ("no_positive_population", "technical_failure")


def _apply_marker(engine, unit, verdict):
    if verdict in ("absent", "technical_failure") and not seen_failing(unit):
        engine.log(event="replan_close_refused", unit=_key(unit), verdict=verdict,
                   whole_image=unit.get("whole_image"))
        release(unit)
        return "kept_for_review"
    unit.pop("replan_from", None) if verdict in ("absent", "technical_failure") else None
    if verdict == "absent":
        unit["state"] = "replanning"
        engine.close(unit, "no_positive_population",
                     "re-planning: the positives are artifact and this tissue has no positive "
                     "population; the gate is put at the maximum")
        return "no_positive_population"
    if verdict == "technical_failure":
        unit["state"] = "replanning"
        engine.close(unit, "technically_failed", "re-planning: the stain did not work",
                     confidence="failed_qc")
        return "technically_failed"
    if verdict == "bleed_through" and not unit.get("replan_reopened") \
            and int(engine.options["max_tier"]) >= 4 and unit.get("candidate") is not None:
        # A real population under another channel's light: up the lattice,
        # once, to candidates the looks judge row by row (as a purity push).
        unit["replan_reopened"] = True
        unit["lineage_ack"] = True
        unit.pop("needs_review", None)
        unit.setdefault("code_moves", []).append("up:replan")
        unit["direction"] = "up"
        unit["magnitude"] = None
        unit["span_from"] = unit["candidate"]
        unit.pop("replan_from", None)
        unit["state"] = "awaiting_t4"
        return "reopened_up"
    release(unit)
    return "kept_for_review"


def _apply_image(engine, project, flags, verdict):
    if verdict != "image_artifact":
        for unit in engine.units_of(project):
            if unit.get("replan_from") in hierarchy.ACCEPTED:
                release(unit)
        return "not_image_level"
    markers = sorted({m for ms in flags.values() for m in ms})
    finding = {"flags": sorted(flags), "markers": markers,
               "recommend": "run QC on this image, then gate these markers again"}
    engine.record.setdefault("image_findings", {})[project] = finding
    tagged = []
    for unit in engine.units_of(project):
        held_accepted = unit["state"] == HELD and unit.get("replan_from") in hierarchy.ACCEPTED
        if held_accepted:
            release(unit)
        if unit["marker"] in markers and unit["state"] in hierarchy.ACCEPTED:
            tag_image_finding(unit, finding)
            tagged.append(unit["marker"])
    return {"image_finding": finding, "tagged": tagged}


# -- a stain the looks could not read ----------------------------------------------


def reads_of(unit) -> dict:
    """{cells, own, not_own, uncertain, artifact, looks, worst_artifact} of
    the unit's cell-panel looks: the valid looks' numbered cells, sentinels
    left out (`hypotheses.evidence.counts` reads them the same way);
    `worst_artifact` is the largest share of one look's cells called
    `artifact`, over looks of at least `artifact_look_min` cells."""
    out = {"cells": 0, "own": 0, "not_own": 0, "uncertain": 0, "artifact": 0, "looks": 0,
           "worst_artifact": 0.0}
    for obs in (unit.get("hypotheses") or {}).get("observations") or ():
        if not obs.get("valid", True):
            continue
        labels = [c.get("label") for c in obs.get("cells") or () if not c.get("sentinel")]
        labels = [x for x in labels if x]
        if not labels:
            continue
        out["looks"] += 1
        out["cells"] += len(labels)
        for label, key in (("own_stain", "own"), ("not_own_stain", "not_own"),
                           ("uncertain", "uncertain"), ("artifact", "artifact")):
            out[key] += labels.count(label)
        if len(labels) >= PARAMS["artifact_look_min"]:
            out["worst_artifact"] = max(out["worst_artifact"],
                                        labels.count("artifact") / len(labels))
    return out


def unreadable(unit) -> dict | None:
    """Why a unit's looks read its stain as almost no cell's own -- what a
    failed stain looks like through a cell panel -- or None. Either a look
    called at least `artifact_look_share` of its cells `artifact` (NKG2A,
    lsp35766_p193 2026-11-22: 14 of 19, with autofluorescence and uneven
    staining named), or, over all its looks, `own_stain` is at most
    `unreadable_own_max` of the cells, `uncertain` and `artifact` together
    at least `unreadable_other_min`, and a look named a stain artifact
    (`STAIN_FLAGS`: panCK there, 23 % own, 68 % uncertain, uneven staining).
    The flag is what tells panCK from CD8a on that run (26 % own, 68 %
    uncertain, the crowding of a T-cell zone and `segmentation` named): the
    counts alone do not, and nothing closes on counts -- this only asks for
    the picture."""
    reads = reads_of(unit)
    n = reads["cells"]
    if n < PARAMS["unreadable_min_cells"]:
        return None
    own, other = reads["own"] / n, (reads["uncertain"] + reads["artifact"]) / n
    flags = sorted(set(unit.get("artifact_flags") or ()) & set(STAIN_FLAGS))
    words = (f"its looks read {reads['own']} of {n} cells as carrying their own stain "
             f"({reads['artifact']} artifact, {reads['uncertain']} uncertain)"
             + (f"; they named {', '.join(flags)}" if flags else ""))
    if reads["worst_artifact"] >= PARAMS["artifact_look_share"]:
        return {"why": "artifact_reads", "words": words, "own_share": round(own, 3),
                "other_share": round(other, 3), "worst_artifact": round(reads["worst_artifact"], 3),
                "flags": flags, "reads": reads}
    if own <= PARAMS["unreadable_own_max"] and other >= PARAMS["unreadable_other_min"] and flags:
        return {"why": "unread_stain", "words": words, "own_share": round(own, 3),
                "other_share": round(other, 3), "worst_artifact": round(reads["worst_artifact"], 3),
                "flags": flags, "reads": reads}
    return None


def check_unreadable(engine, unit, state, reason, proposed) -> bool:
    """Before a unit closes for review (`state`) on looks that could not read
    its stain (`unreadable`): one whole-image check first (`qc_confirm`, the
    `image_inspection` task), the close it was about to make kept for its
    answer (`unit["unreadable_check"]`). `technical_failure` and
    `no_positive_population` close it through `transitions.apply_qc` (the
    absence regions too, when the session checks absence); `real_signal` and
    `cannot_tell` make that close (`after_unreadable`). Once per marker; not
    for a unit a whole-image view already judged, nor one the lineage check
    routes (`check_whole_image`). False: close as asked."""
    if state not in _REVIEW or unit.get("unreadable_checked") or unit.get("qc_done") \
            or unit.get("whole_image") or unit.get("artifact_verdict") \
            or unit.get("lineage_contamination") \
            or (unit.get("candidate") is None and not unit.get("seen")):
        return False
    why = unreadable(unit)
    if why is None:
        return False
    unit["unreadable_checked"] = True
    unit["unreadable_check"] = {"state": state, "reason": reason,
                                "proposed": None if proposed is None else float(proposed),
                                **{k: v for k, v in why.items() if k != "reads"}}
    unit["qc_reason"] = [why["words"]]
    unit["state"] = "qc_confirm"
    engine.log(event="unreadable_whole_image", unit=_key(unit), why=why["why"],
               own_share=why["own_share"], other_share=why["other_share"],
               worst_artifact=why["worst_artifact"], flags=why["flags"])
    return True


def after_unreadable(engine, unit, verdict) -> bool:
    """Route the whole-image answer for a stain its looks could not read
    (`check_unreadable`). `technical_failure` / `no_positive_population`:
    False, the caller closes it as for any marker (the record stays, with
    the verdict, so an absence check that finds clear positives still ends
    in the kept close: `keep_unreadable`). Anything else: the close the
    unit was about to make, made now (True)."""
    unit["whole_image"] = verdict
    unit["unreadable_check"]["verdict"] = verdict
    if verdict in ("no_positive_population", "technical_failure"):
        return False
    keep_unreadable(engine, unit, verdict)
    return True


def keep_unreadable(engine, unit, verdict):
    """The close `check_unreadable` held back, made: the stain is real (or
    the picture could not tell), so the marker is written for review where
    its looks left it, as it would have been."""
    held = unit.pop("unreadable_check")
    said = {"real_signal": "called its stain real",
            "cannot_tell": "could not tell whether its stain worked"}.get(
        verdict, f"answered {verdict}")
    engine.log(event="unreadable_kept", unit=_key(unit), verdict=verdict)
    engine.close(unit, held["state"],
                 f"{held['reason']}; a whole-image check of its unread stain {said}",
                 proposed=held.get("proposed"))
