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


def channel_effects(engine) -> dict:
    """{affected, failed, reached}: what the session's settled findings say of
    each channel -- the one rule the channel statuses follow, during the
    session (`engine.settle_channels`) and when it closes.

    - A region scoped to some channels (`channel`, `channels`) flags them
      (`affected`).
    - A region that names the tissue, not a stain (`WHOLE_SCOPES`, or every
      channel of the image) reaches every channel it was drawn on without
      saying anything about any one of them (`reached`), or one fold flags
      forty clean channels. Only a detector's region still flags the one
      channel it was audited and confirmed on; an image check's region was
      never judged on an audit row, so it flags none.
    - Segmentation flags nothing: a mask problem is no channel's.
    - A confirmed failed-channel verdict fails its channels (`failed`)."""
    affected, failed, reached = set(), set(), {}
    every = {u["id"] for u in engine.units_of("channel")}
    for unit in engine.units_of("candidate"):
        if unit["state"] not in schemas.CONFIRMED_STATES + ("manual_review_recommended",):
            continue
        if unit["state"] == "confirmed_noted":
            continue
        klass = unit.get("class") or unit.get("class_hint") or ""
        if klass == "segmentation_error" or unit.get("detector") == "segmentation":
            continue
        named = set(unit.get("channels") or [])
        scope = (unit.get("decision") or {}).get("scope") or unit.get("scope_hint")
        lead = unit.get("audit_channel")
        if scope in WHOLE_SCOPES or (every and named >= every and len(every) > 1):
            for name in named | ({lead} - {None}):
                reached.setdefault(name, []).append(unit["id"])
            channels = {lead} - {None} if unit.get("origin") != "check" else set()
        else:
            channels = named | ({lead} - {None})
        affected |= channels
        if klass == "empty_or_failed_channel" and unit["state"] == "confirmed_exclude":
            failed |= channels
    return {"affected": affected, "failed": failed, "reached": reached}


def _settle_channel_statuses(engine):
    """Each channel's final status from `channel_effects`: failed, flagged
    (a clean or still-open channel a region is scoped to), and the
    whole-tissue regions that reach it listed (`reached_by`)."""
    effects = channel_effects(engine)
    affected, failed, reached = effects["affected"], effects["failed"], effects["reached"]
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
    if action in ("close", "commit"):
        # One ROI per place: the session's overlapping or same-class findings
        # consolidated before the cells are counted (`consolidate`).
        from plexora.plugins.qc.server import consolidate

        try:
            consolidate.run(engine, action)
        except Exception as exc:  # noqa: BLE001 -- the findings as written still stand
            engine.log(event="consolidation_failed", error=str(exc))
            record.setdefault("warnings", []).append(f"consolidation failed: {exc}")
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
            if unit.get("audit_note"):
                # A staining verdict settled on the audit row: never a region.
                channel["audit_note"] = unit["audit_note"]
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
        result["planning_notes"] = list(record.get("planning_notes") or [])
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
            calls.write_for_active(call, project)
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
