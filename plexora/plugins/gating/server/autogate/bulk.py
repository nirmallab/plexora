"""The deterministic pass: every marker profiled, and settled where numbers can.

Runs as a job (`gating_session_bulk`), ahead of the agent: display
calibration per image, then per marker in gating order the profile (five
estimators, separation, stability, cell and overview QC), and the first
decision -- skip it (locked, excluded, a user's own gate, not a marker of this
image), close it (technically failed with no image to check), accept it at T1
(clean bimodal: the GMM gate is written now and the marker queued for the
audit sheet), or leave it waiting for a look.

Heavy work happens outside the session lock; each result is merged in under
it (`engine_for`), so the agent can already be answering packets for the
first markers while the pass works through the rest -- and for a dataset,
through the next images.
"""

from __future__ import annotations

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
    profile = views.full_profile(call.session, ds, marker, seed=int(options["seed"]),
                                 compartment=_context_of(panel, marker)["compartment"])
    return {**kept, "profile": profile, "gate": gate,
            "context": _context_of(panel, marker),
            "no_image_channel": views.image_channel(ds, marker) is None,
            "image_qc": profile.get("image_qc")}


def settle_unit(engine, unit, prepared):
    """Record a profiled unit's numbers and make its first decision (under
    the lock). In a dataset, an image other than the reference waits for the
    reference's gate instead (`transfer_pending`)."""
    unit["seen"] = prepared.get("seen")
    unit["thresholded"] = prepared.get("thresholded")
    if "close" in prepared:
        state, reason = prepared["close"]
        engine.close(unit, state, reason)
        return
    fill_unit(unit, prepared)
    record = engine.record
    if record.get("scope") == "dataset" and unit["project"] != record.get("reference_image"):
        unit["state"] = "transfer_pending"
        return
    decide_first(engine, unit)


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
        "context": prepared["context"], "no_image_channel": prepared["no_image_channel"],
        "image_qc": {k: (prepared.get("image_qc") or {}).get(k)
                     for k in ("saturation_fraction", "tissue_ratio",
                               "dynamic_range_decades", "flags")}
        if prepared.get("image_qc") else None,
    })


def decide_first(engine, unit):
    """The first decision for a unit whose numbers are recorded."""
    options = engine.options
    if unit.get("gmm") is not None and unit.get("candidate") is None:
        unit["candidate"] = unit["gmm"]
    t1 = unit.get("t1") or {}
    max_tier = int(options["max_tier"])
    tier = t1.get("recommended_tier")
    hard = schemas.hard(unit["flags"])
    binary = (unit["context"] or {}).get("binary", True)
    known = bool((unit["context"] or {}).get("canonical"))
    if unit.get("gmm") is None:
        if unit["no_image_channel"] or max_tier < 2:
            engine.close(unit, "technically_failed",
                         "no mixture to fit (a flat, sparse or constant column)",
                         confidence="failed_qc")
        else:
            unit["candidate"] = (unit.get("seen") or [None])[0]
            unit["qc_reason"] = ["no mixture to fit"]
            unit["state"] = "qc_confirm"
        return
    if unit["class"] == "continuous" and known and not binary:
        engine.close(unit, "not_binary",
                     "a continuously expressed marker (vocabulary) with no valley; gate it "
                     "only with an explicit policy", confidence="manual_review")
        return
    if tier == "QC" or hard:
        if unit["no_image_channel"] or max_tier < 2:
            engine.close(unit, "technically_failed" if hard else "manual_review_recommended",
                         f"technical flags ({', '.join(hard) or unit['class']}) and no image "
                         "to check them against", confidence="failed_qc" if hard else None)
        else:
            unit["qc_reason"] = hard or [unit["class"]]
            unit["state"] = "qc_confirm"
        return
    if t1.get("accept"):
        unit["path"] = "t1"
        unit["method"] = "gmm"
        if options["audit_sheet"] and not unit["no_image_channel"] and max_tier >= 2:
            # Written now so later markers can use it as a reference; the audit
            # sheet confirms it (high) or sends it to a proper look.
            engine.write(unit, unit["candidate"], method="gmm", confidence="high",
                         state="accepted_t1", tier="T1")
            if unit["state"] not in TERMINAL:
                unit["state"] = "accepted_t1"
        else:
            unit["audited"] = False
            engine.finalize(unit, method="gmm")
        return
    if max_tier < 2 or unit["no_image_channel"]:
        unit["path"] = "t2"
        unit["ai_confidence"] = 0.0
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return
    unit["state"] = "awaiting_t2"


def _check_stopped(session_id):
    """End the pass when the user stopped the session in the viewer (the route
    cannot reach this process's job; the pass reads the control file)."""
    from plexora.agent.jobs import JobCancelled
    from plexora.plugins.gating.server.autogate.engine import store

    if store().control(session_id).get("stopped"):
        raise JobCancelled("stopped from the viewer")


def run(call, inp):
    """The bulk job's handler."""
    from plexora.plugins.gating.server.autogate import context

    session_id = inp.session_id
    with engine_for(call, session_id) as engine:
        engine.record["state"] = "bulk_running"
        engine.record["bulk_job_id"] = (call.job or {}).get("job_id")
        images = list(engine.record["images"])
        order = list(engine.record["order"])
        options = dict(engine.options)
    total = len(images) * len(order)
    done = 0
    call.progress(done=0, total=total, message="starting")
    with engine_for(call, session_id, save=False) as engine:
        # A resumed pass keeps the calibration it already made.
        calibrations = dict(engine.record.get("calibration") or {})
    for project in images:
        call.check_cancelled()
        if project not in calibrations:
            calibrations[project] = calibrate_image(call, project, session_id)
        ds = call.session.data(project)
        panel = context.for_project(ds)
        for marker in order:
            call.check_cancelled()
            _check_stopped(session_id)
            key = unit_key(project, marker)
            with engine_for(call, session_id) as engine:
                unit = engine.record["units"].get(key)
                pending = unit is not None and unit["state"] == "pending"
            if pending:
                try:
                    prepared = prepare_unit(call, session_id, project, marker, panel, options)
                except AgentError as exc:
                    prepared = {"close": ("manual_review_recommended",
                                          f"could not be profiled: {exc.message}")}
                with engine_for(call, session_id) as engine:
                    unit = engine.record["units"][key]
                    if unit["state"] == "pending":
                        settle_unit(engine, unit, prepared)
            done += 1
            call.progress(done=done, total=total, message=f"{project}: {marker}")
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
        progress = engine.progress()
    return {"session_id": session_id, "progress": progress, "calibration": calibrations}
