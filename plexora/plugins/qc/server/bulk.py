"""The deterministic pass of a QC session: everything code can do, first.

Runs as a job (`qc_session_bulk`), ahead of the agent: the display calibration
(so every sheet draws each channel the same way), the pyramid scan (cached by
fingerprint, so a rerun reads it back in a second), every detector, the
candidates merged and ranked, and one unit per channel and per candidate.
Nothing is settled from numbers alone: every channel still goes on an audit
sheet. When the project has a cell table the cell modules measure their
columns too, so the agent's looks at the cells can start when the regions are
done. Heavy work runs outside the session lock; results are merged in under
it.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import schemas
from plexora.plugins.qc.server.engine import engine_for, store, unit_key


def calibrate_image(call, project, session_id):
    from plexora.agent.evidence import calibration

    try:
        record, changed = calibration.calibrate(call.session, project)
    except AgentError as exc:
        return {"project": project, "error": exc.message}
    call.audit.append({"status": "ok", "operation_id": f"{call.operation_id}.cal",
                       "capability": "qc.calibrate_display", "project": project,
                       "arguments": {"project": project, "qc_session": session_id},
                       "receipt": {"changed": changed, "persistent_state": "plugin_store:display",
                                   "reversible": False}})
    return {"project": project, "revision": calibration.revision(project), "changed": changed}


def _check_stopped(session_id):
    from plexora.agent.jobs import JobCancelled

    if store().control(session_id).get("stopped"):
        raise JobCancelled("stopped from the viewer")


def candidate_unit(candidate, project, scan):
    from plexora.plugins.qc.server.scan import bbox_fullres

    box = bbox_fullres(scan.grid, candidate.mask) or (0, 0, *scan.grid["image_size"])
    lead = candidate.channels[0] if candidate.channels else None
    return {"type": "candidate", "project": project, "id": candidate.id,
            "channel": lead, "audit_channel": lead, "channels": list(candidate.channels),
            "cycles": [int(c) for c in candidate.cycles if c],
            "detector": candidate.detector, "detector_version": candidate.detector_version,
            "class_hint": candidate.class_hint, "alternatives": list(candidate.alternatives),
            "scope_hint": candidate.scope_hint, "score": float(candidate.score),
            "severity": float(candidate.severity), "metrics": dict(candidate.metrics),
            "primary_metric": candidate.primary_metric,
            "merged_from": list(candidate.merged_from), "mask": cand.encode_mask(candidate.mask),
            "bbox": [float(v) for v in box], "peak": cand.peak_of(candidate, scan),
            "measurement": {"tissue_fraction": cand.area_fraction(candidate.mask, scan),
                            "cells": int(candidate.mask.sum())},
            "level": 0, "state": "awaiting_audit"}


def run(call, inp):
    """The bulk job's handler."""
    from plexora.plugins.qc.server import scan as scanmod
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all, versions

    session_id = inp.session_id
    with engine_for(call, session_id) as engine:
        engine.record["state"] = "bulk_running"
        engine.record["bulk_job_id"] = (call.job or {}).get("job_id")
        project = engine.project
        options = dict(engine.options)
        done_scan = (engine.record.get("scan") or {}).get(project, {}).get("done")
    call.progress(done=0, total=4, message="calibrating")
    if not done_scan:
        calibration = calibrate_image(call, project, session_id)
        _check_stopped(session_id)

        def progress(done, total, message):
            call.progress(done=done, total=total + 2, message=message)
            _check_stopped(session_id)

        try:
            result, reused = scanmod.load_or_run(
                call.session, project, params=options.get("scan_params") or None,
                override=options.get("cycles_override"), channels=options.get("channels"),
                progress=progress, cancelled=call.cancelled)
        except AgentError as exc:
            with engine_for(call, session_id) as engine:
                engine.record["state"] = "failed"
                engine.record["error"] = {"code": exc.code, "message": exc.message}
            raise
        context = DetectorContext(result, project=project)
        raw, skipped = run_all(context, enabled=options.get("detectors"))
        built = cand.build(raw, result, project=project)
        with engine_for(call, session_id) as engine:
            record = engine.record
            record.setdefault("scan", {})[project] = {
                "fingerprint": result.meta["fingerprint"], "reused": reused, "done": True,
                "cycles": result.meta.get("cycles"), "tissue": result.meta.get("tissue"),
                "grid": result.grid, "skipped_detectors": skipped,
                "detector_versions": versions()}
            record.setdefault("calibration", {})[project] = calibration
            record["residual"] = built["residual"]
            by_channel = {c["name"]: c for c in result.channels}
            for unit in engine.units_of("channel", project):
                meta = by_channel.get(unit["id"])
                if meta is None:
                    engine.close(unit, "skipped_no_image", "not a channel of the image")
                    continue
                if result.meta.get("brightfield") and unit["order"] > 0:
                    engine.close(unit, "skipped_brightfield", "a brightfield image has one "
                                                             "plane")
                    continue
                unit["cycle"] = meta.get("cycle")
                unit["nuclear"] = meta.get("nuclear")
                unit["flags"] = meta.get("flags")
                if unit["state"] == "pending":
                    unit["state"] = "scanned"
            channels = {u["id"] for u in engine.units_of("channel", project)
                        if u["state"] == "scanned"}
            for candidate in built["ranked"]:
                unit = candidate_unit(candidate, project, result)
                if unit["audit_channel"] not in channels:
                    unit["audit_channel"] = next(
                        (c for c in unit["channels"] if c in channels), None)
                if unit["audit_channel"] is None:
                    continue
                key = unit_key(project, "candidate", unit["id"])
                if key not in record["units"]:
                    record["units"][key] = unit
            _update_result(engine, result, built, skipped)
        call.progress(done=3, total=4, message="scanned")
    if options.get("cells"):
        _check_stopped(session_id)
        from plexora.plugins.qc.server.cells import bulk as cell_bulk

        cell_bulk.run(call, session_id)
    with engine_for(call, session_id) as engine:
        if engine.record["state"] == "bulk_running":
            engine.record["state"] = "deciding"
        progress = engine.progress()
    call.progress(done=4, total=4, message="ready")
    return {"session_id": session_id, "progress": progress}


def _update_result(engine, result, built, skipped):
    from plexora.plugins.qc.server import results
    from plexora.plugins.qc.server.detectors import versions

    project = engine.project
    with results.lock(project):
        document = results.load(project)
        stored = results.get_result(project, document, engine.record["result_id"])
        if stored is None:
            stored = results.new_result(project, session_id=engine.id,
                                        strictness=engine.record.get("strictness"),
                                        agent={"id": engine.options["agent"]})
            stored["result_id"] = engine.record["result_id"]
        stored.update(scan_version=schemas.SCAN_VERSION,
                      scan_fingerprint=result.meta["fingerprint"],
                      detector_versions=versions(), skipped_detectors=skipped,
                      cycles_method=(result.meta.get("cycles") or {}).get("method"),
                      cycles=(result.meta.get("cycles") or {}).get("cycles") or [],
                      tissue=result.meta.get("tissue"), residual=built["residual"],
                      grid=result.grid, image_identity=result.meta.get("identity"))
        stored["channels"] = [{"name": c["name"], "cycle": c.get("cycle"),
                               "nuclear": c.get("nuclear"), "flags": c.get("flags"),
                               "status": "not_reviewed",
                               "summary": {k: (c.get("summary") or {}).get(k) for k in (
                                   "saturation_fraction", "tissue_ratio",
                                   "dynamic_range_decades", "focus_rel_p10")}}
                              for c in result.channels]
        results.put_result(document, stored)
        results.save(project, document)
