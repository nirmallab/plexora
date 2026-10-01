"""What a finished QC session leaves behind: its result, active in the store.

The session folder is swept eventually; the result is what lasts. On `close`
(or `commit`, or a `cancel` after regions were written) the session's result
takes each channel's final status, the receipts, the summary, and becomes the
project's active QC result -- and, when the project has a cell table, every
cell's call is written beside it (`cells.calls`). A rolled-back session's
result is kept, marked, and never activated.
"""

from __future__ import annotations

from plexora.plugins.qc.server import results, schemas

CHANNEL_STATUS = {"clean": "clean", "flagged": "flagged", "failed_channel": "failed",
                  "manual_review_recommended": "manual_review",
                  "skipped_no_image": "not_scanned", "skipped_brightfield": "not_scanned"}


#: Scopes that name the tissue, not a stain: such a region reaches every
#: channel without saying anything about any one of them.
WHOLE_SCOPES = ("all_channels", "cycle", "cycles")


def _settle_channel_statuses(engine):
    """A channel is flagged by every confirmed region scoped to it -- not only
    the one audited on its row -- and failed by a confirmed failed-channel
    region. A physical region over the whole tissue (a fold, debris, a whole
    cycle) leaves the audit's verdict alone: the channels it reaches are
    listed on it (`reached_by`), not flagged, or one fold flags forty clean
    channels."""
    affected, failed, reached = set(), set(), {}
    for unit in engine.units_of("candidate"):
        if unit["state"] not in schemas.CONFIRMED_STATES + ("manual_review_recommended",):
            continue
        if unit["state"] == "confirmed_noted":
            continue
        channels = set(unit.get("channels") or []) | {unit.get("audit_channel")}
        scope = (unit.get("decision") or {}).get("scope") or unit.get("scope_hint")
        if scope in WHOLE_SCOPES:
            for name in channels:
                reached.setdefault(name, []).append(unit["id"])
            channels = {unit.get("audit_channel")} - {None}
        affected |= channels
        if (unit.get("class") or "") == "empty_or_failed_channel" and \
                unit["state"] == "confirmed_exclude":
            failed |= channels
    for unit in engine.units_of("channel"):
        if unit["state"] in ("skipped_no_image", "skipped_brightfield"):
            continue
        if unit["id"] in failed:
            unit["state"] = "failed_channel"
        elif unit["id"] in affected and unit["state"] in ("clean", "awaiting_candidates"):
            unit["state"] = "flagged"
            unit["reason"] = "a confirmed region reaches this channel"
        if reached.get(unit["id"]):
            unit["reached_by"] = sorted(reached[unit["id"]])


def finish_result(call, engine, action) -> dict:
    from plexora.plugins.qc.server.engine import summary_of

    record = engine.record
    project = engine.project
    written = any(u.get("roi_id") for u in engine.units_of("candidate"))
    activate = action in ("close", "commit") or (action == "cancel" and written)
    from plexora.plugins.qc.server import checks_result

    # Each image check's bar, its source and what became of its regions.
    checks_result.record_all(engine)
    with results.lock(project):
        document = results.load(project)
        result = results.get_result(project, document, record["result_id"])
        if result is None:
            result = results.new_result(project, session_id=engine.id)
            result["result_id"] = record["result_id"]
        units = {u["id"]: u for u in engine.units_of("channel")}
        _settle_channel_statuses(engine)
        for channel in result.get("channels") or []:
            unit = units.get(channel["name"])
            if unit is None:
                continue
            channel["status"] = CHANNEL_STATUS.get(unit["state"], "not_reviewed")
            channel["audit"] = unit.get("audit")
            channel["reason"] = unit.get("reason")
            if unit.get("reached_by"):
                channel["reached_by"] = unit["reached_by"]
        for unit in engine.units_of("candidate"):
            entry = (result.get("candidates") or {}).get(unit["id"])
            if entry is not None:
                entry["state"] = unit["state"]
            elif unit["state"] in ("dismissed", "merged", "user_kept"):
                result.setdefault("dismissed", []).append(
                    {"id": unit["id"], "class_hint": unit.get("class_hint"),
                     "channels": unit.get("channels"), "state": unit["state"],
                     "reason": unit.get("reason"), "detector": unit.get("detector")})
        final = next(iter(engine.units_of("final")), None)
        if final is not None:
            result["final_review"] = {"state": final["state"], "review": final.get("review")}
        result["finished_at"] = results.now_iso()
        result["session_state"] = record["state"]
        result["receipts"] = list(record.get("receipts") or [])
        result["summary"] = summary_of(record)
        result["mode"] = record["options"]["mode"]
        result["strictness"] = {"preset": (record.get("strictness") or {}).get("preset"),
                                "thresholds": (record.get("strictness") or {}).get("custom")}
        if action == "rollback":
            result["rolled_back"] = True
        results.put_result(document, result, activate=activate)
        if activate:
            document["strictness"] = dict(result["strictness"])
        results.save(project, document)
    if activate and record.get("has_table"):
        from plexora.plugins.qc.server.cells import calls

        try:
            calls.write_for_active(call, project, session_id=engine.id)
            # The derivation saved its own copy of the result: report that one,
            # not the one read before the cells were counted.
            fresh = results.get_result(project, results.load(project), record["result_id"])
            if fresh is not None:
                result = fresh
        except Exception as exc:  # a failed cell write never loses the regions
            with results.lock(project):
                document = results.load(project)
                active = results.active(document)
                if active is not None:
                    active.setdefault("warnings", []).append(
                        {"code": "cells_not_written", "message": str(exc)})
                    results.save(project, document)
    return results.summary(result) | {"active": activate,
                                      "result_state": schemas.RESULT_VERSION}
