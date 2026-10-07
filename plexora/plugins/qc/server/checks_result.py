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
        "notes",
        # The visual pass: what the agent wrote and what it left for a person.
        "written", "left", "status", "channels_used", "max_regions")


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


# -- the registration table ------------------------------------------------------------

REGISTRATION = "cross_cycle_registration_error"
LOST = "cycle_specific_tissue_loss"


def registration_table(result) -> list:
    """One row per DNA channel compared with the reference: how far its cycle
    is shifted as a whole, whether that is a verdict on the channel, the local
    places out of register (and the nuclei they hold), the tissue it lost,
    and the markers and cells it costs -- the registration story of a result
    in one table (the report's, `get_qc_results`' `registration`)."""
    checks = ((result or {}).get("checks") or {}).get("registration") or {}
    candidates = (result or {}).get("candidates") or {}
    marker_flags = ((result or {}).get("cells") or {}).get("marker_flags") or {}
    rows = []
    for channel, entry in checks.items():
        stats = entry.get("field_stats") or {}
        shift = stats.get("global_shift_um")
        mine = [c for c in candidates.values()
                if c.get("check_unit") == f"registration:{channel}"]
        verdict = next((c for c in mine if c.get("channel_level")
                        and c.get("action") in ("exclude", "warn")), None)
        local = [c for c in mine if c.get("class") == REGISTRATION and not c.get("channel_level")
                 and not (c.get("user_state") or {}).get("deleted")]
        lost = [c for c in mine if c.get("class") == LOST]
        nuclei = {"displaced": 0, "lost": 0, "aligned": 0}
        for c in local + lost:
            for k, v in ((c.get("refinement") or {}).get("nuclei") or {}).items():
                nuclei[k] = nuclei.get(k, 0) + int(v or 0)
        markers = sorted({m for c in ([verdict] if verdict else []) + local
                          for m in c.get("channels") or []})
        cells = 0
        for marker in markers:
            counts = (marker_flags.get(marker) or {}).get(f"region:{REGISTRATION}") or {}
            cells = max(cells, sum(int(v or 0) for v in counts.values()))
        if entry.get("state") == "skipped_not_applicable":
            status = "skipped"
        elif verdict is not None:
            status = "shifted"
        elif local:
            status = "local"
        else:
            status = "aligned"
        rows.append({"channel": channel, "reference": entry.get("reference"),
                     "status": status, "reason": entry.get("reason")
                     if status == "skipped" else None,
                     "global_shift_um": shift,
                     "global_shift_px": stats.get("global_shift_px"),
                     "correctable": status == "shifted",
                     "local_regions": len(local), "tissue_loss_regions": len(lost),
                     "nuclei": nuclei, "markers": markers, "cells_affected": cells,
                     "action": (verdict or {}).get("action")
                     or (local[0].get("action") if local else None)})
    return rows
