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
from plexora.agent.sessions.engine import DEFAULT_READER

#: How often a reader whose next decision waits on another reader's answer
#: looks again (`busy`), within its `wait_s`.
BUSY_POLL_S = 0.1
#: How many receipt ids an answer lists; beyond that it says how many, the
#: first and the last (`receipts_summary`). The full list stays in the
#: session record, which status, finish and undo read.
RECEIPTS_LISTED = 5


def receipts_summary(receipts):
    """An answer's new receipts: the ids when there are at most
    `RECEIPTS_LISTED`, else {count, first, last} -- one answer that wrote two
    hundred regions sent two hundred ids nobody read."""
    receipts = list(receipts or [])
    if len(receipts) <= RECEIPTS_LISTED:
        return receipts
    return {"count": len(receipts), "first": receipts[0], "last": receipts[-1]}


def busy(progress, record) -> dict:
    """What `<kind>_next` says to a reader while every decision it could be
    given waits on a packet another reader holds."""
    return {"state": "busy", "progress": progress, "retry_after_s": 1,
            "outstanding": len((record or {}).get("outstanding") or {}),
            "note": "every decision left waits on a packet another reader is answering",
            "next": "call next again; an answer frees the decisions that wait on it"}


#: Shared by every session workflow's next/answer inputs.
READER = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_.:-]+$",
               description="Who is answering, when several conversations answer one session "
                           "(one id per conversation, from a worker's brief). Omit when you "
                           "are the only one.")
TASKS = Field(None, max_length=8,
              description="The tasks this reader answers (`module.task`, from its brief): it "
                          "is given only their packets, and `other_tasks` when what is ready "
                          "is another task's. Omit to answer every packet.")
MODEL = Field(None, max_length=80,
              description="The model you are running on, as your client names it: recorded "
                          "with the answer, so the session can show which model answered "
                          "each task.")


class NextInput(AgentModel):
    session_id: str
    wait_s: float = Field(10.0, ge=0, le=30, description="How long to wait for the bulk "
                                                         "pass when nothing is ready yet "
                                                         "(at most 30; call again for "
                                                         "longer).")
    rerender: bool = Field(False, description="Draw the outstanding packet's images again "
                           "(same packet, same charge).")
    reader: str | None = READER
    tasks: list[str] | None = TASKS


class AnswerInput(AgentModel):
    session_id: str
    packet_id: str = Field(description="The packet being answered (pk_nnnn).")
    answer: dict[str, Any] = Field(description="The typed answer: `kind` plus the fields the "
                                   "packet's answer_schema lists.")
    include_next: bool = Field(True, description="Return the next packet with the outcome, "
                                                 "saving a next call.")
    reader: str | None = READER
    tasks: list[str] | None = TASKS
    model: str | None = MODEL


class FinishInput(AgentModel):
    session_id: str
    action: Literal["close", "commit", "cancel", "rollback"] = Field(
        "close", description="close: end the session (writes stay; a session the user "
                             "stopped in the viewer is cancelled); commit: write a "
                             "propose-mode session's results; cancel: stop the bulk pass "
                             "(writes stay); rollback: undo every write the session made, "
                             "newest first.")
    units: Literal["open", "all"] = Field(
        "open", description="open: list only the units still to settle and those left "
                            "for a person (the counts cover the rest); all: every unit.")


class BulkInput(AgentModel):
    session_id: str


def status_input(limit_decisions):
    class StatusInput(AgentModel):
        session_id: str | None = Field(None, description="Omit to list recent sessions.")
        reattach_viewer: bool = False
        replay_mirror: bool = Field(False, description="With reattach_viewer: show the packet "
                                    "out in the viewer now, not at the next packet.")
        view_id: str | None = Field(None, description="With reattach_viewer: the tab to "
                                    "mirror into (defaults to the one mirrored before).")
        pause: bool | None = Field(None, description="Pause (true) or resume (false) the "
                                                     "session.")
        known_guide: str | None = Field(None, description="The `guide_version` you hold: the "
                                        "reading guide is then not sent again.")
        units: Literal["open", "all"] = Field(
            "open", description="open: list only the units still to settle and those left "
                                "for a person (the counts cover the rest); all: every unit.")
        detail: Literal["brief", "full"] = Field(
            "brief", description="brief: the units list capped (with a count of the rest), "
                                 "the residual as its top rows plus totals, no strictness "
                                 "table or vocabulary; full: all of it.")
        limits: dict[str, Literal[limit_decisions]] | None = Field(
            None, description="Answers to the session's limit questions (`requests` of a "
                              "`waiting_for_user` result), by unit: `continue` grants another "
                              "allowance, `stop` flags it for manual review. Pass the user's "
                              "answer, not your own guess.")

    return StatusInput


#: Units listed in a status/finish reply before the rest just get a count,
#: when `units="open"` (the default): during a bulk pass every unit is still
#: open, so the generic MAX_LIST (200) capped nothing for a ~129-unit
#: session -- 30k characters of units the agent was not going to act on yet.
#: `units="all"` is an explicit ask for the whole list (a benchmark reading
#: back every channel's final state, say) and keeps the older, looser cap.
MAX_STATUS_UNITS = 20


def _capped(rows, which):
    """(rows capped, how many were left out) -- `which` is the status/finish
    `units` option that produced `rows` (`listed_units`'s `which`)."""
    limit = MAX_LIST if which == "all" else MAX_STATUS_UNITS
    return rows[:limit], max(0, len(rows) - limit)


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

    def delegate_block(self, record, session_id) -> dict:
        """`delegate` (`plexora.ai.delegation`) for a workflow whose packets are
        handed to workers, else nothing."""
        return {}

    def status_extra(self, engine, detail="brief") -> dict:
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

    def mirror(self, call, session_id, packet, *, replay=False):
        """Show the packet in the session's viewer, one script at a time.
        `replay` is the tab asking to watch again (`attach_viewer`): it skips
        a packet just shown, and a viewer detached meanwhile."""
        from plexora.agent.sessions import mirror as session_mirror

        packet_id = packet.get("packet_id")
        with session_mirror.send_lock(session_id):
            if replay:
                st = self.store()
                current = st.load(session_id).get("mirror") or {}
                if st.control(session_id).get("viewer_detached") or (
                        current.get("status") == "ok" and current.get("packet_id") == packet_id
                        and time.time() - float(current.get("sent_at") or 0) < 30):
                    return mirror_brief(current)
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
                mirror["packet_id"] = packet_id
                mirror["sent_at"] = time.time()
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
        needs = None
        while True:
            with self.engine_for(call, inp.session_id, st=st) as engine:
                record = engine.record
                if record["state"] in self.FINISHED_STATES:
                    return {"state": record["state"], "progress": engine.progress()}
                reader = getattr(inp, "reader", None) or DEFAULT_READER
                held = engine.outstanding(reader)
                outstanding = held[0] if held else None
                mirror = dict(record.get("mirror") or {})
                # "Continue in background": the session goes on, the tab is left be.
                detached = bool(st.control(inp.session_id).get("viewer_detached"))
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
                    packet, images, status = engine.issue(
                        reader=reader, parallel=getattr(inp, "parallel", None),
                        tasks=getattr(inp, "tasks", None))
                    fresh = status == "packet"
                    needs = engine.needs
                asking = []
                if status == "wait_user":
                    asking = [dict(u["limit_request"]) for u in images]
                    for unit in images:
                        unit["limit_request"]["announced"] = True
                progress = engine.progress()
                state = record["state"]
                snapshot = {"images": record["images"], "state": state,
                            "outstanding_kind": record.get("outstanding_kind"),
                            "outstanding": dict(record["outstanding"]),
                            "units": record["units"]}
            if status in ("packet", "again"):
                kind = packet.get("kind")
                mirrors = mirroring(mirror) and not detached
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
                                        **({"detached": True} if detached else {}),
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
            if status == "other_tasks":
                from plexora.ai import delegation

                return delegation.other_tasks(needs, progress)
            if status == "wait":
                self.phase(call, snapshot, inp.session_id, "analyzing")
            if status == "busy" and time.monotonic() >= deadline:
                return busy(progress, snapshot)
            if status == "done" and state != "bulk_running":
                self.phase(call, snapshot, inp.session_id, "summarizing")
                return {"state": "decided", "progress": progress,
                        "next": f"{tool_name_of(self.FINISH)}(session_id, action='close'), "
                                f"then {tool_name_of(self.REPORT)}"}
            if time.monotonic() >= deadline:
                return {"state": "bulk_running", "progress": progress,
                        "job_id": record.get("bulk_job_id"),
                        "next": (f"job_wait(job_id), then call {tool_name_of(self.NEXT)}; "
                                 "the deterministic pass is still running"
                                 if record.get("bulk_job_id") else
                                 f"call {tool_name_of(self.NEXT)} again; the deterministic "
                                 "pass is still running")}
            time.sleep(BUSY_POLL_S if status == "busy" else 0.5)

    def answer(self, call, inp):
        st = self.store()
        halted = self.halted(call, st, inp.session_id)
        if halted:
            return halted
        st.claim(inp.session_id)
        with self.engine_for(call, inp.session_id, st=st) as engine:
            receipts_before = set(engine.record.get("receipts") or [])
            states_before = {k: u["state"] for k, u in engine.record["units"].items()}
            kind = (engine.record["outstanding"].get(inp.packet_id) or {}).get("kind")
            reader, tasks = engine.scope_of(inp.packet_id, inp.reader, inp.tasks)
            try:
                outcome = engine.apply(inp.packet_id, inp.answer)
            except AgentError as exc:
                if exc.code == "invalid_input":
                    exc.save = True
                raise
            if not outcome.get("already_applied"):
                engine.answered_by(inp.packet_id, inp.model)
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
            # One event per answer: the units it closed ride on `answered`
            # (`closed`, each what a `unit_closed` event carried), not one
            # event -- one round trip to the viewer -- per unit.
            self.announce(call, snapshot, inp.session_id, "answered",
                          packet_id=inp.packet_id, kind=kind,
                          outcome_state=outcome.get("state"),
                          phase=self.phase_for(snapshot), progress=progress,
                          closed=[{"project": unit.get("project"), **self.closed_event(unit)}
                                  for unit in closed],
                          **self.answered_extra(outcome, closed, kind))
        result = {"applied": not outcome.get("already_applied")
                  and outcome.get("state") != "reissue",
                  "outcome": outcome, "receipts": receipts_summary(receipts),
                  "progress": progress}
        if inp.include_next and not outcome.get("already_applied"):
            following = self.next_packet(call, NextInput(session_id=inp.session_id,
                                                         wait_s=5.0, reader=reader,
                                                         tasks=tasks))
            images = following.pop("_images", None)
            result["next"] = following
            if following.get("state") == "decision":
                # The breakdown is the status tool's; the packet that follows
                # says how far along the session is.
                result["progress"] = {k: progress[k] for k in ("units_done", "units_total")}
            if images:
                result["_images"] = images
        return result

    def listed_units(self, record, which="open"):
        """The units a status or finish lists: all, or only those not yet
        settled and those left for a person."""
        units = list(record["units"].values())
        if which == "all":
            return units
        return [u for u in units if u["state"] not in self.TERMINAL_STATES
                or "manual_review" in u["state"] or u.get("limit_request")]

    def status(self, call, inp):
        st = self.store()
        if inp.session_id is None:
            return {"sessions": [{k: r.get(k) for k in ("session_id", "created_at", "state",
                                                         "scope", "images")}
                                 for r in st.list()]}
        if inp.limits:
            answered = self.record_limit_answers(st, inp.session_id, st.load(inp.session_id),
                                                 inp.limits)
            self.announce(call, st.load(inp.session_id), inp.session_id, "limit_answered",
                          answers={k.split("::", 1)[-1]: v for k, v in answered.items()},
                          by="agent")
        if inp.pause is not None:
            # Pause/resume is a control flip, not a question about the whole
            # session: the 30k-character status (every unit, the residual,
            # the vocabulary) for a one-word answer was the complaint.
            st.set_control(inp.session_id, paused=bool(inp.pause),
                           paused_by="agent" if inp.pause else None)
            with self.engine_for(call, inp.session_id, st=st) as engine:
                progress = engine.progress()
                state = engine.record["state"]
            self.announce(call, st.load(inp.session_id), inp.session_id, "control",
                          paused=bool(inp.pause), paused_by="agent" if inp.pause else None)
            return {"session_id": inp.session_id, "paused": bool(inp.pause), "state": state,
                    "progress": progress}
        with self.engine_for(call, inp.session_id, st=st) as engine:
            record = engine.record
            if inp.reattach_viewer:
                mirror_ = record.setdefault("mirror", {})
                mirror_.update(status="pending", enabled=True,
                               view_id=inp.view_id or mirror_.get("view_id"))
            replay = record.get("outstanding_packet") \
                if inp.reattach_viewer and inp.replay_mirror else None
            units = [self.unit_row(u) for u in self.listed_units(record, inp.units)]
            shown, omitted = _capped(units, inp.units)
            out = {"session_id": inp.session_id, "state": record["state"],
                   "mode": record["options"]["mode"], "images": record["images"],
                   "progress": engine.progress(), "units": shown,
                   "units_omitted": omitted, "truncated": omitted > 0,
                   "used": record.get("used"),
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
                   **self.status_extra(engine, inp.detail),
                   **self.delegate_block(record, inp.session_id)}
            models = engine.models_used()
            if models:
                out["models"] = models
        if replay and not st.control(inp.session_id).get("viewer_detached"):
            try:
                packet, _ = st.read_packet(inp.session_id, replay)
            except AgentError:
                packet = None     # answered meanwhile: the next packet is mirrored
            if packet is not None:
                out["mirror"] = self.mirror(call, inp.session_id, packet, replay=True)
        return out

    def finish(self, call, inp):
        from plexora.agent import jobs, registry

        st = self.store()
        control_ = st.control(inp.session_id)
        stopped = bool(control_.get("stopped"))
        detached = bool(control_.get("viewer_detached"))
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
                        # The input is a string; a store's revision may be an int.
                        arguments["expected_current_revision"] = str(left[state])
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
            engine.release_all()
            self.on_finished(call, engine, action, out)
            out["progress"] = engine.progress()
            units = [self.unit_row(u) for u in self.listed_units(record, inp.units)]
            out["units"], out["units_omitted"] = _capped(units, inp.units)
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
        if mirroring(mirror) and not detached:
            # A detached viewer is the user's own: nothing is put back in it.
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


def _best_of(sessions):
    """Among several live tabs showing the same project, the one most likely
    meant: visible over hidden (a background tab is unlikely to be the one
    the user is watching), then most recently seen (`seconds_since_seen`,
    from `list_viewers`) -- never a guess across different projects, only
    among tabs that already agree on which image."""
    return sorted(sessions, key=lambda s: (not s.get("visible"),
                                           s.get("seconds_since_seen")
                                           if s.get("seconds_since_seen") is not None
                                           else float("inf")))[0]


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
                    if scope is not None:
                        # Several tabs on this project: pick the one visible
                        # and most recently seen rather than give up -- an
                        # error here just for having two tabs open was the
                        # live-run complaint. Across different projects
                        # (scope is None) this is still ambiguous: say so.
                        view = _best_of(live)
                        break
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
