"""The deterministic pass of a QC session: everything code can do, first.

Runs as a job (`qc_session_bulk`), ahead of the agent: the display calibration
(so every sheet draws each channel the same way), the pyramid scan (cached by
fingerprint, so a rerun reads it back in a second), every detector, the
candidates merged and ranked, and one unit per channel and per candidate.
A staining or signal candidate (`schemas.STAINING_REGION_CLASSES`) is never
a unit: it is a hint on its channel's audit row (`staining_hints`,
`scan_hints`), judged per channel and never outlined; a failed channel is a
verdict on the channel (`channel_level`), listed on its row with no outline.
Nothing is settled from numbers alone: every channel still goes on an audit
sheet. Then the image checks the session plans (`checks_bulk`: Blur QC,
the Registration Check, Segmentation QC) score the tissue -- each
supersedes the scan detector that looked for the same thing on the coarser
scan grid (`schemas.CHECK_SUPERSEDES`), which runs after all when its check
could not. When the project has a cell table the cell modules measure their
columns too, so the agent's looks at the cells can start when the regions are
done. Heavy work runs outside the session lock; results are merged in under
it.

A throttled `_progress_announcer` turns the pass's own stages -- calibrating,
scanning, detectors, candidates, checks -- into a `qc.session` `phase`
event (an open viewer's agent panel, and any tab's long poll) and into the
record's own `bulk_progress`, which `QCEngine.progress` folds into `bulk` --
so `qc_next`'s wait sees the same stage even with nobody watching. At most
one announcement every `PROGRESS_THROTTLE_S`, sooner when the step's message
changes; `run`'s `finally` clears `bulk_progress` once the pass is over
(done, failed or cancelled), so a status read afterwards does not keep
repeating its last stage forever.
"""

from __future__ import annotations

import time

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


#: Two announcements this close together are folded into one, unless the
#: step's message changed -- a 20-minute pass should be seen moving, not
#: flickering.
PROGRESS_THROTTLE_S = 2.0


def _progress_announcer(call, session_id):
    """A throttled `announce(stage, message, done=None, total=None)` for this
    pass: writes the stage into the session's `bulk_progress` and sends a
    `phase` event carrying the engine's own `progress` (which now folds that
    stage into its `bulk`) -- so an open viewer's agent panel, and `qc_next`'s
    wait, both see it move."""
    from plexora.plugins.qc.capabilities_session import TOOLS

    last = {"at": 0.0, "message": None}

    def announce(stage, message="", done=None, total=None):
        now = time.monotonic()
        if message == last["message"] and now - last["at"] < PROGRESS_THROTTLE_S:
            return
        last["at"], last["message"] = now, message
        # Never wait for the session here: a stage reports from inside an open
        # image reader, and an answer drawing the next packet holds the
        # session and waits for that reader. A busy session skips this report
        # (the next one carries the stage on).
        from plexora.agent.sessions.store import SessionBusy

        st = store()
        try:
            with st.lock(session_id, timeout=0.05), engine_for(call, session_id) as engine:
                engine.record["bulk_progress"] = {"stage": stage, "message": message,
                                                  "done": done, "total": total}
                snapshot = {"images": engine.record["images"]}
                progress = engine.progress()
        except SessionBusy:
            last["at"], last["message"] = 0.0, None
            return
        TOOLS.phase(call, snapshot, session_id, "analyzing", progress=progress)

    return announce


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
            "level": 0, "state": "awaiting_audit",
            **(channel_level_fields() if candidate.class_hint == CHANNEL_LEVEL_CLASS else {})}


#: The one staining class that stays a candidate: a verdict on a whole
#: channel, decided from its audit row (`transitions._decide_channel_level`).
CHANNEL_LEVEL_CLASS = "empty_or_failed_channel"


def channel_level_fields() -> dict:
    """What makes a candidate a verdict on its channel: never outlined, never
    traced, removing no tissue (its marker is unreliable in every cell)."""
    return {"channel_level": True, "trace": "none", "variants": {}}


def _staining_hint(record, project, candidate):
    """Tally a staining candidate on its channels' rows instead of keeping it:
    {channel: {class: {n, max_score}}} under `record["staining_hints"]`."""
    hints = record.setdefault("staining_hints", {}).setdefault(project, {})
    for channel in candidate.channels or ():
        entry = hints.setdefault(channel, {}).setdefault(candidate.class_hint,
                                                         {"n": 0, "max_score": 0.0})
        entry["n"] += 1
        entry["max_score"] = round(max(float(entry["max_score"]), float(candidate.score)), 4)


def add_candidates(engine, project, scan, ranked, *, audited=False):
    """Candidate units for `ranked` detector candidates, each on the audit
    rows of the channels it was seen in. `audited`: found after those
    channels were audited (a fallback detector), so each goes straight to a
    confirm look instead of waiting for an audit that has passed.

    Filtered after the merge (`candidates.merge` turns an aggregate seen in
    several markers into debris, which stays): a candidate of a staining
    class becomes a hint on its channels' rows, never a unit."""
    record = engine.record
    channels = {u["id"]: u for u in engine.units_of("channel", project)}
    open_channels = {name for name, u in channels.items()
                     if u["state"] in ("scanned", "awaiting_audit", "awaiting_candidates",
                                       "audit_uncertain")}
    added = []
    for candidate in ranked:
        if candidate.class_hint in schemas.STAINING_REGION_CLASSES:
            _staining_hint(record, project, candidate)
            continue
        unit = candidate_unit(candidate, project, scan)
        if unit["audit_channel"] not in open_channels:
            unit["audit_channel"] = next(
                (c for c in unit["channels"] if c in open_channels), None)
        if unit["audit_channel"] is None:
            continue
        # A candidate merged across channels is drawn on every one of
        # their audit tiles, and kept when any row names it.
        unit["audit_channels"] = [c for c in unit["channels"] if c in open_channels] \
            or [unit["audit_channel"]]
        if audited and all(channels[c]["state"] != "scanned" for c in unit["audit_channels"]):
            unit["state"] = "awaiting_confirm"
            unit["forced_confirm"] = "found by a fallback detector after the audit"
        key = unit_key(project, "candidate", unit["id"])
        if key not in record["units"]:
            record["units"][key] = unit
            added.append(unit["id"])
    return added


def _superseded(engine):
    """The scan detectors a planned check supersedes."""
    checks = {u["check"] for u in engine.units_of("check") if u["state"] == "pending"}
    return sorted({schemas.CHECK_SUPERSEDES[c] for c in checks if c in schemas.CHECK_SUPERSEDES})


def run(call, inp):
    """The bulk job's handler."""
    from plexora.plugins.qc.server import scan as scanmod
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all, versions

    session_id = inp.session_id
    announce = _progress_announcer(call, session_id)
    try:
        return _run_bulk(call, session_id, announce, scanmod, DetectorContext, run_all, versions)
    finally:
        # Whatever happened -- done, failed, cancelled from the viewer -- the
        # pass's last stage must not linger in `bulk_progress`, or a status
        # read after the job finished still says its last stage forever.
        with engine_for(call, session_id) as engine:
            engine.record["bulk_progress"] = None


def _run_bulk(call, session_id, announce, scanmod, DetectorContext, run_all, versions):
    with engine_for(call, session_id) as engine:
        engine.record["state"] = "bulk_running"
        engine.record["bulk_job_id"] = (call.job or {}).get("job_id")
        project = engine.project
        options = dict(engine.options)
        done_scan = (engine.record.get("scan") or {}).get(project, {}).get("done")
        if not done_scan:
            engine.record["superseded"] = _superseded(engine)
        superseded = list(engine.record.get("superseded") or [])
    call.progress(done=0, total=4, message="calibrating")
    if not done_scan:
        announce("calibrating", "calibrating display")
        calibration = calibrate_image(call, project, session_id)
        _check_stopped(session_id)

        def progress(done, total, message):
            call.progress(done=done, total=total + 2, message=message)
            announce("scanning", message, done=done, total=total)
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
        enabled = options.get("detectors") or list(versions())
        enabled = [name for name in enabled if name not in superseded]

        def detector_progress(done, total, name):
            announce("detectors", name, done=done, total=total)

        raw, skipped = run_all(context, enabled=enabled, progress=detector_progress,
                               cancelled=lambda: _check_stopped(session_id))
        by_detector = {d: c for c, d in schemas.CHECK_SUPERSEDES.items()}
        skipped = skipped + [{"name": name, "reason": f"superseded by the {by_detector[name]} "
                                                     "check", "superseded_by": by_detector[name]}
                             for name in superseded]
        announce("candidates", "ranking candidates")
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
            add_candidates(engine, project, result, built["ranked"])
            _update_result(engine, result, built, skipped)
        call.progress(done=3, total=4, message="scanned")
    _check_stopped(session_id)
    background_step(call, session_id, scan=result if not done_scan else None,
                    announce=announce)
    _check_stopped(session_id)
    dna_step(call, session_id, announce=announce)
    _check_stopped(session_id)
    from plexora.plugins.qc.server import checks_bulk

    try:
        ran = checks_bulk.run(call, session_id, announce=announce)
    except AgentError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a check never fails the session
        from plexora.agent.jobs import JobCancelled

        if isinstance(exc, JobCancelled):
            raise
        with engine_for(call, session_id) as engine:
            skipped_now = []
            for unit in checks_bulk.planned(engine):
                engine.close(unit, "skipped_not_applicable", f"the checks failed: {exc}")
                skipped_now.append({"id": unit["id"], "check": unit["check"]})
        ran = {"ran": [], "skipped": skipped_now}
    if ran["skipped"]:
        checks_bulk.fallback(call, session_id, ran["skipped"])
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
        hints = (engine.record.get("staining_hints") or {}).get(project) or {}
        stored["channels"] = [{"name": c["name"], "cycle": c.get("cycle"),
                               "nuclear": c.get("nuclear"), "flags": c.get("flags"),
                               "status": "not_reviewed",
                               "summary": {k: (c.get("summary") or {}).get(k) for k in (
                                   "saturation_fraction", "tissue_ratio",
                                   "dynamic_range_decades", "focus_rel_p10")},
                               **({"scan_hints": hints[c["name"]]}
                                  if hints.get(c["name"]) else {})}
                              for c in result.channels]
        results.put_result(document, stored)
        results.save(project, document)


def _note(engine, check, status, reason):
    notes = engine.record.setdefault("planning_notes", [])
    if not any(n.get("check") == check for n in notes):
        notes.append({"check": check, "status": status, "reason": reason})


def background_step(call, session_id, *, scan=None, announce=None):
    """The tissue step, first in the bulk pass: the Background ROI (the
    glass outside the feathered tissue) written in apply mode into the
    session's own result, receipted on the session (a rollback undoes it);
    propose mode writes nothing and says so. Once per session."""
    from plexora.plugins.qc.server import background

    with engine_for(call, session_id) as engine:
        if engine.record.get("background") is not None:
            return engine.record["background"]
        project = engine.project
        options = dict(engine.options)
        result_id = engine.record["result_id"]
    out = {"written": [], "kept": []}
    if not options.get("background_roi", True):
        out["skipped"] = "background_roi is off"
    elif options.get("mode") != "apply":
        out["skipped"] = "propose mode"
    else:
        if announce is not None:
            announce("tissue", "outlining the background outside the tissue")
        try:
            out = background.write(call, project, scan=scan, result_id=result_id, cells=False,
                                   session_id=session_id)
        except AgentError as exc:
            out = {"written": [], "kept": [], "error": exc.message}
        except Exception as exc:  # noqa: BLE001 -- the background never fails the session
            from plexora.agent.jobs import JobCancelled

            if isinstance(exc, JobCancelled):
                raise
            out = {"written": [], "kept": [], "error": str(exc)}
    with engine_for(call, session_id) as engine:
        engine.record["background"] = {k: out.get(k) for k in (
            "written", "kept", "removed", "reason", "error", "skipped", "tissue_method")
            if out.get(k) is not None}
        engine.record.setdefault("receipts", []).extend(out.get("receipts") or [])
        if out.get("skipped") == "propose mode":
            _note(engine, "background", "proposed", "propose mode writes no ROI: "
                                                    "write_qc_background_roi writes it")
        elif out.get("error") or (out.get("reason") and not out.get("kept")):
            _note(engine, "background", "not_run", out.get("error") or out.get("reason"))
        return engine.record["background"]


def dna_step(call, session_id, *, announce=None):
    """DNA retention across cycles, when the project has a table and a mask:
    measured (or reused) once, its summary stored on the session's result
    (`result["dna_retention"]`) for the cells, the report and the final
    review. One that cannot run is a planning note, never a failure."""
    from plexora.plugins.qc.server import dna_retention, results

    with engine_for(call, session_id) as engine:
        if engine.record.get("dna_retention") is not None:
            return engine.record["dna_retention"]
        project = engine.project
        has_table = bool(engine.record.get("has_table"))
        result_id = engine.record["result_id"]
    record = call.session.project(project)
    if not has_table or not record.segmentation.available:
        with engine_for(call, session_id) as engine:
            engine.record["dna_retention"] = {"skipped": "no cell table and mask"}
        return None

    def progress(done=0, total=1, message=""):
        if announce is not None:
            announce("dna_retention", f"DNA retention: {message}", done=done, total=total)
        _check_stopped(session_id)

    try:
        summary, reused = dna_retention.load_or_run(call.session, project, progress=progress,
                                                    check_cancelled=lambda: _check_stopped(
                                                        session_id))
    except AgentError as exc:
        with engine_for(call, session_id) as engine:
            engine.record["dna_retention"] = {"error": exc.message}
            _note(engine, "dna_retention", "not_run", exc.message)
        return None
    except Exception as exc:  # noqa: BLE001 -- a measure never fails the session
        from plexora.agent.jobs import JobCancelled

        if isinstance(exc, JobCancelled):
            raise
        with engine_for(call, session_id) as engine:
            engine.record["dna_retention"] = {"error": str(exc)}
            _note(engine, "dna_retention", "not_run", f"failed: {exc}")
        return None
    with results.lock(project):
        document = results.load(project)
        stored = results.get_result(project, document, result_id)
        if stored is not None:
            stored["dna_retention"] = summary
            results.put_result(document, stored)
            results.save(project, document)
    with engine_for(call, session_id) as engine:
        engine.record["dna_retention"] = {"fingerprint": summary.get("fingerprint"),
                                          "reused": reused,
                                          "digest": dna_retention.digest_line(summary)}
    return summary
