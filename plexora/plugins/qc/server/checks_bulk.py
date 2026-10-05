"""The image checks inside a session's bulk pass: score first, look second.

Each pending check unit (`capabilities_session.plan_checks`) is run with the
same functions the QC panel runs -- Blur QC per channel, the Registration
Check per comparison, Segmentation QC once -- reusing a cached result when
the inputs have not changed. The heavy work runs outside the session lock;
what it found is merged in under it: the result's fingerprint, the
automatic threshold, one step of it, the distribution and the flagged share
at the automatic bar, and whether the problem may be everywhere. The unit
then waits for its `score_review`.

A check that cannot run here (no mask, too few evaluable tiles, an image
over `max_pixels`, a failure) is closed `skipped_not_applicable` with why;
the scan detector it would have superseded then runs after all
(`fallback`), so a session never loses a kind of artifact to a failed check.
The one exception is a Registration Check skipped because the DNA channel of
each cycle is not certain (`cycles.resolved`): the scan's cross-cycle
detectors are under the same rule, so nothing compares cycles at all.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas, score_fields

VERSION = "2"


def planned(engine) -> list:
    return [u for u in engine.units_of("check") if u["state"] == "pending"]


def _run_blur(call, project, unit, progress, cancelled):
    from plexora.plugins.qc.server import blur

    summary, reused = blur.load_or_run(call.session, project, channel=unit["channel"],
                                       progress=progress, check_cancelled=cancelled)
    if summary.get("status") != "ok":
        raise _Skip(f"too few evaluable tiles in {unit['channel']} to score focus")
    arrays = blur.load_arrays(project, summary["fingerprint"])
    field = score_fields.from_blur(summary, arrays)
    return field, {"fingerprint": summary["fingerprint"], "reused": reused}


def _run_registration(call, project, unit, progress, cancelled):
    from plexora.plugins.qc.server import registration, score_review

    state = registration.load_state(project)
    if unit.get("reference"):
        state = {**state, "reference": unit["reference"]}
    progress(done=0, total=1, message=f"registering {unit['channel']}")
    out = registration.compute(call.session, project, state, comparison=unit["channel"],
                               include_overlay=False)
    entry = registration.field_entry(project, out["field_fingerprint"])
    if entry is None:
        raise _Skip("the registration field could not be kept")
    pixel_um = _pixel_um(call, project)
    size = score_review.image_size(call.session, project)
    field = score_fields.from_registration(
        entry, pixel_um=pixel_um, fingerprint=out["field_fingerprint"],
        image_size=size, reference=out["reference"], comparison=out["comparison"],
        stats=out["stats"])
    # The places only one cycle has nuclei in are kept out of the score and
    # become tissue-loss candidates of the cycle that lost them.
    from plexora.plugins.qc.server import check_candidates

    one_cycle = check_candidates.one_cycle_found(
        entry, pixel_um=pixel_um, fingerprint=out["field_fingerprint"], image_size=size,
        reference=out["reference"], comparison=out["comparison"])
    return field, {"fingerprint": out["field_fingerprint"], "reused": out["reused"],
                   "reference": out["reference"], "_one_cycle": one_cycle}


def _run_segmentation(call, project, unit, progress, cancelled):
    from plexora.plugins.qc.server import score_review
    from plexora.plugins.qc.server.segqc import run as segqc

    size = score_review.image_size(call.session, project)
    limit = unit.get("max_pixels")
    if limit and size[0] * size[1] > int(limit):
        raise _Skip(f"the image has {size[0] * size[1]:,} pixels, over this session's "
                    f"max_pixels ({int(limit):,})")
    segqc.note_running(project, (call.job or {}).get("job_id"))
    try:
        summary, reused = segqc.load_or_run(call.session, project, progress=progress,
                                            check_cancelled=cancelled)
    finally:
        segqc.note_running(project, None)
    field = score_fields.from_segmentation(project, summary, image_size=size)
    if field is None:
        raise _Skip("this Segmentation QC result has no cell positions")
    unit["channel"] = summary.get("dna_channel")
    unit["channels"] = [summary.get("dna_channel")] if summary.get("dna_channel") else []
    return field, {"fingerprint": summary["fingerprint"], "reused": reused,
                   "segqc": {k: summary.get(k) for k in (
                       "n_cells", "pct_cells", "pct_area", "d_nucleus_um", "notice",
                       "peaks_on_labels_pct")}}


def _run_artifacts(call, project, unit, progress, cancelled):
    """One category of the Artifact Detector: the run covers every category
    and channel at once, so its sibling units reuse it by fingerprint."""
    from plexora.plugins.qc.server import artifacts

    summary, reused = artifacts.load_or_run(call.session, project, progress=progress,
                                            check_cancelled=cancelled)
    if summary.get("status") != "ok":
        raise _Skip("no tissue was found to look for artifacts on")
    arrays = artifacts.load_arrays(project, summary["fingerprint"])
    if arrays is None:
        raise _Skip("the Artifact Detector's result could not be kept")
    field = artifacts.score_field(summary, arrays, unit["category"])
    return field, {"fingerprint": summary["fingerprint"], "reused": reused}


RUNNERS = {"blur": _run_blur, "registration": _run_registration,
           "segmentation": _run_segmentation, "artifacts": _run_artifacts}


class _Skip(Exception):
    #: Whether the scan detector this check supersedes may run instead.
    fallback = True


class _Unresolved(_Skip):
    """The cycles' DNA channels are not certain: no cycle is compared."""
    fallback = False


def _pixel_um(call, project):
    from plexora.server.utils import pixel_scale

    pixel = pixel_scale.pixel_size(call.session.project(project))
    return float(pixel["value"]) if pixel else None


def summarise(field) -> dict:
    """What a check unit keeps of its field: enough to plan and report, the
    field itself re-read by fingerprint when the review is drawn."""
    import numpy as np

    dist = score_fields.distribution(field)
    bar = score_fields.bar(field)
    found = score_fields.regions(field, bar["value"], geometry=False)
    values = field.values[np.isfinite(field.values)]
    near = int((values >= bar["value"] - bar["step"]).sum())
    return {"auto_threshold": bar["auto"], "step": bar["step"], "threshold": bar["value"],
            "threshold_source": "auto", "distribution": dist,
            "global": score_fields.global_possible(field),
            "at_auto": {"n_regions": found["n_regions"], "flagged_pct": found["flagged_pct"],
                        "denominator": found["denominator"], "near_bar": near},
            "cell_um": field.cell_um, "field_stats": _stats(field)}


def nothing_to_judge(summary) -> bool:
    """A check with no region at its bar, no place within a step below it and
    no sign of a problem everywhere: a look would show only clean tissue, so
    none is spent (the report still says what was scored)."""
    at = summary.get("at_auto") or {}
    return not at.get("n_regions") and not at.get("near_bar") \
        and not (summary.get("global") or {}).get("possible")


def _stats(field):
    stats = field.stats or {}
    if field.check == "registration":
        return {k: stats.get(k) for k in ("pattern", "highlighted_pct", "global_shift_um",
                                          "global_shift_px", "dense_mismatch_pct",
                                          "effective_threshold_px", "components")}
    if field.check == "blur":
        return {"global_blur": stats.get("global")}
    if field.check == "artifacts":
        return {k: stats.get(k) for k in ("category", "n_objects", "saturated_channels")}
    return {k: stats.get(k) for k in ("n_cells", "n_flagged", "thresholds", "d_nucleus_um")}


def run(call, session_id, announce=None) -> dict:
    """Score every pending check unit; returns {ran: [id], skipped: [{id,
    check, reason}]}. `announce(stage, message, done, total)`, when given, is
    the bulk pass's own throttled progress -- `done` blended with this
    check's own share (`index + inner_done/inner_total`), not just the
    outer count the job's own `call.progress` keeps (the QC panel's "step n
    of m" reads that one, so it stays whole numbers)."""
    from plexora.plugins.qc.server.bulk import _check_stopped
    from plexora.plugins.qc.server.engine import engine_for

    from plexora.plugins.qc.server import cycles as cycle_rules

    with engine_for(call, session_id) as engine:
        project = engine.project
        todo = [dict(u) for u in planned(engine)]
        unresolved = None
        if any(u["check"] == "registration" for u in todo):
            _ok, unresolved = cycle_rules.resolved(engine.scan(project).meta.get("cycles"))
    ran, skipped = [], []
    for index, unit in enumerate(todo):
        _check_stopped(session_id)
        check = unit["check"]

        def progress(done=0, total=1, message="", _i=index, _c=check):
            step = f" ({int(done)}/{int(total)})" if total and int(total) > 1 else ""
            call.progress(done=_i, total=len(todo), message=f"{_c} check: {message}{step}")
            if announce is not None:
                blended = _i + (float(done) / total if total else 0.0)
                announce("checks", f"{_c} check: {message}{step}", done=blended, total=len(todo))
            _check_stopped(session_id)

        def cancelled():
            _check_stopped(session_id)
            if getattr(call, "cancelled", None) is not None and call.cancelled():
                from plexora.agent.jobs import JobCancelled

                raise JobCancelled("cancelled")

        outcome = {"id": unit["id"], "check": check}
        one_cycle = []
        try:
            if check == "registration" and unresolved:
                raise _Unresolved(unresolved)
            field, about = RUNNERS[check](call, project, unit, progress, cancelled)
            one_cycle = about.pop("_one_cycle", None) or []
            if one_cycle:
                from plexora.plugins.qc.server import check_candidates

                about["one_cycle"] = check_candidates.one_cycle_summary(one_cycle)
            summary = summarise(field)
            outcome.update(state="awaiting_score_review", **about, **summary,
                           channel=unit.get("channel"), channels=unit.get("channels"))
            if nothing_to_judge(summary):
                outcome.update(state="decided", reason="nothing at or near the automatic "
                                                       "bar: no place to judge",
                               regions={"decided": 0, "to_confirm": 0, "dismissed": 0,
                                        "manual_review": 0, "residual": 0},
                               n_regions=0, flagged_pct=0.0,
                               denominator=summary["at_auto"]["denominator"])
            ran.append(unit["id"])
        except _Skip as exc:
            outcome.update(state="skipped_not_applicable", reason=str(exc))
            skipped.append({"id": unit["id"], "check": check, "reason": str(exc),
                            "fallback": exc.fallback})
        except AgentError as exc:
            outcome.update(state="skipped_not_applicable", reason=exc.message)
            skipped.append({"id": unit["id"], "check": check, "reason": exc.message})
        except Exception as exc:  # noqa: BLE001 -- a check never fails the session
            from plexora.agent.jobs import JobCancelled

            if isinstance(exc, JobCancelled):
                raise
            outcome.update(state="skipped_not_applicable", reason=f"failed: {exc}")
            skipped.append({"id": unit["id"], "check": check, "reason": f"failed: {exc}"})
        with engine_for(call, session_id) as engine:
            held = engine.record["units"].get(f"{project}::check::{unit['id']}")
            if held is None or held["state"] != "pending":
                continue
            state = outcome.pop("state")
            held.update({k: v for k, v in outcome.items() if k not in ("id", "check")})
            if one_cycle and state != "skipped_not_applicable":
                _add_one_cycle(engine, held, one_cycle)
            if state in ("skipped_not_applicable", "decided"):
                engine.close(held, state, outcome.get("reason") or "could not run")
            else:
                held["state"] = state
            from plexora.plugins.qc.server import checks_result

            checks_result.record(engine, held)
    return {"ran": ran, "skipped": skipped}


def _add_one_cycle(engine, check_unit, found_list):
    """The registration check's one-cycle places as `cycle_specific_tissue_loss`
    candidates (awaiting a confirm; probed like the check's own regions)."""
    from plexora.plugins.qc.server import check_candidates

    fresh = []
    for candidate in check_candidates.one_cycle_units(engine, check_unit, found_list):
        key = engine.unit_key_of({"project": candidate["project"], "type": "candidate",
                                  "id": candidate["id"]})
        if key in engine.record["units"]:
            continue
        engine.record["units"][key] = candidate
        fresh.append(candidate)
    check_candidates.hold_for_probes(engine, check_unit, fresh, group="one_cycle")
    summary = dict(check_unit.get("one_cycle") or {})
    summary["candidates"] = [c["id"] for c in fresh]
    check_unit["one_cycle"] = summary
    return fresh


def fallback(call, session_id, skipped) -> list:
    """Run the scan detector a skipped check would have superseded, for the
    channels it was skipped on; returns the detector names run."""
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server.bulk import add_candidates
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all
    from plexora.plugins.qc.server.engine import engine_for

    barred = {schemas.CHECK_SUPERSEDES[s["check"]]: s["reason"] for s in skipped
              if s["check"] in schemas.CHECK_SUPERSEDES and not s.get("fallback", True)}
    if barred:
        # Superseded by a check that did not run, and barred from running in
        # its place: the detector's row says the check's own reason.
        with engine_for(call, session_id) as engine:
            scanned = engine.record.setdefault("scan", {}).setdefault(engine.project, {})
            scanned["skipped_detectors"] = [
                {**s, "reason": barred[s["name"]], "superseded_by": None}
                if s.get("name") in barred else s
                for s in scanned.get("skipped_detectors") or []]
    wanted = sorted({schemas.CHECK_SUPERSEDES[s["check"]] for s in skipped
                     if s["check"] in schemas.CHECK_SUPERSEDES and s.get("fallback", True)})
    if not wanted:
        return []
    with engine_for(call, session_id) as engine:
        project = engine.project
        superseded = set(engine.record.get("superseded") or [])
        wanted = [d for d in wanted if d in superseded]
        if not wanted:
            return []
        scan = engine.scan(project)
    raw, skipped_detectors = run_all(DetectorContext(scan, project=project), enabled=wanted)
    built = cand.build(raw, scan, project=project)
    with engine_for(call, session_id) as engine:
        engine.record["superseded"] = [d for d in engine.record.get("superseded") or []
                                       if d not in wanted]
        scanned = engine.record.setdefault("scan", {}).setdefault(project, {})
        scanned["skipped_detectors"] = [
            s for s in scanned.get("skipped_detectors") or []
            if s.get("name") not in wanted] + skipped_detectors + [
            {"name": d, "reason": "ran as a fallback: its check could not run",
             "fallback": True} for d in wanted]
        add_candidates(engine, project, scan, built["ranked"], audited=True)
    return wanted


def field_of(engine, unit):
    """The ScoreField of a scored check unit, re-read by its fingerprint;
    None when the cache no longer holds it."""
    from plexora.plugins.qc.server import blur, registration, score_review

    project = unit["project"]
    fp = unit.get("fingerprint")
    if not fp:
        return None
    try:
        if unit["check"] == "blur":
            summary = blur.load_summary(project, fp)
            arrays = blur.load_arrays(project, fp)
            return score_fields.from_blur(summary, arrays) if summary and arrays is not None \
                else None
        if unit["check"] == "registration":
            entry = registration.field_entry(project, fp)
            if entry is None:
                return None
            return score_fields.from_registration(
                entry, pixel_um=_pixel_um(engine.call, project), fingerprint=fp,
                image_size=score_review.image_size(engine.call.session, project),
                reference=unit.get("reference"), comparison=unit.get("channel"),
                stats=(unit.get("field_stats") or {}))
        if unit["check"] == "artifacts":
            from plexora.plugins.qc.server import artifacts

            summary = artifacts.load_summary(project, fp)
            arrays = artifacts.load_arrays(project, fp)
            return artifacts.score_field(summary, arrays, unit["category"]) \
                if summary and arrays is not None else None
        if unit["check"] == "segmentation":
            from plexora.plugins.qc.server.segqc import run as segqc

            summary = segqc.load_summary(project, fp)
            if summary is None:
                return None
            return score_fields.from_segmentation(
                project, summary, image_size=score_review.image_size(engine.call.session,
                                                                     project))
    except AgentError:
        return None
    return None
