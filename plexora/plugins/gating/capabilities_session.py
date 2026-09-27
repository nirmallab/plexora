"""The gating session as tools: start it, fetch a decision, answer it, finish.

    gating_session_start  -> {session_id, job_id}         (the bulk pass runs as a job)
                             or `needs_setup` + an `expression_setup` packet, when which
                             matrix to gate cannot be settled from the values
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
from plexora.agent.registry import Capability, tool_name_of
from plexora.agent.schemas import AgentModel
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions import mirror as mirroring
from plexora.plugins.gating import PLUGIN
from plexora.plugins.gating.server.autogate import schemas

OWNER = "gating"
TAGS = ("gate", "gating", "threshold", "auto", "automatic", "session", "workflow",
        "phenotype", "marker")
STATE = "plugin_store:gating"


def _allowance(key, description):
    low, high = budgets.UNIT_BOUNDS[key]
    return Field(budgets.UNIT_DEFAULT[key], ge=low, le=high, description=description)


class Budget(AgentModel):
    packets: int | None = _allowance("packets", "Looks per marker (first look, beside a "
                                                "reference, candidates); confirmations "
                                                "are free.")
    images: int | None = _allowance("images", "Images per marker, over its looks.")
    pixels: int | None = _allowance("pixels", "Image pixels per marker, over its looks.")
    chars: int | None = _allowance("chars", "Packet JSON characters per marker, over its "
                                            "looks.")


class SessionOptions(AgentModel):
    scope: Literal["project", "dataset"] = Field(
        "project", description="project: one image; dataset: every image of a dataset, the "
                               "reference image first and gates carried to the rest.")
    project: str | None = Field(None, description="With scope=project.")
    dataset: str | None = Field(None, description="With scope=dataset: name or id.")
    markers: list[str] | None = Field(None, description="Default: every marker except "
                                      "structural channels (DNA and the like).")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: accepted gates -- and the empty gate of a failed or "
                             "all-negative marker -- are written as they are decided (each "
                             "receipted and undoable); propose: nothing is written until "
                             "gating_session_finish(action=commit).")
    overwrite_manual: bool = Field(False, description="Also re-gate markers the user set by "
                                   "hand (never locked or approved ones).")
    max_tier: int = Field(schemas.ENGINE["max_tier_default"], ge=1, le=len(schemas.TIERS),
                          description="1: numbers only; 2: + one look per marker; 3: + "
                                      "reference channels; 4: + candidate refinement; 5: + "
                                      "questions for the user.")
    audit_sheet: bool = Field(True, description="Show automatically accepted markers on a "
                                                "batched audit sheet (~70 tokens a marker).")
    reference_image: str | None = Field(None, description="Dataset scope: the image gated "
                                        "first (default: the one with the most cells).")
    mirror: bool = Field(False, description="Show each decision in an open viewer too.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(mirroring.DEFAULT_DELAY_MS, ge=0, le=5000)
    image_format: Literal["webp", "png"] = "webp"
    budget: Budget = Field(default_factory=Budget, description="What each marker may spend.")
    seed: int = 0


def option_defaults() -> dict:
    """Every session option at its default (what an older session's stored
    options are completed with)."""
    return SessionOptions.model_construct().model_dump(mode="json")


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
    expression, expression_receipts = _expression_check(call, images)
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
        "receipts": list(expression_receipts), "questions": [], "packet_seq": 0,
        "write_seq": 0, "mirror": _mirror_at_start(call, inp), "expression": expression,
    }
    if expression.get("status") == "pending":
        record["state"] = "needs_setup"
    st = engines.store()
    st.create(record)
    st.sweep()
    receipt = make_receipt(call, changed=False, before=None,
                           after={"session_id": session_id, "images": images,
                                  "markers": order},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": session_id, "note": "the session's "
                                  "writes are receipted as <operation_id>.<nnn>"})
    packet = images_ = None
    if record["state"] == "needs_setup":
        # Nothing is profiled until the matrix is settled: every number the
        # bulk pass computes would be of the wrong values.
        job = {"job_id": None}
        with engines.engine_for(call, session_id) as engine:
            packet, images_, _status = engine.issue()
            phase = engines.phase_for(engine.record)
            progress = engine.progress()
        _announce(call, record, session_id, "started", phase=phase, progress=progress,
                  order=order, images=images, mode=inp.mode,
                  view_id=record["mirror"].get("view_id"))
        _announce(call, record, session_id, "needs_setup", phase="planning",
                  view_id=record["mirror"].get("view_id"),
                  needs=_setup_needs(images, expression))
    else:
        job = _submit_bulk(call, session_id)
        with engines.engine_for(call, session_id) as engine:
            engine.record["bulk_job_id"] = job["job_id"]
            phase = engines.phase_for(engine.record)
            progress = engine.progress()
        _announce(call, record, session_id, "started", phase=phase, progress=progress,
                  order=order, images=images, mode=inp.mode,
                  view_id=record["mirror"].get("view_id"))
    out = {"session_id": session_id, "job_id": job["job_id"], "scope": inp.scope,
            "images": images, "reference_image": reference, "order": order,
            "n_units": len(units), "skipped_markers": skipped,
            "unresolved_markers": unresolved, "mode": inp.mode,
            "mirror": {k: record["mirror"].get(k) for k in ("status", "view_id", "last_error",
                                                            "hint") if record["mirror"].get(k)},
            "receipt": receipt.model_dump(mode="json"),
            "resource": session_uri(session_id), "expression": expression,
            "state": record["state"],
            "next": f"{tool_name_of('gating.next')}(session_id) -- packets start as soon as "
                    "the first markers are profiled; answer each with "
                    f"{tool_name_of('gating.answer')}"}
    if packet is not None:
        out["packet"] = packet
        out["_images"] = [{"data": data, "format": fmt} for data, fmt in images_ or []]
        out["next"] = ("answer the expression_setup packet with the user's choice "
                       f"({tool_name_of('gating.answer')}), or let them confirm the "
                       "expression source in the viewer; then "
                       f"{tool_name_of('gating.next')}(session_id)")
    return out


def _expression_check(call, images):
    """Settle which matrix each image is gated on, before anything is
    profiled: a confirmed source is used as it is; a certain recommendation is
    applied (receipted, undoable); anything else waits on the user.

    Returns (expression record, receipt ids)."""
    from plexora.agent import registry

    applied, pending, found = [], [], {}
    receipts = []
    for index, project in enumerate(images):
        answer_ = registry.invoke(call.session, "inspect_expression_sources",
                                  {"project": project}, policy=call.policy,
                                  audit=call.audit, link=call.link)
        if not answer_["ok"]:
            continue          # nothing to read here (a node-bound table): used as it is
        result = answer_["result"]
        if result["current"]["confirmed"]:
            continue
        recommendation = result["recommendation"]
        choice = recommendation.get("choice")
        if recommendation["confidence"] == "certain" and choice:
            done = registry.invoke(call.session, "set_expression_source",
                                   {"project": project, **choice, "confirm": True},
                                   policy=call.policy, audit=call.audit, link=call.link,
                                   notify=call.notify,
                                   operation_id=f"{call.operation_id}.expr{index + 1}")
            if done["ok"]:
                receipt = (done["result"] or {}).get("receipt") or {}
                if receipt.get("changed") and receipt.get("operation_id"):
                    receipts.append(receipt["operation_id"])
                applied.append({"project": project, "choice": choice,
                                "why": recommendation["why"]})
                continue
        pending.append(project)
        found[project] = result
    if pending:
        first = found[pending[0]]
        common = [o["value"] for o in first["options"]
                  if all(o["value"] in [x["value"] for x in found[p]["options"]]
                         for p in pending)]
        return {"status": "pending", "projects": pending, "applied": applied,
                "source_kind": first["source_kind"], "current": first["current"],
                "options": [o for o in first["options"] if o["value"] in common],
                "recommendation": first["recommendation"], "rule": first["rule"]}, receipts
    if applied:
        return {"status": "applied", "applied": applied}, receipts
    return {"status": "confirmed"}, receipts


def _setup_needs(images, expression):
    from plexora.api.plugin import requirement

    return {"tool": OWNER, "project": (expression.get("projects") or images)[0],
            "projects": expression.get("projects") or images, "keys": ["features"],
            "requirements": [requirement("features").describe()]}


def _settle_expression(call, session_id, st):
    """A session waiting on the expression source moves on once it is settled
    -- by the agent's answer, or by the user in the viewer's modal."""
    from plexora.plugins.gating.server.autogate import engine as engines

    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if record["state"] != "needs_setup":
            return False
        expression = record.setdefault("expression", {})
        if expression.get("status") == "pending":
            projects = expression.get("projects") or record["images"]
            records = [call.session.project(p) for p in projects]
            if not all("features" in set(r.confirmed) for r in records):
                return False
            for name in projects:
                call.session.invalidate(name)
            expression.update(status="applied_by_user",
                              choice={"features_layer": records[0].feature_source,
                                      "features_log": bool(records[0].log_transformed)},
                              why="confirmed by the user in the viewer")
        if record.get("outstanding_kind") in schemas.USER_SETUP_KINDS:
            record["outstanding_packet"] = None
            record["outstanding_kind"] = None
        record["state"] = "created"
        record["bulk_job_id"] = None
        progress = engine.progress()
    _announce(call, record, session_id, "phase", phase="analyzing", progress=progress)
    return True


def session_uri(session_id):
    """The MCP resource a session is readable at (`plexora.mcp.resources_gating`)."""
    return f"plexora://gating/session/{session_id}"


def _mirror_at_start(call, inp):
    """The session's mirror, probed once: `pending` when a tab can be driven,
    `off` (and why) when none can -- a viewer that predates agent control,
    no tab open, no server attached."""
    mirror = {"enabled": inp.mirror, "view_id": inp.view_id, "status": "off",
              "last_error": None}
    if not inp.mirror:
        return mirror
    from plexora.agent import viewer

    try:
        view = viewer.resolve_view(viewer.require(call.link), inp.view_id)
    except AgentError as exc:
        mirror["last_error"] = {"code": exc.code, "message": exc.message}
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        mirror["hint"] = ((detail.get("hint") or viewer.NOT_AVAILABLE_HINT)
                          + f"; then {tool_name_of('gating.session_status')}(session_id, "
                            "reattach_viewer=true)")
        return mirror
    mirror.update(status="pending", view_id=view.get("view_id") or inp.view_id)
    return mirror


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


def _announce(call, record, session_id, event, /, **payload):
    """Tell every tab open on one of the session's images what the session is
    doing (`schemas.SESSION_EVENTS`; core's agent panel listens)."""
    from plexora.plugins.gating.server.autogate import events

    images = record.get("images") if isinstance(record, dict) else [record]
    events.announce(call.notify, images, session_id, event, **payload)


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
    rerender: bool = Field(False, description="Draw the outstanding packet's images again "
                           "(same packet, same charge) -- after a renderer change.")


def _packet_result(packet, images):
    state = "needs_setup" if packet.get("kind") in schemas.USER_SETUP_KINDS else "decision"
    return {"state": state, "packet": packet,
            "_images": [{"data": data, "format": fmt} for data, fmt in images]}


def _halted(call, st, session_id):
    """`stopped` (the user stopped the session in the viewer) or `paused`,
    or None when the session may go on."""
    control = st.control(session_id)
    if control.get("stopped"):
        from plexora.agent import jobs

        try:
            record = st.load(session_id)
        except Exception:
            record = {}
        if record.get("state") == "bulk_running" and record.get("bulk_job_id"):
            jobs.store().cancel(record["bulk_job_id"])
        finish = tool_name_of("gating.session_finish")
        return {"state": "stopped", "by": control.get("stopped_by"),
                "note": "the user stopped this session in the viewer",
                "next": f"{finish}(session_id, action='close') keeps the gates written so "
                        f"far; {finish}(session_id, action='rollback') undoes them. Then stop."}
    if control.get("paused"):
        return {"state": "paused", "by": control.get("paused_by"), "retry_after_s": 10,
                "note": "the user paused this session in the viewer"}
    return None


#: The last phase each session announced from this process (so a wait loop
#: says "analyzing" once, not every half second).
_LAST_PHASE: dict = {}


def _phase(call, record, session_id, phase, /, **payload):
    if _LAST_PHASE.get(session_id) == phase and not payload:
        return
    _LAST_PHASE[session_id] = phase
    _announce(call, record, session_id, "phase", phase=phase, **payload)


def _mirroring(mirror) -> bool:
    return bool(mirror.get("enabled")) and mirror.get("status") != "off"


def next_packet(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    halted = _halted(call, st, inp.session_id)
    if halted:
        return halted
    st.claim(inp.session_id)
    _settle_expression(call, inp.session_id, st)
    _resume_bulk(call, inp.session_id, st)
    deadline = time.monotonic() + float(inp.wait_s)
    while True:
        with engines.engine_for(call, inp.session_id, st=st) as engine:
            record = engine.record
            if record["state"] in schemas.FINISHED_STATES:
                return {"state": record["state"], "progress": engine.progress()}
            outstanding = record.get("outstanding_packet")
            mirror = dict(record.get("mirror") or {})
            if outstanding:
                packet, images = st.read_packet(inp.session_id, outstanding)
                if inp.rerender or len(images) < len(packet.get("images") or []):
                    packet, images = engine.rerender(outstanding)
                    fresh = packet is not None
                else:
                    fresh = False
                if packet is not None:
                    status = "again"
            if not outstanding or packet is None:
                packet, images, status = engine.issue()
                fresh = status == "packet"
            progress = engine.progress()
            state = record["state"]
            snapshot = {"images": record["images"], "state": state,
                        "outstanding_kind": record.get("outstanding_kind"),
                        "panel_pending": record.get("panel_pending"),
                        "units": record["units"]}
        if status in ("packet", "again"):
            kind = packet.get("kind")
            mirrors = _mirroring(mirror)
            if fresh:
                refs = packet.get("units") or []
                phase = engines.phase_for(snapshot, mirroring=mirrors)
                _LAST_PHASE[inp.session_id] = phase
                _announce(call, snapshot, inp.session_id, "issued",
                          packet_id=packet.get("packet_id"), kind=kind,
                          marker=refs[0]["marker"] if len(refs) == 1 else None,
                          markers=[r["marker"] for r in refs],
                          project=refs[0]["project"] if refs else None,
                          subject=_subject(packet), phase=phase, progress=progress)
            if mirrors and (fresh or mirror.get("status") in ("pending", "degraded")):
                packet["mirror"] = _mirror(call, inp.session_id, packet)
            elif mirror.get("enabled"):
                packet["mirror"] = {**_mirror_brief(mirror),
                                    **({"resent": False} if status == "again" else {})}
            if fresh and mirrors and kind in schemas.LOOK_KINDS:
                _phase(call, snapshot, inp.session_id, "thinking")
            return _packet_result(packet, images)
        if status == "wait":
            _phase(call, snapshot, inp.session_id, "analyzing")
        if status == "done":
            if state != "bulk_running":
                _phase(call, snapshot, inp.session_id, "summarizing")
                return {"state": "decided", "progress": progress,
                        "next": f"{tool_name_of('gating.session_finish')}(session_id, "
                                f"action='close'), then {tool_name_of('gating.report')}"}
        if time.monotonic() >= deadline:
            return {"state": "bulk_running", "progress": progress,
                    "job_id": engine.record.get("bulk_job_id"),
                    "next": f"call {tool_name_of('gating.next')} again; the deterministic "
                            "pass is still profiling the next marker"}
        time.sleep(0.5)


def _subject(packet):
    """What a packet is about, in a few words (the panel's phase line)."""
    refs = packet.get("units") or []
    kind = packet.get("kind")
    if kind == "expression_setup":
        return "expression source"
    if kind == "panel_context":
        return "panel context"
    if len(refs) == 1:
        return refs[0]["marker"]
    return f"{len(refs)} markers" if refs else None


def _resume_bulk(call, session_id, st):
    """A session whose bulk pass is no longer running anywhere -- the MCP
    server that ran it was restarted -- gets it again. The pass skips every
    unit already profiled, so only the rest is redone."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.autogate import engine as engines

    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if st.control(session_id).get("stopped"):
            return None       # a stopped pass is not resubmitted
        if record["state"] != "bulk_running" and not (
                record["state"] == "created" and record.get("bulk_job_id") is None):
            return None
        job = jobs.store().get(record.get("bulk_job_id") or "") if record.get(
            "bulk_job_id") else None
        if job is not None and job.get("status") in ("queued", "running", "done"):
            return None
        job = _submit_bulk(call, session_id)
        record["bulk_job_id"] = job["job_id"]
        record.setdefault("bulk_resumed", []).append(job["job_id"])
        return job["job_id"]


def _mirror_brief(mirror):
    return {k: mirror.get(k) for k in ("status", "view_id", "last_error", "sent")}


def _mirror(call, session_id, packet):
    """Show the packet in the session's viewer; returns what the agent is
    told about it (`packet["mirror"]`), and records it on the session."""
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import mirror_script

    try:
        result = mirror_script.run(call, session_id, packet)
    except Exception as exc:  # mirroring never breaks a session
        result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
    with engines.engine_for(call, session_id) as engine:
        mirror = engine.record.setdefault("mirror", {})
        mirror["status"] = result.get("status")
        mirror["last_error"] = (result.get("errors") or [None])[0]
        mirror["view_id"] = result.get("view_id") or mirror.get("view_id")
        mirror["sent"] = int(result.get("sent") or 0)
        return _mirror_brief(mirror)


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
    halted = _halted(call, st, inp.session_id)
    if halted:
        return halted
    st.claim(inp.session_id)
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        receipts_before = list(engine.record.get("receipts") or [])
        states_before = {k: u["state"] for k, u in engine.record["units"].items()}
        kind = engine.record.get("outstanding_kind")
        try:
            outcome = engine.apply(inp.packet_id, inp.answer)
        except AgentError as exc:
            if exc.code == "invalid_input":
                exc.save = True
            raise
        receipts = [r for r in engine.record.get("receipts") or []
                    if r not in set(receipts_before)]
        progress = engine.progress()
        record = engine.record
        closed = [u for k, u in record["units"].items()
                  if u["state"] in schemas.TERMINAL_STATES
                  and states_before.get(k) not in schemas.TERMINAL_STATES]
        snapshot = {"images": record["images"], "state": record["state"],
                    "outstanding_kind": None, "units": record["units"]}
    if not outcome.get("already_applied"):
        for unit in closed:
            _announce(call, snapshot, inp.session_id, "unit_closed", marker=unit["marker"],
                      project=unit["project"], state=unit["state"],
                      confidence=unit.get("confidence"),
                      low=unit.get("final") if unit.get("final") is not None
                      else unit.get("proposed"), reason=unit.get("reason"))
        refs = outcome.get("unit") or ""
        _announce(call, snapshot, inp.session_id, "answered", packet_id=inp.packet_id,
                  kind=kind, marker=refs.split("::", 1)[-1] if refs else None,
                  outcome_state=outcome.get("state"),
                  phase=engines.phase_for(snapshot), progress=progress)
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
                                     "proposed", "gmm", "tier", "class", "reason")
            if unit.get(k) is not None}


def _bulk_status(record):
    """The bulk job's own status beside the session's (read-only): a session
    left `bulk_running` by a restarted server says so rather than looking busy."""
    from plexora.agent import jobs

    job_id = record.get("bulk_job_id")
    job = jobs.store().get(job_id) if job_id else None
    out = {"job_id": job_id, "status": (job or {}).get("status"),
           "resumed": list(record.get("bulk_resumed") or [])}
    if record["state"] == "bulk_running" and out["status"] not in ("queued", "running"):
        out["note"] = ("the bulk pass is not running anywhere (the server that ran it "
                       f"restarted); the next {tool_name_of('gating.next')} resumes it")
    return out


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
        _announce(call, st.load(inp.session_id), inp.session_id, "control",
                  paused=bool(inp.pause), paused_by="agent" if inp.pause else None)
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        if inp.reattach_viewer:
            record.setdefault("mirror", {}).update(status="pending", enabled=True)
        units = [_unit_row(u) for u in record["units"].values()]
        out = {"session_id": inp.session_id, "state": record["state"],
               "scope": record.get("scope"), "mode": record["options"]["mode"],
               "images": record["images"], "reference_image": record.get("reference_image"),
               "progress": engine.progress(), "bulk": _bulk_status(record),
               "units": units[:MAX_LIST],
               "truncated": len(units) > MAX_LIST, "used": record.get("used"),
               "estimated_vision_tokens": budgets.vision_tokens(
                   (record.get("used") or {}).get("pixels", 0)),
               "vocabulary": {"terminal_states": list(schemas.TERMINAL_STATES),
                              "accepted_states": list(schemas.ACCEPTED_STATES),
                              "written_states": list(schemas.WRITTEN_STATES),
                              "confidence": list(schemas.CONFIDENCE)},
               "expression": record.get("expression"),
               "summary": engines.summary_of(record),
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
        "close", description="close: end the session (writes stay; a session the user "
                             "stopped in the viewer is cancelled); commit: write a "
                             "propose-mode session's accepted gates and empty gates; "
                             "cancel: stop the bulk pass (writes stay); rollback: undo every "
                             "write the session made, newest first.")


def finish(call, inp):
    from plexora.agent import jobs, registry
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    stopped = bool(st.control(inp.session_id).get("stopped"))
    action = "cancel" if stopped and inp.action == "close" else inp.action
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        out = {"session_id": inp.session_id, "action": action}
        if action == "cancel":
            job_id = record.get("bulk_job_id")
            if job_id:
                out["job"] = jobs.store().cancel(job_id)
            record["state"] = "cancelled"
        elif action == "commit":
            if record["options"]["mode"] != "propose":
                raise AgentError("invalid_input", "commit is for a propose-mode session")
            record["options"]["mode"] = "apply"
            written = []
            for unit in record["units"].values():
                if unit.get("proposed") is not None and unit["state"] in \
                        schemas.WRITTEN_STATES:
                    empty = unit["state"] in schemas.EMPTY_GATE_STATES
                    op = engine.write(unit, unit["proposed"],
                                      method=unit.get("method") or "ai_accepted",
                                      confidence=unit.get("confidence") or "low",
                                      state=unit["state"], tier=unit.get("tier") or "T2",
                                      empty=empty)
                    if op:
                        written.append(op)
            out["written"] = written
            record["options"]["mode"] = "propose"
            record["state"] = "done"
        elif action == "rollback":
            undone, refused = [], []
            # Newest first; each undo moves its store's revision, which the
            # next-older receipt was not made against -- so the revision this
            # rollback's own previous undo left is vouched for (and nothing
            # anyone else changed in between).
            left = {}
            for operation_id in reversed(record.get("receipts") or []):
                line = call.audit.find(operation_id) or {}
                state = (line.get("receipt") or {}).get("persistent_state")
                arguments = {"operation_id": operation_id}
                if left.get(state):
                    arguments["expected_current_revision"] = left[state]
                answer_ = registry.invoke(call.session, "undo_operation", arguments,
                                          policy=call.policy, audit=call.audit, link=call.link,
                                          notify=call.notify,
                                          operation_id=f"{call.operation_id}.u{len(undone):03d}")
                if answer_["ok"]:
                    undone.append(operation_id)
                    receipt_ = (answer_["result"] or {}).get("receipt") or {}
                    if receipt_.get("revision_after") is not None:
                        left[state] = receipt_["revision_after"]
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
        summary = engines.summary_of(record)
        out["summary"] = summary
        mirror = dict(record.get("mirror") or {})
        snapshot = {"images": record["images"]}
        state = record["state"]
    st.release(inp.session_id)
    reason = "stopped" if stopped else {"close": "closed", "commit": "committed",
                                        "cancel": "cancelled",
                                        "rollback": "rolled_back"}[action]
    _LAST_PHASE.pop(inp.session_id, None)
    _announce(call, snapshot, inp.session_id, "finished", reason=reason, state=state,
              summary=summary, phase="summarizing")
    if _mirroring(mirror):
        from plexora.plugins.gating.server.autogate import mirror_script

        try:
            out["teardown"] = mirror_script.run_teardown(call, inp.session_id, reason)
        except Exception as exc:  # the view is restored best effort
            out["teardown"] = {"status": "degraded", "errors": [{"message": str(exc)}]}
    receipt = make_receipt(call, changed=inp.action in ("commit", "rollback"),
                           before=None, after={k: out.get(k) for k in ("action", "written",
                                                                          "undone")},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": inp.session_id})
    out["receipt"] = receipt.model_dump(mode="json")
    out["next"] = (f"{tool_name_of('gating.report')}(session_id) for the review report; "
                   f"{tool_name_of('gating.export')} for CSV")
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
