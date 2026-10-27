"""The deterministic pass: every marker profiled, and the first decision made.

Runs as a job (`gating_session_bulk`), ahead of the agent: display
calibration per image, then per marker in gating order the profile (five
estimators, separation, stability, cell and overview QC), and the first
decision -- skip it (locked, excluded, a user's own gate, not a marker of this
image, no image channel to judge it on), accept it at T1 (clean bimodal: the
GMM gate is written now and the marker queued for the audit sheet), or leave
it waiting for a look. No gate is accepted here that the pixels will not be
shown: there is no numbers-only path. A continuously expressed marker
waits for a look like any other: it is gated where expression rises out of
background, never skipped as "not binary". Its starting gate, like every
looked-at marker's, is chosen just before the first look (`Engine.propose`),
when its partners have been gated.

Heavy work happens outside the session lock; each result is merged in under
it (`engine_for`), so the agent can already be answering packets for the
first markers while the pass works through the rest -- and for a dataset,
through the next images.
"""

from __future__ import annotations

import time

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import schemas, views
from plexora.plugins.gating.server.autogate.engine import (TERMINAL, compact_profile,
                                                           engine_for, unit_key, _metrics)


def _context_of(panel, marker):
    entry = (panel.get("entries") or {}).get(marker) or {}
    return {k: entry.get(k) for k in ("canonical", "role", "compartment", "lineage", "binary",
                                      "caveats", "expected_fraction", "source", "partners")}


def calibrate_image(call, project, session_id):
    """Store the image's display calibration (derived state; audited)."""
    from plexora.agent.evidence import calibration

    try:
        record, changed = calibration.calibrate(call.session, project)
    except AgentError as exc:
        return {"project": project, "error": exc.message}
    call.audit.append({"status": "ok", "operation_id": f"{call.operation_id}.cal",
                       "capability": "gating.calibrate_display", "project": project,
                       "arguments": {"project": project, "gating_session": session_id},
                       "receipt": {"changed": changed,
                                   "persistent_state": "plugin_store:display",
                                   "reversible": False}})
    return {"project": project, "revision": calibration.revision(project),
            "needs_vision_check": calibration.needs_vision_check(record),
            "changed": changed}


def prepare_unit(call, session_id, project, marker, panel, options):
    """Everything about one unit that can be computed without the lock."""
    from plexora.plugins.gating.server.autogate import provenance

    ds = call.session.data(project)
    if marker not in ds.table.markers:
        return {"close": ("skipped_no_marker", f"{marker!r} is not a marker of {project!r}")}
    row = provenance.read(ds.name).get(marker) or {}
    gate = model.get_gate(ds, marker)
    seen = [gate["low"], gate["high"]]
    kept = {"seen": seen, "thresholded": bool(gate["thresholded"])}
    status = row.get("status")
    if status == "locked":
        return {**kept, "close": ("skipped_locked", "the user locked this gate")}
    # An approved gate is never passed through, whatever overwrite_manual says:
    # the option re-gates what the user set by hand, not what they signed off.
    if status == "approved":
        return {**kept, "close": ("skipped_locked", "the user approved this gate")}
    if status == "excluded":
        return {**kept, "close": ("skipped_excluded", "excluded from automatic gating")}
    # The user's own gate: thresholded, and not something an agent wrote -- no
    # row, a row that never recorded a write (a proposal, a kept manual gate),
    # a manual or rolled-back row, or a written gate edited since.
    manual = gate["thresholded"] and (
        not row or row.get("written_low") is None
        or row.get("method") in ("manual", "rolled_back", None)
        or provenance.edited_since(row, gate))
    if manual and not options["overwrite_manual"]:
        return {**kept,
                "close": ("skipped_manual", "a gate the user set is kept (overwrite_manual "
                                             "is off); the GMM proposal is recorded")}
    if views.image_channel(ds, marker) is None:
        # Checked at the start (`capabilities_session.channel_match`); a
        # channel renamed since is caught here. No pixels, no gate: nothing is
        # profiled or written for it.
        return {**kept, "close": ("skipped_no_channel", "no image channel matches this "
                                  "marker: automatic gating judges every gate on the pixels")}
    profile = views.full_profile(call.session, ds, marker, seed=int(options["seed"]),
                                 compartment=_context_of(panel, marker)["compartment"])
    return {**kept, "profile": profile, "gate": gate,
            "context": _context_of(panel, marker),
            "image_qc": profile.get("image_qc")}


def settle_unit(engine, unit, prepared):
    """Record a profiled unit's numbers and make its first decision (under
    the lock). In a dataset, an image other than the reference waits for the
    reference's gate instead (`transfer_pending`)."""
    unit["seen"] = prepared.get("seen")
    unit["thresholded"] = prepared.get("thresholded")
    if "close" in prepared:
        state, reason = prepared["close"]
        # The user's own gate, or data that could not be read: no look can settle it.
        engine.close(unit, state, reason, unlooked="not_profiled")
        return
    fill_unit(unit, prepared)
    record = engine.record
    if record.get("scope") == "dataset" and unit["project"] != record.get("reference_image"):
        unit["state"] = "transfer_pending"
        return
    decide_first(engine, unit)


#: The hard flags a QC verdict of `usable` settles (`decide_first`): read off
#: the image overview, they are what QC's channel review looks at.
QC_SETTLED_FLAGS = ("saturated", "empty_channel", "illumination_gradient")


def fill_unit(unit, prepared):
    profile = prepared["profile"]
    fit = profile.get("fit") or {}
    unit.update({
        "summary": compact_profile(profile), "metrics": _metrics(profile),
        "class": profile.get("distribution_class"), "t1": profile.get("t1"),
        "flags": profile.get("flags") or [], "fit_space": profile.get("fit_space"),
        "gmm": fit.get("gate_raw"), "candidate": fit.get("gate_raw"),
        "high": prepared["gate"]["high"], "source": "gmm",
        "qc_exclusion": profile.get("qc_exclusion"),
        # QC's word on the channel (`cell_exclusions.VERDICTS`), when QC ran.
        "qc_verdict": (profile.get("qc_exclusion") or {}).get("channel_verdict"),
        "context": prepared["context"],
        "image_qc": {k: (prepared.get("image_qc") or {}).get(k)
                     for k in ("saturation_fraction", "tissue_ratio",
                               "dynamic_range_decades", "flags")}
        if prepared.get("image_qc") else None,
    })


def decide_first(engine, unit):
    """The first decision for a unit whose numbers are recorded. Nothing here
    accepts a gate the pixels have not been shown: a clean split is written
    for the audit sheet to confirm, and everything else waits for a look."""
    options = engine.options
    if unit.get("gmm") is not None and unit.get("candidate") is None:
        unit["candidate"] = unit["gmm"]
    t1 = unit.get("t1") or {}
    tier = t1.get("recommended_tier")
    if unit.get("qc_verdict") == "usable":
        # QC looked at this channel and found a working stain: a flag read off
        # the overview (a gradient, a saturated corner) is QC's to judge, and
        # it has; the marker takes the ordinary path, the flag kept as a note.
        unit["flags"] = [f for f in unit["flags"] if f not in QC_SETTLED_FLAGS] + \
            [f"{f}_downgraded" for f in unit["flags"] if f in QC_SETTLED_FLAGS]
    hard = schemas.hard(unit["flags"])
    # False only when the vocabulary (or the panel's context) says so; an
    # unknown marker's None is not a statement.
    continuous = (unit["context"] or {}).get("binary") is False
    if unit.get("gmm") is None:
        unit["candidate"] = (unit.get("seen") or [None])[0]
        unit["qc_reason"] = ["no mixture to fit"]
        unit["state"] = "qc_confirm"
        return
    if continuous:
        # Continuous expression (HLA-ABC, B2M, PD-L1): no valley, but a
        # background to rise out of -- gated at that onset (`scoring`), with
        # looks that judge where real expression starts. Never skipped.
        unit["continuous"] = True
    if tier == "QC" or hard:
        unit["qc_reason"] = hard or [unit["class"]]
        unit["state"] = "qc_confirm"
        return
    if t1.get("accept") and engine.note_observed(unit):
        # The contrast check saw what a clean split cannot: dim cells stained
        # below the gate, debris among the brightest, or the wrong pattern
        # (`SessionOptions.observations`). The marker gets a look.
        unit["observed_routed"] = True
        unit["state"] = "awaiting_t2"
        return
    if t1.get("accept"):
        unit["path"] = "t1"
        unit["method"] = "gmm"
        if options["audit_sheet"]:
            # Written now so later markers can use it as a reference; the audit
            # sheet confirms it (high) or sends it to a proper look.
            engine.write(unit, unit["candidate"], method="gmm", confidence="high",
                         state="accepted_t1", tier="T1")
            if unit["state"] not in TERMINAL:
                unit["state"] = "accepted_t1"
        else:
            unit["audited"] = False
            unit["state"] = "awaiting_regression"
            engine.settle(unit)
        return
    # Whether the marker has a positive population at all comes first: a
    # mixture splits every column in two (`absence`).
    unit["state"] = "awaiting_absence" if engine.options["absence_check"] \
        and not unit.get("absence_done") else "awaiting_t2"


#: How a marker's first decision reads on the agent panel's progress line
#: ("CD3E: waiting for a look"); any other state reads as its own words.
BULK_WORDS = {
    "accepted_t1": "clean split, written for the audit sheet",
    "awaiting_t2": "waiting for a look",
    "awaiting_panel": "waiting for a look at single cells",
    "qc_confirm": "the stain gets a look first",
    "awaiting_absence": "waiting for a look at six regions",
    "transfer_pending": "waiting for the reference image",
    "transfer_check": "reference gate carried, waiting for a look",
    "skipped_no_channel": "no image channel, skipped",
    "skipped_manual": "your gate kept",
    "skipped_locked": "locked, kept",
    "skipped_excluded": "excluded, skipped",
    "skipped_no_marker": "not in this image",
    "manual_review_recommended": "could not be profiled, needs review",
    "display_set": "brightness settled",
}

#: Announcements closer together than this are dropped (the last one always
#: goes): a run of skipped markers settles faster than a panel can read.
PROGRESS_THROTTLE_S = 1.0


def _progress_announcer(call, session_id):
    """`announce(images, progress, last=False)`: tell the session's tabs the
    pass moved, in a `phase` event carrying the engine's `progress` -- whose
    `bulk` now holds the marker just settled (`bulk_progress`) -- as QC's
    bulk pass does, so core's agent panel shows each marker as it is decided
    instead of six silent minutes."""
    from plexora.plugins.gating.capabilities_session import _phase

    sent = {"at": 0.0}

    def announce(images, progress, *, last=False):
        now = time.monotonic()
        if not last and now - sent["at"] < PROGRESS_THROTTLE_S:
            return
        sent["at"] = now
        try:
            _phase(call, {"images": images}, session_id, "analyzing", progress=progress)
        except Exception:  # a progress line is never a reason to stop the pass
            pass

    return announce


def _check_stopped(session_id):
    """End the pass when the user stopped the session in the viewer (the route
    cannot reach this process's job; the pass reads the control file)."""
    from plexora.agent.jobs import JobCancelled
    from plexora.plugins.gating.server.autogate.engine import store

    if store().control(session_id).get("stopped"):
        raise JobCancelled("stopped from the viewer")


def _check_matrix(call, session_id):
    """End the pass, and stop the session saying why, when its images are no
    longer read from the values it started on (`_matrix_moved`): a profile
    of the new values beside gates of the old would mix two units."""
    from plexora.agent.jobs import JobCancelled
    from plexora.plugins.gating import capabilities_session
    from plexora.plugins.gating.server.autogate.engine import store

    if capabilities_session._matrix_moved(call, store(), session_id):
        raise JobCancelled("the expression values changed under the session")


#: The longest a basic QC run may take before the pass goes on without it.
BASIC_QC_TIMEOUT_S = 3600.0


def _basic_qc(call, session_id, project):
    """QC's model-free checks (`qc.basic_run`) on an image with no QC result,
    before it is calibrated or profiled: what they find is left out of its
    windows, fits, samples and looks as any QC's is. Once per image and
    session; a failure is recorded and logged, and the pass goes on."""
    from plexora.agent import cell_exclusions, jobs, registry

    with engine_for(call, session_id, save=False) as engine:
        due = project in (engine.record.get("qc_missing") or ()) \
            and project not in (engine.record.get("basic_qc") or {})
    if not due:
        return
    outcome = {"status": "skipped", "reason": "QC is not installed"}
    if any(c.name == "qc.basic_run" for c in registry.all_capabilities()):
        call.progress(message=f"basic QC on {project} (no model calls)")
        started = registry.invoke(call.session, "qc.basic_run", {"project": project},
                                  policy=call.policy, audit=call.audit, link=call.link,
                                  operation_id=f"{call.operation_id}.qc.{project}")
        if not started.get("ok"):
            outcome = {"status": "failed",
                       "reason": (started.get("error") or {}).get("message") or "refused"}
        else:
            job_id = started["result"]["job_id"]
            deadline = time.monotonic() + BASIC_QC_TIMEOUT_S
            record = None
            while time.monotonic() < deadline:
                record = jobs.store().wait(job_id, timeout_s=5.0)
                if record is None or record.get("status") in jobs.TERMINAL:
                    break
                try:
                    call.check_cancelled()
                except Exception:
                    jobs.store().cancel(job_id)
                    raise
            status = (record or {}).get("status")
            if status == "done":
                result = record.get("result") or {}
                outcome = {"status": "done", "job_id": job_id,
                           **{k: result.get(k) for k in ("ran", "skipped", "regions",
                                                         "held_back", "note")}}
            else:
                outcome = {"status": "failed", "job_id": job_id,
                           "reason": ((record or {}).get("error") or {}).get("message")
                           or f"the job ended {status or 'out of time'}"}
    block = cell_exclusions.describe(call.session.data(project))
    with engine_for(call, session_id) as engine:
        engine.record.setdefault("basic_qc", {})[project] = outcome
        engine.record.setdefault("qc_exclusion", {})[project] = {
            k: block[k] for k in ("mode", "applied", "reason", "n_left_out", "n_cells",
                                  "fraction", "result_id", "origin", "n_exclude", "n_warn",
                                  "n_marker", "warning") if k in block}
        if block.get("applied"):
            engine.record["qc_missing"] = [p for p in engine.record.get("qc_missing") or ()
                                           if p != project]
        engine.log(event="basic_qc", project=project, status=outcome["status"],
                   ran=outcome.get("ran"), regions=outcome.get("regions"),
                   reason=outcome.get("reason"), n_left_out=block.get("n_left_out"))


def run(call, inp):
    """The bulk job's handler."""
    from plexora.plugins.gating.server.autogate import biology, context

    session_id = inp.session_id
    with engine_for(call, session_id) as engine:
        engine.record["state"] = "bulk_running"
        engine.record["bulk_job_id"] = (call.job or {}).get("job_id")
        images = list(engine.record["images"])
        order = list(engine.record["order"])
        options = dict(engine.options)
        sample_biology = engine.record.get("biology")
    total = len(images) * len(order)
    done = 0
    call.progress(done=0, total=total, message="starting")
    with engine_for(call, session_id, save=False) as engine:
        # A resumed pass keeps the calibration it already made.
        calibrations = dict(engine.record.get("calibration") or {})
    announce = _progress_announcer(call, session_id)
    for project in images:
        call.check_cancelled()
        if options["basic_qc"]:
            _basic_qc(call, session_id, project)
        if project not in calibrations:
            calibrations[project] = calibrate_image(call, project, session_id)
            with engine_for(call, session_id) as engine:
                # Stored now, not after the image's markers: the contrast
                # check (`display_setup`) goes out as soon as it is there.
                engine.record.setdefault("calibration", {})[project] = calibrations[project]
                display = (engine.record.get("display") or {}).get("projects") or {}
                if calibrations[project].get("error") and project in display \
                        and display[project].get("status") == "pending":
                    display[project].update(status="unavailable",
                                            reason=calibrations[project]["error"])
                    engine.settle_display()
        if options["purpose"] == "contrast":
            # Auto contrast on its own: the calibration is all this pass
            # makes; the markers wait for the image's contrast check and
            # close with it (`close_display_units`), never profiled.
            with engine_for(call, session_id) as engine:
                engine.close_display_units()
                profiled = engine.record.setdefault("images_profiled", [])
                if project not in profiled:
                    profiled.append(project)
            done += len(order)
            call.progress(done=done, total=total, message=f"{project}: calibrated")
            continue
        ds = call.session.data(project)
        for marker in order:
            call.check_cancelled()
            _check_stopped(session_id)
            _check_matrix(call, session_id)
            key = unit_key(project, marker)
            with engine_for(call, session_id) as engine:
                unit = engine.record["units"].get(key)
                pending = unit is not None and unit["state"] == "pending"
            settled = None
            if pending:
                # Read for every marker, not once for the pass: the panel's
                # context packet is answered while this pass runs, and a
                # snapshot taken before it would overwrite the answer on every
                # marker settled after it (one file read; `context.for_project`
                # caches nothing).
                panel = biology.overlay(context.for_project(ds), sample_biology)
                try:
                    prepared = prepare_unit(call, session_id, project, marker, panel, options)
                except AgentError as exc:
                    prepared = {"close": ("manual_review_recommended",
                                          f"could not be profiled: {exc.message}")}
                with engine_for(call, session_id) as engine:
                    unit = engine.record["units"][key]
                    if unit["state"] == "pending":
                        settle_unit(engine, unit, prepared)
                    settled = unit["state"]
                    words = BULK_WORDS.get(settled, settled.replace("_", " "))
                    engine.record["bulk_progress"] = {
                        "stage": "profiling", "message": f"{marker}: {words}",
                        "done": done + 1, "total": total}
                    progress = engine.progress()
                    images_now = list(engine.record["images"])
            done += 1
            call.progress(done=done, total=total, message=f"{project}: {marker}")
            if settled is not None:
                announce(images_now, progress, last=done == total)
        with engine_for(call, session_id) as engine:
            engine.record.setdefault("calibration", {})[project] = calibrations[project]
            profiled = engine.record.setdefault("images_profiled", [])
            if project not in profiled:
                profiled.append(project)
    with engine_for(call, session_id) as engine:
        if engine.record.get("scope") == "dataset":
            from plexora.plugins.gating.server.autogate import transfer

            transfer.after_bulk(engine)
        engine.record["state"] = "deciding"
        engine.record.pop("bulk_progress", None)
        progress = engine.progress()
    return {"session_id": session_id, "progress": progress, "calibration": calibrations}
