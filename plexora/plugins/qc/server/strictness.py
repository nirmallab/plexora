"""Strictness: thresholds only, applied to stored measurements and decisions.

A preset (Lenient, Standard, Strict, or a Custom table inside their band)
changes no measurement and no agent answer -- it changes which of them become
an exclusion. Everything here is a pure function of what a session stored, so
changing the preset re-derives every region's action and every cell's call
without a packet, and Strict excludes everything Standard does, which
excludes everything Lenient does (`schemas._assert_monotonic`, and the
property test).
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas

ACTIONS = schemas.ACTIONS
#: Keys of the retired cell modules (counterstain, table area, cycle
#: stability, channel outliers). A custom table saved before they were
#: retired still holds them; they are dropped, not refused.
RETIRED_KEYS = frozenset({"counterstain.low_k", "counterstain.high_k", "area.k",
                          "area.ratio_low", "area.ratio_high", "area.solidity_min",
                          "area.seg_conf_min", "cycle.abs_floor", "cycle.k", "outlier.k"})


def thresholds(preset="standard", custom=None) -> dict:
    """The threshold table of a preset, or of a custom table (standard's numbers
    where it is silent). A custom value outside [lenient, strict] is refused,
    which keeps the monotonicity claim true of everything accepted."""
    if preset not in schemas.STRICTNESS:
        raise AgentError("invalid_input", f"unknown strictness {preset!r}",
                         detail={"allowed": list(schemas.STRICTNESS)})
    if preset != "custom":
        if custom:
            raise AgentError("invalid_input", "custom_thresholds need strictness='custom'")
        return dict(schemas.STRICTNESS_PRESETS[preset])
    table = dict(schemas.STRICTNESS_PRESETS["standard"])
    custom = {k: v for k, v in (custom or {}).items() if k not in RETIRED_KEYS}
    unknown = sorted(set(custom or {}) - set(schemas.STRICTNESS_KEYS))
    if unknown:
        raise AgentError("invalid_input", f"unknown strictness keys {unknown}",
                         detail={"keys": sorted(schemas.STRICTNESS_KEYS)})
    for key, value in (custom or {}).items():
        low = schemas.STRICTNESS_PRESETS["lenient"][key]
        high = schemas.STRICTNESS_PRESETS["strict"][key]
        bottom, top = min(low, high), max(low, high)
        if not bottom <= float(value) <= top:
            raise AgentError("invalid_input", f"{key}={value} is outside the lenient..strict "
                             f"band [{bottom}, {top}]", detail={"key": key})
        table[key] = float(value)
    return table


def max_action(*actions):
    return max((a for a in actions if a), key=ACTIONS.index, default="ignore")


def min_action(*actions):
    return min((a for a in actions if a), key=ACTIONS.index, default="ignore")


def measured_support(unit):
    """What measured a region, beside the agent's words: `check` (an image
    check scored it above its bar), `detector` (a scan detector raised it),
    `traced` (the pixels were traced inside its outline), or None -- a region
    the agent alone put there (a grid, a region drawn without a detector)
    that no trace bore out. Stored as `measurement["support"]`;
    `decide_artifact` excludes on the agent's word alone never."""
    if unit.get("origin") == "check":
        return "check"
    if (unit.get("refinement") or {}).get("status") == "refined":
        return "traced"
    if unit.get("detector") not in (None, "consolidated"):
        return "detector"
    return None


def decide_artifact(decision, measurement, table) -> dict:
    """{action, reason} for one confirmed candidate.

    `decision` is the agent's (strictness-free): class, severity word,
    confidence word, exclude_recommended. `measurement` carries the region's
    `tissue_fraction` (its envelope: what the detectors measured and the
    agent judged) and `refined_fraction` (what the traced outline removes).
    Rules, in order:

    - a region the agent was not sure enough about (`uncertain_manual_review`)
      is a warning, never an exclusion -- and only noted when it is a large
      share of the tissue (`manual_review_action`);
    - exclude when severity, confidence and area all reach the preset's floor;
    - warn when severity reaches the warn floor; otherwise the region is noted;
    - the agent saying `exclude_recommended: false` caps it at warn;
    - a region nothing measured (`measurement["support"]` present and None,
      `measured_support`) is capped at warn: the agent's own severity and
      confidence words never exclude cells on their own;
    - a region removing over `ENGINE["large_region_fraction"]` of the tissue
      (its trace's share, when traced) excludes only under a preset that
      allows it (Strict), or an approval.

    The area floor stays on the envelope: an aggregate field traced down to
    its specks is still the size of artifact the agent judged.
    """
    decision = decision or {}
    measurement = measurement or {}
    klass = decision.get("artifact_class") or "other_technical"
    if klass == "uncertain_manual_review" or decision.get("manual_review"):
        return manual_review_action(measurement)
    severity = schemas.SEVERITY_RANK.get(decision.get("severity") or "moderate", 1)
    confidence = schemas.AI_CONFIDENCE.get(decision.get("confidence") or "unsure", 0.3)
    fraction = measurement.get("tissue_fraction")
    reasons = []
    if severity >= table["artifact.exclude_min_severity"] \
            and confidence >= table["artifact.exclude_min_confidence"] \
            and (fraction is None or fraction >= table["artifact.min_area_fraction_exclude"]):
        action = "exclude"
    elif severity >= table["artifact.warn_min_severity"]:
        action = "warn"
        reasons.append("below the exclusion floor")
    else:
        action = "ignore"
        reasons.append("minor")
    if action == "exclude" and decision.get("exclude_recommended") is False:
        action = "warn"
        reasons.append("the agent did not recommend excluding it")
    if action == "exclude" and "support" in measurement and measurement["support"] is None:
        action = "warn"
        reasons.append("no detector, check or trace measured it: excluded only on approval")
    removed = measurement.get("refined_fraction")
    removed = fraction if removed is None else removed
    if action == "exclude" and removed is not None \
            and removed >= schemas.ENGINE["large_region_fraction"] \
            and not table.get("artifact.large_region_exclude"):
        action = "warn"
        reasons.append("removes a large share of the tissue: excluded only on approval")
    return {"action": action, "reason": "; ".join(reasons) or "meets the exclusion floor"}


def manual_review_action(measurement) -> dict:
    """{action, reason} of a region left for a person: a warning, never an
    exclusion -- but noted (drawn in Needs review, its cells not warned)
    when its envelope is over `ENGINE["manual_review_warn_fraction"]` of
    the tissue. An unsettled question that size is about a channel or a
    detector, not about cells: one tile-seam envelope over the epithelium
    of two sections warned 40 % of an image's cells. The same under every
    preset, so the presets stay nested."""
    fraction = (measurement or {}).get("tissue_fraction")
    if fraction is not None and float(fraction) > schemas.ENGINE["manual_review_warn_fraction"]:
        return {"action": "ignore", "reason": "manual review over a large share of the tissue: "
                                              "drawn for a person, its cells not warned"}
    return {"action": "warn", "reason": "manual review: never excluded automatically"}


def action_for(candidate, table) -> str:
    """The action a stored candidate takes under `table`, after the user's own
    state (approved action pinned; a user-drawn region excludes)."""
    user = candidate.get("user_state") or {}
    if user.get("approved_action"):
        return user["approved_action"]
    if candidate.get("class") == schemas.BACKGROUND_CLASS:
        # The background is an annotation under every preset: only the
        # user's own approval or renaming (pinned above) removes its cells.
        return "note"
    if candidate.get("created_by") == "user" or user.get("created_by") == "user":
        return "exclude"
    members = [f for f in candidate.get("findings") or []
               if f.get("primary") and f.get("ai_decision") is not None]
    if members:
        # A consolidated ROI (one per category and action): its findings'
        # own decisions, the strongest action of them -- its own decision is
        # only its first finding's, under the category's class.
        return max_action(*(action_for({k: f.get(k) for k in (
            "state", "ai_decision", "measurement")}, table) for f in members))
    if candidate.get("state") == "manual_review_recommended":
        return manual_review_action(candidate.get("measurement"))["action"]
    return decide_artifact(candidate.get("ai_decision"), candidate.get("measurement"),
                           table)["action"]


def actions_by_preset(candidate) -> dict:
    return {preset: action_for(candidate, thresholds(preset))
            for preset in ("lenient", "standard", "strict")}
