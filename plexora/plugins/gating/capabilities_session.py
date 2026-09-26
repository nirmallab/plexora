"""The gating session as tools: start it, fetch a decision, answer it, finish.

    gating_session_start  -> {session_id, job_id}         (the bulk pass runs as a job)
    gating_next           -> one decision packet (+ <= 2 images), or a status
    gating_answer         -> the outcome, and the next packet inline
    gating_session_status -> every unit's state, the budget, the questions
    gating_session_finish -> close | commit (propose mode) | cancel | rollback

The loop an agent runs is `gating_next`, then `gating_answer` until the answer
says `done`. Packets are self-contained, so a new conversation can pick a
session up with `gating_next(session_id)`; nothing depends on what an earlier
conversation saw.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel
from plexora.plugins.gating import PLUGIN

OWNER = "gating"
TAGS = ("gate", "gating", "threshold", "auto", "automatic", "session", "workflow",
        "phenotype", "marker")
STATE = "plugin_store:gating"


class Budget(AgentModel):
    packets: int | None = Field(3, ge=1, le=20, description="Packets per marker.")
    images: int | None = Field(4, ge=0, le=40)
    pixels: int | None = Field(2_500_000, ge=0)
    chars: int | None = Field(12_000, ge=1000)


class SessionOptions(AgentModel):
    scope: Literal["project", "dataset"] = Field(
        "project", description="project: one image; dataset: every image of a dataset, the "
                               "reference image first and gates carried to the rest.")
    project: str | None = Field(None, description="With scope=project.")
    dataset: str | None = Field(None, description="With scope=dataset: name or id.")
    markers: list[str] | None = Field(None, description="Default: every marker except "
                                      "structural channels (DNA and the like).")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: accepted gates are written as they are decided (each "
                             "receipted and undoable); propose: nothing is written until "
                             "gating_session_finish(action=commit).")
    overwrite_manual: bool = Field(False, description="Also re-gate markers the user set by "
                                   "hand (never locked or approved ones).")
    max_tier: int = Field(4, ge=1, le=5, description="1: numbers only; 2: + one look per "
                          "marker; 3: + reference channels; 4: + candidate refinement; 5: + "
                          "questions for the user.")
    audit_sheet: bool = Field(True, description="Show automatically accepted markers on a "
                                                "batched audit sheet (~70 tokens a marker).")
    reference_image: str | None = Field(None, description="Dataset scope: the image gated "
                                        "first (default: the one with the most cells).")
    mirror: bool = Field(False, description="Show each decision in an open viewer too.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(600, ge=0, le=5000)
    image_format: Literal["webp", "png"] = "webp"
    budget: Budget | None = None
    seed: int = 0


def _cap_by_name(name):
    from plexora.agent import registry

    return registry.get(name)


def _markers_for(call, images, wanted):
    from plexora.plugins.gating.server.autogate import context

    first = call.session.data(images[0])
    panel = context.for_project(first)
    available = list(dict.fromkeys(m for p in images for m in call.session.data(p).table.markers))
    if wanted:
        unknown = [m for m in wanted if m not in available]
        if unknown:
            raise AgentError("invalid_input", f"not markers of this scope: {unknown}",
                             detail={"markers": available[:MAX_LIST]})
        chosen = list(wanted)
    else:
        chosen = [m for m in available
                  if (panel["entries"].get(m) or {}).get("role") != "context"]
    skipped = [m for m in available if m not in chosen]
    order = [m for m in panel["order"] if m in chosen] + \
        [m for m in chosen if m not in panel["order"]]
    return order, skipped, panel


def start(call, inp):
    from plexora.agent.sessions.store import new_session_id
    from plexora.plugins.gating.server.autogate import engine as engines

    if inp.scope == "project":
        if not inp.project:
            raise AgentError("invalid_input", "scope=project needs `project`")
        call.session.project(inp.project)
        images = [inp.project]
        reference = inp.project
        dataset_name = None
    else:
        if not inp.dataset:
            raise AgentError("invalid_input", "scope=dataset needs `dataset`")
        from plexora import datasets

        try:
            cohort = datasets.dataset(inp.dataset)
        except KeyError as exc:
            raise AgentError("invalid_input", str(exc.args[0]),
                             detail={"hint": "list_datasets"}) from None
        requires = PLUGIN.requires
        images = []
        for name in cohort.projects:
            record = call.session.project(name)
            if requires.applies_to(record) and not requires.missing_from(record):
                images.append(name)
        if not images:
            raise AgentError("precondition_missing", f"{cohort.name!r} has no gateable images")
        dataset_name = cohort.name
        if inp.reference_image:
            if inp.reference_image not in images:
                raise AgentError("invalid_input", f"{inp.reference_image!r} is not a gateable "
                                 "image of this dataset", detail={"images": images})
            reference = inp.reference_image
        else:
            sizes = {name: call.session.data(name).table.geometry().height for name in images}
            reference = max(images, key=lambda n: (sizes[n], -images.index(n)))
        images = [reference] + [n for n in images if n != reference]
    if len(images) > 1:
        # The reference image stays held while the bulk pass walks the rest;
        # an evicted table would refit every marker for every packet.
        call.session.table_limit = max(call.session.table_limit, 4)
    order, skipped, panel = _markers_for(call, images, inp.markers)
    if not order:
        raise AgentError("invalid_input", "no markers to gate")
    session_id = new_session_id()
    units = {}
    for project in images:
        markers = call.session.data(project).table.markers
        for marker in order:
            units[engines.unit_key(project, marker)] = {
                "project": project, "marker": marker,
                "state": "pending" if marker in markers else "skipped_no_marker"}
    unresolved = [m for m in panel["unresolved"] if m in order]
    record = {
        "session_id": session_id, "kind": "gating", "created_at": _now(),
        "principal": getattr(call.policy, "principal", None) or "agent",
        "options": inp.model_dump(mode="json"), "scope": inp.scope,
        "dataset": dataset_name, "state": "created", "operation_id": call.operation_id,
        "images": images, "reference_image": reference, "order": order,
        "skipped_markers": skipped, "units": units, "panel_hash": panel["panel_hash"],
        "panel_pending": bool(unresolved) and inp.max_tier >= 2,
        "used": {"packets": 0, "images": 0, "pixels": 0, "chars": 0},
        "receipts": [], "questions": [], "packet_seq": 0, "write_seq": 0,
        "mirror": {"enabled": inp.mirror, "view_id": inp.view_id,
                   "status": "pending" if inp.mirror else "off", "last_error": None},
    }
    st = engines.store()
    st.create(record)
    st.sweep()
    receipt = make_receipt(call, changed=False, before=None,
                           after={"session_id": session_id, "images": images,
                                  "markers": order},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": session_id, "note": "the session's "
                                  "writes are receipted as <operation_id>.<nnn>"})
    job = _submit_bulk(call, session_id)
    with engines.engine_for(call, session_id) as engine:
        engine.record["bulk_job_id"] = job["job_id"]
    _announce(call, images[0], session_id, "started")
    return {"session_id": session_id, "job_id": job["job_id"], "scope": inp.scope,
            "images": images, "reference_image": reference, "order": order,
            "n_units": len(units), "skipped_markers": skipped,
            "unresolved_markers": unresolved, "mode": inp.mode,
            "receipt": receipt.model_dump(mode="json"),
            "resource": f"plexora://gating/session/{session_id}",
            "next": "gating_next(session_id) -- packets start as soon as the first markers "
                    "are profiled; answer each with gating_answer"}


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def _submit_bulk(call, session_id):
    from plexora.agent import jobs

    capability = _cap_by_name("gating.session_bulk")
    child = dataclasses.replace(call, capability=capability,
                                operation_id=f"{call.operation_id}.bulk",
                                arguments={"session_id": session_id}, receipted=False,
                                extras=dict(call.extras))
    return jobs.submit(child, BulkInput(session_id=session_id))


def _announce(call, project, session_id, kind):
    """Tell an open viewer a session targets it (the pause pill listens)."""
    if call.notify is None:
        return
    try:
        call.notify(project, OWNER, "gating.session", {"session_id": session_id,
                                                        "event": kind})
    except Exception:
        pass


class BulkInput(AgentModel):
    session_id: str


def bulk(call, inp):
    from plexora.plugins.gating.server.autogate import bulk as bulk_pass

    return bulk_pass.run(call, inp)


# -- next / answer ------------------------------------------------------------


class NextInput(AgentModel):
    session_id: str
    wait_s: float = Field(10.0, ge=0, le=30, description="How long to wait for the bulk "
                                                         "pass when nothing is ready yet.")


def _packet_result(packet, images):
    from plexora.plugins.gating.server.autogate.engine import TERMINAL  # noqa: F401

    return {"state": "decision", "packet": packet,
            "_images": [{"data": data, "format": fmt} for data, fmt in images]}


def _paused(st, session_id):
    control = st.control(session_id)
    if control.get("paused"):
        return {"state": "paused", "by": control.get("paused_by"), "retry_after_s": 10,
                "note": "the user paused this session in the viewer"}
    return None


def next_packet(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    paused = _paused(st, inp.session_id)
    if paused:
        return paused
    st.claim(inp.session_id)
    deadline = time.monotonic() + float(inp.wait_s)
    while True:
        with engines.engine_for(call, inp.session_id, st=st) as engine:
            record = engine.record
            if record["state"] in ("done", "cancelled", "rolled_back", "failed"):
                return {"state": record["state"], "progress": engine.progress()}
            outstanding = record.get("outstanding_packet")
            if outstanding:
                packet, images = st.read_packet(inp.session_id, outstanding)
                return _packet_result(packet, images)
            packet, images, status = engine.issue()
            progress = engine.progress()
            state = record["state"]
            mirror = dict(record.get("mirror") or {})
        if status == "packet":
            if mirror.get("enabled") and mirror.get("status") != "off":
                _mirror(call, inp.session_id, packet)
            return _packet_result(packet, images)
        if status == "done":
            if state != "bulk_running":
                return {"state": "decided", "progress": progress,
                        "next": "gating_session_finish(session_id, action='close'), then "
                                "gating_report"}
        if time.monotonic() >= deadline:
            return {"state": "bulk_running", "progress": progress,
                    "job_id": engine.record.get("bulk_job_id"),
                    "next": "call gating_next again; the deterministic pass is still "
                            "profiling the next marker"}
        time.sleep(0.5)


def _mirror(call, session_id, packet):
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import mirror_script

    try:
        result = mirror_script.run(call, session_id, packet)
    except Exception as exc:  # mirroring never breaks a session
        result = {"status": "degraded", "errors": [{"message": str(exc)}]}
    with engines.engine_for(call, session_id) as engine:
        mirror = engine.record.setdefault("mirror", {})
        mirror["status"] = result.get("status")
        mirror["last_error"] = (result.get("errors") or [None])[0]
        mirror["view_id"] = result.get("view_id") or mirror.get("view_id")


class AnswerInput(AgentModel):
    session_id: str
    packet_id: str = Field(description="The packet being answered (pk_nnnn).")
    answer: dict[str, Any] = Field(description="The typed answer: `kind` plus the fields the "
                                   "packet's answer_schema lists.")
    include_next: bool = Field(True, description="Return the next packet with the outcome, "
                                                 "saving a gating_next call.")


def answer(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    paused = _paused(st, inp.session_id)
    if paused:
        return paused
    st.claim(inp.session_id)
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        receipts_before = len(engine.record.get("receipts") or [])
        try:
            outcome = engine.apply(inp.packet_id, inp.answer)
        except AgentError as exc:
            if exc.code == "invalid_input":
                exc.save = True
            raise
        receipts = (engine.record.get("receipts") or [])[receipts_before:]
        progress = engine.progress()
    result = {"applied": not outcome.get("already_applied"), "outcome": outcome,
              "receipts": receipts, "progress": progress}
    if inp.include_next and not outcome.get("already_applied"):
        following = next_packet(call, NextInput(session_id=inp.session_id, wait_s=5.0))
        images = following.pop("_images", None)
        result["next"] = following
        if images:
            result["_images"] = images
    return result


# -- status / finish ----------------------------------------------------------


class StatusInput(AgentModel):
    session_id: str | None = Field(None, description="Omit to list recent sessions.")
    reattach_viewer: bool = False
    pause: bool | None = Field(None, description="Pause (true) or resume (false) the "
                                                 "session.")


def _unit_row(unit):
    return {k: unit.get(k) for k in ("project", "marker", "state", "confidence", "final",
                                     "gmm", "tier", "class", "reason") if unit.get(k)
            is not None}


def status(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import transfer

    st = engines.store()
    if inp.session_id is None:
        return {"sessions": [{k: r.get(k) for k in ("session_id", "created_at", "state",
                                                     "scope", "images", "dataset")}
                             for r in st.list()]}
    if inp.pause is not None:
        st.set_control(inp.session_id, paused=bool(inp.pause),
                       paused_by="agent" if inp.pause else None)
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        if inp.reattach_viewer:
            record.setdefault("mirror", {}).update(status="pending", enabled=True)
        units = [_unit_row(u) for u in record["units"].values()]
        out = {"session_id": inp.session_id, "state": record["state"],
               "scope": record.get("scope"), "mode": record["options"]["mode"],
               "images": record["images"], "reference_image": record.get("reference_image"),
               "progress": engine.progress(), "units": units[:MAX_LIST],
               "truncated": len(units) > MAX_LIST, "used": record.get("used"),
               "estimated_vision_tokens": -(-int((record.get("used") or {}).get("pixels", 0))
                                            // 750),
               "questions": record.get("questions") or [], "mirror": record.get("mirror"),
               "control": st.control(inp.session_id),
               "outstanding_packet": record.get("outstanding_packet"),
               "receipts": len(record.get("receipts") or [])}
        if record.get("scope") == "dataset":
            out["dataset"] = transfer.dataset_summary(engine)
    return out


class FinishInput(AgentModel):
    session_id: str
    action: Literal["close", "commit", "cancel", "rollback"] = Field(
        "close", description="close: end the session (writes stay); commit: write a "
                             "propose-mode session's accepted gates; cancel: stop the bulk "
                             "pass (writes stay); rollback: undo every write the session "
                             "made, newest first.")


def finish(call, inp):
    from plexora.agent import jobs, registry
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        out = {"session_id": inp.session_id, "action": inp.action}
        if inp.action == "cancel":
            job_id = record.get("bulk_job_id")
            if job_id:
                out["job"] = jobs.store().cancel(job_id)
            record["state"] = "cancelled"
        elif inp.action == "commit":
            if record["options"]["mode"] != "propose":
                raise AgentError("invalid_input", "commit is for a propose-mode session")
            record["options"]["mode"] = "apply"
            written = []
            for unit in record["units"].values():
                if unit.get("proposed") is not None and unit["state"] in (
                        "accepted", "accepted_low_confidence"):
                    op = engine.write(unit, unit["proposed"],
                                      method=unit.get("method") or "ai_accepted",
                                      confidence=unit.get("confidence") or "low",
                                      state=unit["state"], tier=unit.get("tier") or "T2")
                    if op:
                        written.append(op)
            out["written"] = written
            record["options"]["mode"] = "propose"
            record["state"] = "done"
        elif inp.action == "rollback":
            undone, refused = [], []
            for operation_id in reversed(record.get("receipts") or []):
                answer_ = registry.invoke(call.session, "undo_operation",
                                          {"operation_id": operation_id}, policy=call.policy,
                                          audit=call.audit, link=call.link, notify=call.notify,
                                          operation_id=f"{call.operation_id}.u{len(undone):03d}")
                if answer_["ok"]:
                    undone.append(operation_id)
                else:
                    refused.append({"operation_id": operation_id,
                                    "reason": answer_["error"]["message"]})
            out.update(undone=undone, refused=refused)
            record["state"] = "rolled_back"
        else:
            if record["state"] == "bulk_running":
                raise AgentError("conflict", "the bulk pass is still running; cancel it or "
                                 "wait for it", retryable=True)
            record["state"] = "done"
        record["finished_at"] = _now()
        record["outstanding_packet"] = None
        out["progress"] = engine.progress()
        out["units"] = [_unit_row(u) for u in record["units"].values()][:MAX_LIST]
        out["questions"] = record.get("questions") or []
    st.release(inp.session_id)
    receipt = make_receipt(call, changed=inp.action in ("commit", "rollback"),
                           before=None, after={k: out.get(k) for k in ("action", "written",
                                                                          "undone")},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": inp.session_id})
    out["receipt"] = receipt.model_dump(mode="json")
    out["next"] = "gating_report(session_id) for the review report; export_gates for CSV"
    return out


# -- across a dataset ---------------------------------------------------------


class CompareInput(AgentModel):
    dataset: str
    markers: list[str] | None = None
    reference_image: str | None = None


def compare(call, inp):
    """Per marker and image: aligned vs own gate, drift class, strategy."""
    from plexora import datasets
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import reference as refmod
    from plexora.plugins.gating.server.autogate import tableops

    try:
        cohort = datasets.dataset(inp.dataset)
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc.args[0])) from None
    images = [p for p in cohort.projects
              if PLUGIN.requires.applies_to(call.session.project(p))
              and not PLUGIN.requires.missing_from(call.session.project(p))]
    if len(images) < 2:
        raise AgentError("precondition_missing", "comparing gates needs at least two images")
    summaries = {}
    total = len(images)
    for index, project in enumerate(images, start=1):
        call.check_cancelled()
        ds = call.session.data(project)
        markers = [m for m in (inp.markers or ds.table.markers) if m in ds.table.markers]
        per = {}
        for marker in markers:
            profile = tableops.local_or_node(ds, "gating.autogate.profile",
                                             {"marker": marker, "with_cell_qc": False})
            gate = model.get_gate(ds, marker)
            per[marker] = {"summary": engines.compact_profile(profile),
                           "metrics": engines._metrics(profile),
                           "class": profile.get("distribution_class"),
                           "fit_space": profile.get("fit_space"),
                           "gate": gate["low"] if gate["thresholded"] else None,
                           "gmm": (profile.get("fit") or {}).get("gate_raw"),
                           "n": ds.table.geometry().height}
        summaries[project] = per
        call.progress(done=index, total=total, message=f"profiled {project}")
    reference_image = inp.reference_image or max(
        images, key=lambda p: (max((v["n"] for v in summaries[p].values()), default=0),
                               -images.index(p)))
    out = {}
    for marker in sorted({m for per in summaries.values() for m in per}):
        ref = summaries[reference_image].get(marker)
        if ref is None:
            continue
        ref_gate = ref["gate"] if ref["gate"] is not None else ref["gmm"]
        rows = []
        for project in images:
            item = summaries[project].get(marker)
            if item is None:
                continue
            if project == reference_image:
                rows.append({"project": project, "class": "reference", "gate": ref_gate})
                continue
            alignment = refmod.align(item["summary"], ref["summary"], item["fit_space"])
            predicted = refmod.predict(alignment, ref_gate, item["fit_space"]) \
                if alignment and ref_gate is not None else None
            klass = refmod.classify(alignment, item, ref)
            sd = (ref["metrics"] or {}).get("sd_bg") or 1.0
            shift = ((alignment["a"] + (alignment["b"] - 1.0)
                      * ((ref["metrics"] or {}).get("mu_bg") or 0.0)) / sd
                     if alignment else None)
            rows.append({"project": project, "class": klass, "shift_bg_sd": shift,
                         "aligned_gate": predicted, "own_gate": item["gate"],
                         "own_gmm": item["gmm"], "alignment": alignment})
        refined = refmod.dataset_classes(
            [{**r, "shift": r.get("shift_bg_sd")} for r in rows if r["class"] != "reference"])
        out[marker] = {"reference_gate": ref_gate, "images": rows,
                       "classes": [r["class"] for r in refined],
                       "strategy": refmod.strategy([r["class"] for r in refined])}
    return {"dataset": cohort.name, "reference_image": reference_image, "markers": out,
            "experimental_unit": "image", "n_images": len(images),
            "note": "classes are relative to the reference image; gates in each table's "
                    "own units"}


# -- export -------------------------------------------------------------------


class ExportInput(AgentModel):
    project: str | None = None
    dataset: str | None = None
    include_provenance: bool = True


def export(call, inp):
    from plexora.plugins.gating.server.autogate import report

    if bool(inp.project) == bool(inp.dataset):
        raise AgentError("invalid_input", "give exactly one of project or dataset")
    return report.export_gates(call, project=inp.project, dataset=inp.dataset,
                               include_provenance=inp.include_provenance)


class ReportInput(AgentModel):
    session_id: str
    format: Literal["html", "pdf", "both"] = "both"


def gating_report(call, inp):
    from plexora.plugins.gating.server.autogate import report

    return report.write_report(call, inp.session_id, fmt=inp.format)


def capabilities():
    requires = PLUGIN.requires

    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        cap(name="gating.session_start", tool_name="gating_session_start",
            purpose="Gate a whole image or dataset automatically. Starts a gating session: "
                    "every marker is profiled and settled deterministically where the "
                    "numbers suffice (a job), and the rest become decision packets for you, "
                    "one at a time, through gating_next / gating_answer.",
            permission="reversible_write", input_model=SessionOptions, handler=start,
            writes=("gates",), persistent=True, egress="metadata",
            reads=("table", "gates", "image")),
        cap(name="gating.session_bulk", tool_name="gating_session_bulk",
            purpose="The deterministic pass of a gating session (started by "
                    "gating_session_start; call it yourself only to resume one).",
            permission="reversible_write", input_model=BulkInput, handler=bulk,
            writes=("gates",), persistent=True, execution="job", egress="aggregates",
            reads=("table", "gates", "image")),
        cap(name="gating.next", tool_name="gating_next",
            purpose="The session's next decision packet: one question, compact numbers, at "
                    "most two small images, and the answer schema. The same packet again "
                    "if it is still unanswered.",
            permission="reversible_write", input_model=NextInput, handler=next_packet,
            writes=("gates",), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("table", "gates", "image", "mask")),
        cap(name="gating.answer", tool_name="gating_answer",
            purpose="Answer the outstanding packet with a typed judgement; the server moves "
                    "the marker on (and writes its gate when it is decided) and returns the "
                    "next packet.",
            permission="reversible_write", input_model=AnswerInput, handler=answer,
            writes=("gates",), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("table", "gates", "image", "mask")),
        cap(name="gating.session_status", tool_name="gating_session_status",
            purpose="A gating session's units (state, confidence, gate), what it has spent, "
                    "its open questions, mirroring; or the recent sessions. Can pause or "
                    "resume it.",
            permission="read", input_model=StatusInput, handler=status, egress="aggregates"),
        cap(name="gating.session_finish", tool_name="gating_session_finish",
            purpose="Finish a gating session: close it, commit a propose-mode session's "
                    "gates, cancel its bulk pass, or roll back every gate it wrote.",
            permission="reversible_write", input_model=FinishInput, handler=finish,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.compare_images", tool_name="compare_gates_across_images",
            purpose="One marker across a dataset's images: each image's intensities aligned "
                    "to a reference image's, the reference gate carried through, the drift "
                    "class (stable, drift, batch, image-specific, changed, failed) and the "
                    "strategy it implies. A job; the image is the unit.",
            permission="read", input_model=CompareInput, handler=compare, execution="job",
            requires=None, egress="aggregates", reads=("table", "gates"),
            tags=TAGS + ("dataset", "cohort", "batch", "drift", "compare")),
        cap(name="gating.export", tool_name="export_gates",
            purpose="Write a project's or dataset's gates (with provenance) to CSV files "
                    "under Plexora's data directory, for other tools.",
            permission="read", input_model=ExportInput, handler=export, egress="aggregates",
            reads=("gates",), tags=TAGS + ("export", "csv", "download")),
        cap(name="gating.report", tool_name="gating_report",
            purpose="The review report of a gating session, HTML and/or PDF: per marker the "
                    "final gate, the GMM proposal, confidence, flags, the distribution and "
                    "the near-gate cells; for a dataset the spread across images.",
            permission="read", input_model=ReportInput, handler=gating_report,
            egress="rendered_pixels", reads=("gates",),
            tags=TAGS + ("report", "pdf", "html", "review", "provenance")),
    ]
