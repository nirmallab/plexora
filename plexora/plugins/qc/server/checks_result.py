"""What each image check contributed to a result: its bar and where it came from.

`result["checks"][check][channel]` (the segmentation check under `calls`)
holds one check unit's record: the fingerprint and version of the scores,
the automatic threshold, the threshold used and its source (`auto`, or
`agent_refined` after a look moved it), the steps it moved, the
distribution it was judged against, every round's row verdicts, the
whole-tissue verdict, the packets and sheets, and what became of its
regions (decided by the review, sent to a confirm, dismissed as normal,
left for manual review), and the agent's notes on its looks. Written when the unit is scored and again when it
closes, so a report or an export reads it without the session.
"""

from __future__ import annotations

KEEP = ("check", "channel", "reference", "fingerprint", "auto_threshold", "threshold",
        "threshold_source", "offset_steps", "step", "distribution", "at_auto", "global",
        "cell_um", "field_stats", "segqc", "reused", "state", "reason", "strata_verdicts",
        "whole_tissue", "packets", "artifacts", "regions", "n_regions", "flagged_pct",
        "denominator", "at_threshold", "confirm_groups", "one_cycle", "manual_overflow",
        "notes")


def entry_of(unit) -> dict:
    from plexora.plugins.qc.server import checks_bulk, score_fields

    out = {k: unit.get(k) for k in KEEP if unit.get(k) is not None}
    out["versions"] = {"checks": checks_bulk.VERSION, "score_fields": score_fields.VERSION}
    dist = out.get("distribution")
    if isinstance(dist, dict):
        out["distribution"] = {k: dist.get(k) for k in ("n", "median", "mad", "quantiles",
                                                        "histogram")}
    return out


def key_of(unit):
    if unit["check"] == "segmentation":
        return "calls"
    if unit["check"] == "artifacts":
        return unit.get("category") or "artifacts"
    return unit.get("channel") or "image"


def record(engine, unit):
    """Store one check unit's record in the session's result."""
    from plexora.plugins.qc.server import results

    project = unit["project"]
    with results.lock(project):
        document = results.load(project)
        result = results.get_result(project, document, engine.record["result_id"])
        if result is None:
            return
        result.setdefault("checks", {}).setdefault(unit["check"], {})[key_of(unit)] = \
            entry_of(unit)
        results.put_result(document, result)
        results.save(project, document)


def record_all(engine):
    for unit in engine.units_of("check"):
        record(engine, unit)
