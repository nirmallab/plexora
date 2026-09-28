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


def decide_artifact(decision, measurement, table) -> dict:
    """{action, reason} for one confirmed candidate.

    `decision` is the agent's (strictness-free): class, severity word,
    confidence word, exclude_recommended. `measurement` carries the region's
    `tissue_fraction`. Rules, in order:

    - a region the agent was not sure enough about (`uncertain_manual_review`)
      is a warning, never an exclusion;
    - exclude when severity, confidence and area all reach the preset's floor;
    - warn when severity reaches the warn floor; otherwise the region is noted;
    - the agent saying `exclude_recommended: false` caps it at warn;
    - a region over `ENGINE["large_region_fraction"]` of the tissue excludes
      only under a preset that allows it (Strict), or an approval.
    """
    decision = decision or {}
    measurement = measurement or {}
    klass = decision.get("artifact_class") or "other_technical"
    if klass == "uncertain_manual_review" or decision.get("manual_review"):
        return {"action": "warn", "reason": "manual review: never excluded automatically"}
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
    if action == "exclude" and fraction is not None \
            and fraction >= schemas.ENGINE["large_region_fraction"] \
            and not table.get("artifact.large_region_exclude"):
        action = "warn"
        reasons.append("covers a large share of the tissue: excluded only on approval")
    return {"action": action, "reason": "; ".join(reasons) or "meets the exclusion floor"}


def action_for(candidate, table) -> str:
    """The action a stored candidate takes under `table`, after the user's own
    state (approved action pinned; a user-drawn region excludes)."""
    user = candidate.get("user_state") or {}
    if user.get("approved_action"):
        return user["approved_action"]
    if candidate.get("created_by") == "user" or user.get("created_by") == "user":
        return "exclude"
    if candidate.get("state") == "manual_review_recommended":
        return "warn"
    return decide_artifact(candidate.get("ai_decision"), candidate.get("measurement"),
                           table)["action"]


def actions_by_preset(candidate) -> dict:
    return {preset: action_for(candidate, thresholds(preset))
            for preset in ("lenient", "standard", "strict")}
