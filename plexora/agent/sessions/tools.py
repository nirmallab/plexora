"""The session tools every workflow shares: next, answer, status, finish.

A workflow's capabilities are thin wrappers over one `SessionTools` bound to
its engine, its events and its words. The loop an agent runs is `<kind>_next`,
then `<kind>_answer` until the answer says `decided`; the user's controls
(pause, stop) are read before every call; a bulk pass orphaned by a restarted
server is resubmitted; limits held for the user are surfaced as
`waiting_for_user`; and mirroring an open viewer is best effort, always.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import tool_name_of
from plexora.agent.schemas import AgentModel
from plexora.agent.sessions import budget as budgets


class NextInput(AgentModel):
    session_id: str
    wait_s: float = Field(10.0, ge=0, le=30, description="How long to wait for the bulk "
                                                         "pass when nothing is ready yet.")
    rerender: bool = Field(False, description="Draw the outstanding packet's images again "
                           "(same packet, same charge).")


class AnswerInput(AgentModel):
    session_id: str
    packet_id: str = Field(description="The packet being answered (pk_nnnn).")
    answer: dict[str, Any] = Field(description="The typed answer: `kind` plus the fields the "
                                   "packet's answer_schema lists.")
    include_next: bool = Field(True, description="Return the next packet with the outcome, "
                                                 "saving a next call.")


class FinishInput(AgentModel):
    session_id: str
    action: Literal["close", "commit", "cancel", "rollback"] = Field(
        "close", description="close: end the session (writes stay; a session the user "
                             "stopped in the viewer is cancelled); commit: write a "
                             "propose-mode session's results; cancel: stop the bulk pass "
                             "(writes stay); rollback: undo every write the session made, "
                             "newest first.")


class BulkInput(AgentModel):
    session_id: str


def status_input(limit_decisions):
    class StatusInput(AgentModel):
        session_id: str | None = Field(None, description="Omit to list recent sessions.")
        reattach_viewer: bool = False
        pause: bool | None = Field(None, description="Pause (true) or resume (false) the "
                                                     "session.")
        known_guide: str | None = Field(None, description="The `guide_version` you hold: the "
                                        "reading guide is then not sent again.")
        limits: dict[str, Literal[limit_decisions]] | None = Field(
            None, description="Answers to the session's limit questions (`requests` of a "
                              "`waiting_for_user` result), by unit: `continue` grants another "
                              "allowance, `stop` flags it for manual review. Pass the user's "
                              "answer, not your own guess.")

    return StatusInput


def mirroring(mirror) -> bool:
    return bool(mirror.get("enabled")) and mirror.get("status") != "off"


def mirror_brief(mirror):
    return {k: mirror.get(k) for k in ("status", "view_id", "last_error", "sent")}


class SessionTools:
    """Bound to one workflow. Subclasses supply the hooks at the bottom."""

    #: capability names of this workflow's tools
    NEXT = ""
    ANSWER = ""
    STATUS = ""
    FINISH = ""
    REPORT = ""
    BULK = ""
    STATE = ""            # the receipt's persistent_state for session bookkeeping
    FINISHED_STATES: tuple = ()
    TERMINAL_STATES: tuple = ()
    LOOK_KINDS: tuple = ()
    UNIT_NOUN = "unit"

    def __init__(self):
        self._last_phase: dict = {}

    # -- hooks --------------------------------------------------------------------

    def store(self):
        raise NotImplementedError

    def engine_for(self, call, session_id, *, st=None, save=True):
        raise NotImplementedError

    def announce(self, call, record, session_id, event, /, **payload):
        raise NotImplementedError

    def phase_for(self, record, *, mirroring=False) -> str:
        raise NotImplementedError

    def summary_of(self, record) -> dict:
        raise NotImplementedError

    def subject(self, packet):
        refs = packet.get("units") or []
        return None if not refs else (f"{len(refs)} {self.UNIT_NOUN}s" if len(refs) > 1
                                      else self.unit_word(refs[0]))

    def unit_word(self, ref) -> str:
        return str(ref)

    def issued_extra(self, packet) -> dict:
        """More fields for the `issued` event (a workflow's evidence list, say)."""
        return {}

    def answered_extra(self, outcome, closed, kind) -> dict:
        """More fields for the `answered` event (its narration, say); `closed`
        are the units this answer closed."""
        return {}

    def unit_row(self, unit) -> dict:
        return {k: unit.get(k) for k in ("state", "reason", "confidence")
                if unit.get(k) is not None}

    def closed_event(self, unit) -> dict:
        return {"state": unit["state"], "reason": unit.get("reason")}

    def mirror_run(self, call, session_id, packet) -> dict:
        return {"status": "off", "sent": 0, "errors": []}

    def mirror_teardown(self, call, session_id, reason) -> dict:
        return {"status": "off", "sent": 0, "errors": []}

    def record_limit_answers(self, st, session_id, record, answers) -> dict:
        raise NotImplementedError

    def limit_brief(self, request) -> dict:
        return {k: v for k, v in request.items() if k != "announced"}

    def commit(self, call, engine) -> list:
        """Write a propose-mode session's results; returns receipt ids."""
        raise AgentError("invalid_input", "this workflow has nothing to commit")

    def on_finished(self, call, engine, action, out):
        """After the record is closed and before it is saved (a workflow keeps
        its result, say)."""

    def guide(self, reading, known=None) -> dict:
        return {}

    def status_extra(self, engine) -> dict:
        return {}

    def status_input_model(self):
        raise NotImplementedError

    # -- the loop -----------------------------------------------------------------

    def halted(self, call, st, session_id):
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
            finish = tool_name_of(self.FINISH)
            return {"state": "stopped", "by": control.get("stopped_by"),
                    "note": "the user stopped this session in the viewer",
                    "next": f"{finish}(session_id, action='close') keeps what was written so "
                            f"far; {finish}(session_id, action='rollback') undoes it. Then "
                            "stop."}
        if control.get("paused"):
            return {"state": "paused", "by": control.get("paused_by"), "retry_after_s": 10,
                    "note": "the user paused this session in the viewer"}
        return None

    def phase(self, call, record, session_id, phase, /, **payload):
        if self._last_phase.get(session_id) == phase and not payload:
            return
        self._last_phase[session_id] = phase
        self.announce(call, record, session_id, "phase", phase=phase, **payload)

    def submit_bulk(self, call, session_id):
        from plexora.agent import jobs, registry

        capability = registry.get(self.BULK)
        child = dataclasses.replace(call, capability=capability,
                                    operation_id=f"{call.operation_id}.bulk",
                                    arguments={"session_id": session_id}, receipted=False,
                                    extras=dict(call.extras))
        return jobs.submit(child, BulkInput(session_id=session_id))

    def resume_bulk(self, call, session_id, st):
        """A session whose bulk pass is no longer running anywhere (the server
        that ran it restarted) gets it again; the pass skips what it did."""
        from plexora.agent import jobs

        with self.engine_for(call, session_id, st=st) as engine:
            record = engine.record
            if st.control(session_id).get("stopped"):
                return None
            if record["state"] != "bulk_running" and not (
                    record["state"] == "created" and record.get("bulk_job_id") is None):
                return None
            job = jobs.store().get(record.get("bulk_job_id") or "") if record.get(
                "bulk_job_id") else None
            if job is not None and job.get("status") in ("queued", "running", "done"):
                return None
            job = self.submit_bulk(call, session_id)
            record["bulk_job_id"] = job["job_id"]
            record.setdefault("bulk_resumed", []).append(job["job_id"])
            return job["job_id"]

    def mirror(self, call, session_id, packet):
        try:
            result = self.mirror_run(call, session_id, packet)
        except Exception as exc:  # mirroring never breaks a session
            result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
        with self.engine_for(call, session_id) as engine:
            mirror = engine.record.setdefault("mirror", {})
            mirror["status"] = result.get("status")
            mirror["last_error"] = (result.get("errors") or [None])[0]
            mirror["view_id"] = result.get("view_id") or mirror.get("view_id")
            mirror["sent"] = int(result.get("sent") or 0)
            return mirror_brief(mirror)

    def packet_result(self, packet, images):
        return {"state": "decision", "packet": packet,
                "_images": [{"data": data, "format": fmt} for data, fmt in images]}

    def waiting_for_user(self, requests, progress):
        status_tool = tool_name_of(self.STATUS)
        return {"state": "waiting_for_user", "progress": progress, "retry_after_s": 10,
                "requests": [self.limit_brief(r) for r in requests],
                "note": f"these {self.UNIT_NOUN}s reached their allowance while the evidence "
                        "still says to keep going; the user is asked in the viewer whether to "
                        "continue",
                "next": f"ask the user if no viewer is open, then {status_tool}(session_id, "
                        "limits={unit: 'continue' | 'stop'}); otherwise call "
                        f"{tool_name_of(self.NEXT)} again after retry_after_s"}

    def before_next(self, call, session_id, st):
        """A workflow's settling step before a packet is looked for."""

    def next_packet(self, call, inp):
        st = self.store()
        halted = self.halted(call, st, inp.session_id)
        if halted:
            return halted
        st.claim(inp.session_id)
        self.before_next(call, inp.session_id, st)
        self.resume_bulk(call, inp.session_id, st)
        deadline = time.monotonic() + float(inp.wait_s)
        while True:
            with self.engine_for(call, inp.session_id, st=st) as engine:
                record = engine.record
                if record["state"] in self.FINISHED_STATES:
                    return {"state": record["state"], "progress": engine.progress()}
                outstanding = record.get("outstanding_packet")
                mirror = dict(record.get("mirror") or {})
                packet = None
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
                asking = []
                if status == "wait_user":
                    asking = [dict(u["limit_request"]) for u in images]
                    for unit in images:
                        unit["limit_request"]["announced"] = True
                progress = engine.progress()
                state = record["state"]
                snapshot = {"images": record["images"], "state": state,
                            "outstanding_kind": record.get("outstanding_kind"),
                            "units": record["units"]}
            if status in ("packet", "again"):
                kind = packet.get("kind")
                mirrors = mirroring(mirror)
                if fresh:
                    refs = packet.get("units") or []
                    phase = self.phase_for(snapshot, mirroring=mirrors)
                    self._last_phase[inp.session_id] = phase
                    self.announce(call, snapshot, inp.session_id, "issued",
                                  packet_id=packet.get("packet_id"), kind=kind,
                                  project=refs[0]["project"] if refs else None,
                                  subject=self.subject(packet), phase=phase,
                                  progress=progress, narration=packet.get("narration"),
                                  **self.issued_extra(packet))
                if mirrors and (fresh or mirror.get("status") in ("pending", "degraded")):
                    packet["mirror"] = self.mirror(call, inp.session_id, packet)
                elif mirror.get("enabled"):
                    packet["mirror"] = {**mirror_brief(mirror),
                                        **({"resent": False} if status == "again" else {})}
                if fresh and mirrors and kind in self.LOOK_KINDS:
                    self.phase(call, snapshot, inp.session_id, "thinking")
                return self.packet_result(packet, images)
            if status == "wait_user":
                for request in asking:
                    if not request.pop("announced", False):
                        self.announce(call, snapshot, inp.session_id, "limit_reached",
                                      **self.limit_brief(request), phase="waiting")
                return self.waiting_for_user(asking, progress)
            if status == "wait":
                self.phase(call, snapshot, inp.session_id, "analyzing")
            if status == "done" and state != "bulk_running":
                self.phase(call, snapshot, inp.session_id, "summarizing")
                return {"state": "decided", "progress": progress,
                        "next": f"{tool_name_of(self.FINISH)}(session_id, action='close'), "
                                f"then {tool_name_of(self.REPORT)}"}
            if time.monotonic() >= deadline:
                return {"state": "bulk_running", "progress": progress,
                        "job_id": record.get("bulk_job_id"),
                        "next": f"call {tool_name_of(self.NEXT)} again; the deterministic "
                                "pass is still running"}
            time.sleep(0.5)

    def answer(self, call, inp):
        st = self.store()
        halted = self.halted(call, st, inp.session_id)
        if halted:
            return halted
        st.claim(inp.session_id)
        with self.engine_for(call, inp.session_id, st=st) as engine:
            receipts_before = set(engine.record.get("receipts") or [])
            states_before = {k: u["state"] for k, u in engine.record["units"].items()}
            kind = engine.record.get("outstanding_kind")
            try:
                outcome = engine.apply(inp.packet_id, inp.answer)
            except AgentError as exc:
                if exc.code == "invalid_input":
                    exc.save = True
                raise
            receipts = [r for r in engine.record.get("receipts") or []
                        if r not in receipts_before]
            progress = engine.progress()
            record = engine.record
            closed = [u for k, u in record["units"].items()
                      if u["state"] in self.TERMINAL_STATES
                      and states_before.get(k) not in self.TERMINAL_STATES]
            snapshot = {"images": record["images"], "state": record["state"],
                        "outstanding_kind": None, "units": record["units"]}
        if not outcome.get("already_applied"):
            for unit in closed:
                self.announce(call, snapshot, inp.session_id, "unit_closed",
                              project=unit.get("project"), **self.closed_event(unit))
            self.announce(call, snapshot, inp.session_id, "answered",
                          packet_id=inp.packet_id, kind=kind,
                          outcome_state=outcome.get("state"),
                          phase=self.phase_for(snapshot), progress=progress,
                          **self.answered_extra(outcome, closed, kind))
        result = {"applied": not outcome.get("already_applied"), "outcome": outcome,
                  "receipts": receipts, "progress": progress}
        if inp.include_next and not outcome.get("already_applied"):
            following = self.next_packet(call, NextInput(session_id=inp.session_id,
                                                         wait_s=5.0))
            images = following.pop("_images", None)
            result["next"] = following
            if images:
                result["_images"] = images
        return result

    def status(self, call, inp):
        st = self.store()
        if inp.session_id is None:
            return {"sessions": [{k: r.get(k) for k in ("session_id", "created_at", "state",
                                                         "scope", "images")}
                                 for r in st.list()]}
        if inp.pause is not None:
            st.set_control(inp.session_id, paused=bool(inp.pause),
                           paused_by="agent" if inp.pause else None)
            self.announce(call, st.load(inp.session_id), inp.session_id, "control",
                          paused=bool(inp.pause), paused_by="agent" if inp.pause else None)
        if inp.limits:
            answered = self.record_limit_answers(st, inp.session_id, st.load(inp.session_id),
                                                 inp.limits)
            self.announce(call, st.load(inp.session_id), inp.session_id, "limit_answered",
                          answers={k.split("::", 1)[-1]: v for k, v in answered.items()},
                          by="agent")
        with self.engine_for(call, inp.session_id, st=st) as engine:
            record = engine.record
            if inp.reattach_viewer:
                record.setdefault("mirror", {}).update(status="pending", enabled=True)
            units = [self.unit_row(u) for u in record["units"].values()]
            out = {"session_id": inp.session_id, "state": record["state"],
                   "mode": record["options"]["mode"], "images": record["images"],
                   "progress": engine.progress(), "units": units[:MAX_LIST],
                   "truncated": len(units) > MAX_LIST, "used": record.get("used"),
                   "estimated_vision_tokens": budgets.vision_tokens(
                       (record.get("used") or {}).get("pixels", 0)),
                   "summary": self.summary_of(record),
                   **self.guide(record["options"].get("reading"), inp.known_guide),
                   "mirror": record.get("mirror"), "control": st.control(inp.session_id),
                   "outstanding_packet": record.get("outstanding_packet"),
                   "receipts": len(record.get("receipts") or []),
                   "replayed": len(record.get("replayed") or []),
                   "limit_requests": [self.limit_brief(u["limit_request"])
                                      for u in engine.waiting_for_user()],
                   **self.status_extra(engine)}
        return out

    def finish(self, call, inp):
        from plexora.agent import jobs, registry

        st = self.store()
        stopped = bool(st.control(inp.session_id).get("stopped"))
        action = "cancel" if stopped and inp.action == "close" else inp.action
        with self.engine_for(call, inp.session_id, st=st) as engine:
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
                out["written"] = self.commit(call, engine)
                record["state"] = "done"
            elif action == "rollback":
                undone, refused = [], []
                left = {}
                for operation_id in reversed(record.get("receipts") or []):
                    line = call.audit.find(operation_id) or {}
                    state = (line.get("receipt") or {}).get("persistent_state")
                    arguments = {"operation_id": operation_id}
                    if left.get(state):
                        arguments["expected_current_revision"] = left[state]
                    answer_ = registry.invoke(call.session, "undo_operation", arguments,
                                              policy=call.policy, audit=call.audit,
                                              link=call.link, notify=call.notify,
                                              operation_id=f"{call.operation_id}.u"
                                                           f"{len(undone):03d}")
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
                    raise AgentError("conflict", "the bulk pass is still running; cancel it "
                                     "or wait for it", retryable=True)
                record["state"] = "done"
            record["finished_at"] = _now()
            record["outstanding_packet"] = None
            self.on_finished(call, engine, action, out)
            out["progress"] = engine.progress()
            out["units"] = [self.unit_row(u) for u in record["units"].values()][:MAX_LIST]
            summary = self.summary_of(record)
            out["summary"] = summary
            mirror = dict(record.get("mirror") or {})
            snapshot = {"images": record["images"]}
            state = record["state"]
        st.release(inp.session_id)
        reason = "stopped" if stopped else {"close": "closed", "commit": "committed",
                                            "cancel": "cancelled",
                                            "rollback": "rolled_back"}[action]
        self._last_phase.pop(inp.session_id, None)
        self.announce(call, snapshot, inp.session_id, "finished", reason=reason, state=state,
                      summary=summary, phase="summarizing")
        if mirroring(mirror):
            try:
                out["teardown"] = self.mirror_teardown(call, inp.session_id, reason)
            except Exception as exc:  # the view is restored best effort
                out["teardown"] = {"status": "degraded", "errors": [{"message": str(exc)}]}
        receipt = make_receipt(call, changed=inp.action in ("commit", "rollback"),
                               before=None, after={k: out.get(k) for k in ("action", "written",
                                                                              "undone")},
                               persistent_state=self.STATE, reversible=False,
                               extra={"session": inp.session_id})
        out["receipt"] = receipt.model_dump(mode="json")
        out["next"] = f"{tool_name_of(self.REPORT)}(session_id) for the review report"
        return out


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def limit_default(env, defaults, policies, name):
    """A limit option's default: the environment a command line set, else the
    workflow's default. An unreadable value falls back to the default."""
    import os

    raw = os.environ.get(env[name])
    default = defaults[name]
    if raw is None or raw == "":
        return default
    if name == "on_limit":
        return raw if raw in policies else default
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return default


def mirror_at_start(call, enabled, view_id, status_tool, project=None):
    """A session's mirror, probed once: `pending` when a tab can be driven,
    `off` (and why) when none can.

    `enabled` is tri-state. True asks for the mirror (and says why it is off
    when no tab can be driven); False opts out; None is AUTO -- on when this
    process can drive exactly one live tab (the one showing `project`, else
    the only one open, which `open_project` then points at the image), and
    otherwise silently off with a `reason`. The record's `enabled` is the
    outcome, so everything downstream still reads a plain bool; `requested`
    keeps what was asked for.
    """
    requested = "auto" if enabled is None else ("on" if enabled else "off")
    mirror = {"enabled": bool(enabled), "requested": requested, "view_id": view_id,
              "status": "off", "last_error": None}
    if enabled is None:
        return _mirror_auto(call, mirror, view_id, project)
    if not enabled:
        mirror["reason"] = "turned off (mirror=false)"
        return mirror
    from plexora.agent import viewer

    try:
        view = viewer.resolve_view(viewer.require(call.link), view_id)
    except AgentError as exc:
        mirror["last_error"] = {"code": exc.code, "message": exc.message}
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        mirror["hint"] = ((detail.get("hint") or viewer.NOT_AVAILABLE_HINT)
                          + f"; then {tool_name_of(status_tool)}(session_id, "
                            "reattach_viewer=true)")
        return mirror
    mirror.update(status="pending", view_id=view.get("view_id") or view_id)
    return mirror


def _mirror_auto(call, mirror, view_id, project):
    """Auto: a tab this process can drive, or `off` with the reason -- never
    an error, since nobody asked for a mirror."""
    from plexora.agent import viewer

    def off(reason):
        mirror.update(enabled=False, status="off", reason=reason)
        return mirror

    if viewer.connect(call.link) is None:
        return off("no viewer: this agent is not attached to a running Plexora server")
    try:
        control = viewer.require(call.link)
        if view_id:
            view = viewer.resolve_view(control, view_id)
        else:
            view = None
            for scope in ((project,) if project else ()) + (None,):
                sessions = control.list_sessions(scope)
                live = [s for s in sessions if s.get("status") != "stale"]
                if len(live) == 1:
                    view = live[0]
                    break
                if len(live) > 1:
                    return off(f"{len(live)} viewers are open; pass mirror=true and view_id "
                               "to pick one")
            if view is None:
                return off("no viewer: no Plexora tab is open")
    except AgentError as exc:
        return off(f"no viewer: {exc.message}")
    except Exception as exc:  # a probe never stops a session starting
        return off(f"no viewer: {exc}")
    mirror.update(enabled=True, status="pending", view_id=view.get("view_id") or view_id)
    return mirror
