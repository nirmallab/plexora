"""Plexora AI runs as capabilities: start one, read it, steer it, see the balance.

    ai.run_session  (job)  gate or QC the open project with the harness: the
                           decision loop of `decision.py` on a job thread,
                           its calls billed through the gateway
    ai.run_status          one run (by run id or job id), or the recent ones
    ai.run_control         pause / resume / stop the run's session
    ai.balance             the organisation's credit, the price list, and what
                           a run of this project would be quoted (its ceiling)

`/ai/v1` (server/routes/ai_routes.py) and MCP reach these through
`registry.invoke`, so receipts and audit lines are the same whoever asks.

A run started here is the in-app twin of `plexora ai run`: the same
`GatingRun` / `QCRun`, given the job's own call -- its policy, audit log,
viewer link and notifier -- so the session's events (`gating.session`,
`qc.session`) reach the open tabs exactly as an external agent's do, and the
agent panel shows them. The harness adds its own events on the same channel
(`AI_EVENTS`): the quote, a usage line per packet, and a pause for credit or a
gateway error, which the panel turns into a Resume card.

The run id is the job id's (`air_<hex>` for `job_<hex>`), so either names it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

OWNER = "ai"
ENTITLEMENT = "ai"
TAGS = ("ai", "plexora_ai", "harness", "gating", "qc", "session", "credits")
STATE = "ai_runs"
#: The harness's own events on a session's channel (beside the workflow's
#: `SESSION_EVENTS`); agentPanel.js handles each.
AI_EVENTS = ("ai_run", "ai_context", "ai_usage", "ai_paused", "ai_finished",
             # One look of several out: asked again, asked afresh, cut by a stop.
             "ai_retry", "ai_reissued", "ai_cut")
#: Credits are 10,000 micro-dollars (the gateway's `credit_micro`).
CREDIT_MICRO = 10_000

#: How a run is charged, beside its estimate (`ai_balance`): the gateway's
#: ledger charges a declared run min(metered calls, quote) and holds the
#: quote from the one organisation wallet while it runs.
CHARGING = ("each estimate is the quote a run is declared with: up to that many credits. Every "
            "model call is metered and the run is charged what its calls cost, never more than "
            "the quote, which is held from the wallet while the run lasts. The wallet is the "
            "organisation's one AI wallet, which every product's AI -- a Plexora run, a "
            "conversation -- draws on.")


def run_id_for(job_id: str) -> str:
    return "air_" + str(job_id).removeprefix("job_")


def job_id_for(run_id: str) -> str:
    return "job_" + str(run_id).removeprefix("air_")


def _gateway(dev: bool = False):
    from plexora.ai.harness.gateway import GatewayClient

    return GatewayClient(dev=True if dev else None)


#: The runs Plexora AI starts (`ai_run_session`'s `kind`).
RUN_KINDS = ("gating", "contrast", "qc")


def contrast_grids(markers):
    from plexora.ai.harness.decision import contrast_grids as grids

    return grids(markers)


def _workflow(kind):
    from plexora.ai.harness.decision import WORKFLOWS

    return WORKFLOWS[kind]


# -- ai.run_session ------------------------------------------------------------------------


class RunInput(AgentModel):
    kind: Literal["gating", "contrast", "qc", "qc_gating"] = Field(
        description="gating: Auto Gating of the markers; contrast: how bright each marker is "
                    "drawn, no gating; qc: AutoQC of the image; qc_gating: AutoQC, then Auto "
                    "Gating on QC's stain verdicts and the cells QC kept.")
    project: str
    markers: list[str] | None = Field(None, max_length=200, description="gating, contrast: the "
                                      "markers (default: every marker of the table).")
    channels: list[str] | None = Field(None, max_length=200, description="qc: the channels "
                                       "(default: every channel of the image).")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: gates / regions are written as they are decided; propose: "
                             "nothing is written until the session is committed.")
    resume_session: str | None = Field(None, description="Continue this paused session (after "
                                       "a credit pause, say) instead of starting one.")
    dev: bool = Field(False, description="The gateway's dev route (internal testing accounts "
                                         "only).")
    model: str | None = Field(None, max_length=80, description="With dev only: the model to use.")
    units_per_worker: int | None = Field(None, ge=1, le=16, description="Units one rolling "
                                         "worker covers (default: 1 marker, 4 QC units).")
    #: Default: the session's `lanes`, chosen from the panel; more lanes
    #: finish sooner.
    parallel_markers: int | None = Field(None, ge=1, le=32, description="gating and qc: packets "
                                         "answered side by side, each lane its own workers; the "
                                         "results are the serial run's. 1: one at a time.")
    start_options: dict | None = Field(None, description="Extra options for the session's start "
                                       "(the workflow's own, e.g. QC's map_cell_um).")
    #: A cheap text model normalises it (plexora.ai.context); empty: no call.
    context: str | None = Field(None, max_length=1000, description="gating: the user's note about "
                                "the sample, as typed (\"melanoma from skin\", \"only gate CD3 and "
                                "CD8\"): read into the session's biology, narrowing the markers only "
                                "when it says so.")


class _Relay:
    """The harness's events, told to the job's progress and to the tabs."""

    def __init__(self, call, inp):
        self.call = call
        self.inp = inp
        self.wf = _workflow(inp.kind)
        self.job_id = (call.job or {}).get("job_id")
        self._images = {}

    def images(self, session_id) -> list:
        """The images a session's events go to, as its own events do (a
        dataset's session: every image, so a tab switched to another still
        hears the run): the launching project first, then the session's
        `images`, read once."""
        if session_id not in self._images:
            try:
                images = _session_parts(self.inp.kind)[0].store().load(session_id).get("images")
            except Exception:             # the launching tab is told regardless
                return [self.inp.project]
            self._images[session_id] = list(dict.fromkeys([self.inp.project, *(images or [])]))
        return self._images[session_id]

    def _tell(self, event, session_id, **fields):
        notify = self.call.notify
        if notify is None or not session_id:
            return
        try:
            channel = self.wf.events()
            body = {"session_id": session_id, "event": event, "control": channel.control_for(session_id),
                    "workflow": self.wf.name, "job_id": self.job_id, **fields}
        except Exception:                 # telling the tabs never breaks a run
            return
        for project in self.images(session_id):
            try:
                notify(project, channel.owner, channel.kind, body)
            except Exception:             # telling the tabs never breaks a run
                pass

    def _tell_run(self, event, **fields):
        """An event of a run that has no session yet (collecting its set-up):
        on the session channel, by run id, for the tab whose card launched it."""
        notify = self.call.notify
        if notify is None:
            return
        try:
            channel = self.wf.events()
            notify(self.inp.project, channel.owner, channel.kind,
                   {"session_id": None, "event": event, "workflow": self.wf.name,
                    "job_id": self.job_id, **fields})
        except Exception:                 # telling the tabs never breaks a run
            pass

    def __call__(self, event):
        name = event.get("event")
        session_id = event.get("session_id")
        usage = event.get("usage") or {}
        run = {"run_id": event.get("run_id"), "kind": self.inp.kind, "project": self.inp.project}
        if name == "started":
            self.call.progress(0, None, f"session {session_id} started: {event.get('units')} "
                                        f"{event.get('unit_noun') or 'unit'}s")
        elif name == "context":
            self._tell("ai_context", session_id, interpretation=event.get("interpretation"),
                       units=event.get("units"), **run)
        elif name in ("quoted", "resumed"):
            # A resume carries what the session has spent so far, so the
            # card shows its total at once rather than counting from zero.
            self._tell("ai_run", session_id, quote_credits=event.get("quote_credits"),
                       resumed=name == "resumed",
                       **({"usage": event["usage"]} if event.get("usage") else {}), **run)
        elif name == "answered":
            self.call.progress(usage.get("packets"), None,
                               f"{usage.get('packets')} packets, {usage.get('charged_credits')} credits")
            self._tell("ai_usage", session_id, usage=usage, **run)
        elif name == "retry":
            # A call asked again (a dropped stream, a rate limit): the panel
            # marks that look "retrying" rather than leaving it to look stuck.
            self._tell("ai_retry", session_id, packet_id=event.get("packet_id"), code=event.get("code"),
                       attempt=event.get("attempt"), retry_after=event.get("retry_after"),
                       throttle=event.get("throttle"), **run)
        elif name in ("reissued", "cut"):
            self._tell(f"ai_{name}", session_id, packet_id=event.get("packet_id"), **run)
        elif name == "parked":
            # The user's pause, in the viewer: the plugin's control route has
            # told the tabs already; the run waits on its thread.
            self.call.progress(None, None, "paused in the viewer")
        elif name == "waiting_for_setup":
            self.call.progress(None, None, "waiting for the set-up form in the viewer")
            # Before its session exists the run speaks for itself: the
            # launching tab's card opens the form. A session waiting on it
            # told the tabs already (`needs_setup`).
            if not session_id:
                self._tell_run("ai_setup", setup=event.get("setup"), message=event.get("message"), **run)
        elif name == "setup_done":
            self.call.progress(None, None, "set-up finished")
            if not session_id:
                self._tell_run("ai_setup_done", **run)
        elif name == "paused":
            self._tell("ai_paused", session_id, reason=event.get("reason"), message=event.get("message"),
                       credit=bool(event.get("credit")), top_up_url=event.get("top_up_url"), usage=usage,
                       resume={"kind": self.inp.kind, "project": self.inp.project,
                               "resume_session": session_id}, **run)
        elif name == "finished":
            self._tell("ai_finished", session_id, status=event.get("status"), reason=event.get("reason"),
                       usage=usage, resume={"kind": self.inp.kind, "project": self.inp.project,
                                            "resume_session": session_id}, **run)


#: The start options QC's half of a `qc_gating` run takes from the launcher's
#: (the rest -- a gating confirm's yes -- are gating's).
SHARED_START = ("mirror", "view_id")


def run_session(call, inp):
    from plexora.ai.harness.gateway import GatewayError

    if inp.model and not inp.dev:
        raise AgentError("invalid_input", "a model may be named only with dev=true")
    try:
        gateway = _gateway(inp.dev)
    except GatewayError as exc:
        raise AgentError("license_required" if exc.code == "no_license" else "resource_unavailable",
                         str(exc)) from None
    run_id = run_id_for(call.job["job_id"]) if call.job else None
    if inp.kind != "qc_gating":
        summary, relay = _run_one(call, inp, gateway, run_id)
        return {**summary, "job_id": relay.job_id}
    # QC, then gating, on one job: gating starts only once QC is done, so it
    # reuses QC's stain verdicts (`stain_check: reuse`) and leaves out the cells
    # QC flagged. A QC that paused, failed or was stopped ends the run there;
    # the paused part is resumed by its own kind (`resume_session`).
    if inp.resume_session:
        raise AgentError("invalid_input", "a qc_gating run is not resumed as one: resume the paused "
                         "session with kind qc or gating")
    start = dict(inp.start_options or {})
    qc_inp = inp.model_copy(update={"kind": "qc", "context": None, "markers": None,
                                    "start_options": {k: v for k, v in start.items()
                                                      if k in SHARED_START}})
    qc, relay = _run_one(call, qc_inp, gateway, run_id)
    out = {"qc": qc, "job_id": relay.job_id}
    if qc.get("status") != "done":
        return {**qc, **out, "gating": None,
                "reason": f"QC ended {qc.get('status')}: gating did not start"}
    gating_inp = inp.model_copy(update={"kind": "gating", "channels": None})
    gating, _relay = _run_one(call, gating_inp, gateway, f"{run_id}_gating" if run_id else None)
    charged = round(float(qc.get("charged_credits") or 0) + float(gating.get("charged_credits") or 0), 2)
    return {**gating, **out, "gating": gating, "charged_credits": charged}


def _run_one(call, inp, gateway, run_id):
    """One session of `inp.kind` (gating, contrast or qc) on the job: (summary, relay)."""
    from dataclasses import replace

    from plexora.ai.harness.decision import RUNS, GatingOptions, QCOptions
    common = {"project": inp.project, "mode": inp.mode, "model": inp.model,
              "resume_session": inp.resume_session, "start_options": dict(inp.start_options or {}),
              "context": inp.context if inp.kind == "gating" else None}
    if inp.kind == "gating":
        options = GatingOptions(markers=inp.markers, parallel_markers=inp.parallel_markers, **common)
    elif inp.kind == "contrast":
        options = GatingOptions(markers=inp.markers, **common)
    else:
        options = QCOptions(channels=inp.channels, parallel_markers=inp.parallel_markers, **common)
    if inp.units_per_worker:
        options = replace(options, units_per_worker=inp.units_per_worker)
    relay = _Relay(call, inp)
    summary = RUNS[inp.kind](options, gateway=gateway, session=call.session, on_event=relay, run_id=run_id,
                             policy=call.policy, audit=call.audit, link=call.link, notify=call.notify,
                             check=call.check_cancelled).run()
    return summary, relay


# -- ai.run_status --------------------------------------------------------------------------


class StatusInput(AgentModel):
    run_id: str | None = Field(None, description="A run id (air_...) or the job id that runs it "
                               "(job_...). Omit to list the recent runs.")
    limit: int = Field(20, ge=1, le=100)
    project: str | None = Field(None, description="With resumable: the project whose sessions "
                                "to list.")
    resumable: bool = Field(False, description="List the project's sessions that stopped part-way "
                            "(paused, failed, or cut off by a restart) and can be picked up with "
                            "ai_run_session(resume_session=...): the newest session of each kind, "
                            "when it is not finished.")


def _row(row) -> dict:
    import json

    out = {k: row.get(k) for k in ("run_id", "kind", "project", "session_id", "gateway_run_id", "billing",
                                   "status", "started_at", "finished_at")}
    out["job_id"] = job_id_for(row["run_id"])
    summary = json.loads(row.get("summary_json") or "null")
    if summary:
        out["reason"] = summary.get("reason")
        out["charged_credits"] = summary.get("charged_credits")
        out["packets"] = summary.get("packets")
    return out


def _live(trace, row) -> dict:
    """What a run has spent so far, from its trace (complete while it runs)."""
    report = trace.cache_report(row["run_id"])
    return {"model_calls": report["calls"], "charged_micro": report["charged_micro"],
            "charged_credits": round(report["charged_micro"] / CREDIT_MICRO, 2),
            "cache_read_share": report["cache_read_share"], "verdicts": report["verdicts"],
            "invalid_answers": report["invalid_answers"]}


def run_status(call, inp):
    import json

    from plexora.agent import jobs
    from plexora.ai.harness.trace import TraceStore

    trace = TraceStore()
    if inp.resumable:
        if not inp.project:
            raise AgentError("invalid_input", "resumable needs a project")
        return {"project": inp.project, "resumable": resumable_sessions(inp.project, trace=trace)}
    if not inp.run_id:
        runs = [_row(r) for r in trace.runs(limit=inp.limit) if r["kind"] in RUN_KINDS]
        return {"runs": runs}
    run_id = run_id_for(inp.run_id) if inp.run_id.startswith("job_") else inp.run_id
    job = jobs.store().get(job_id_for(run_id))
    row = trace.run(run_id)
    if row is None:
        if job is None:
            raise AgentError("invalid_input", f"no Plexora AI run {inp.run_id!r}")
        # Queued, or ended before the session began (no licence, say).
        error = job.get("error") or {}
        return {"run_id": run_id, "job_id": job["job_id"], "status": {
            "queued": "queued", "running": "starting"}.get(job["status"], job["status"]),
                "reason": error.get("message"), "error": error or None, "job": jobs.public(job)}
    out = _row(row)
    out["usage"] = _live(trace, row)
    summary = json.loads(row.get("summary_json") or "null")
    if summary:
        out["summary"] = summary
    if job is not None:
        out["job"] = {k: job.get(k) for k in ("job_id", "status", "progress", "error")}
        # A job that is no longer running (it failed, was cancelled, or the
        # server that ran it restarted) leaves its trace row `running`.
        if job.get("status") in ("failed", "interrupted", "cancelled") \
                and row["status"] == "running":
            out["status"] = "failed" if job.get("status") == "failed" else "interrupted"
            out["reason"] = (job.get("error") or {}).get("message")
    if out["status"] == "running" and row.get("session_id"):
        live = _session_snapshot(row["kind"], row["session_id"])
        if live:
            out["session"] = live
    return out


#: How many of the newest runs the launcher's resume offer looks through.
RESUMABLE_SCAN = 200
#: A run's job in these states is still driving its session: nothing to offer.
_LIVE_JOB = ("queued", "running")


def resumable_sessions(project, *, trace=None, scan=RESUMABLE_SCAN) -> list:
    """The sessions of `project` the launcher offers to resume, newest first:
    for each kind, its newest session, when the run that last drove it ended
    before the session did (paused, failed, stopped, or cut off when Plexora
    closed) and the session itself is not finished. A session a live job is
    still answering is the agent panel's to show, not this list's; a kind
    whose newest session finished has nothing to resume -- starting afresh
    supersedes an older one.

    [{kind, project, session_id, run_id, status, reason, stopped_at,
      charged_credits, phase, progress, resume}] -- `resume` is the
    `ai.run_session` arguments that pick it up."""
    import json

    from plexora.agent import jobs
    from plexora.ai.harness.trace import TraceStore

    trace = trace or TraceStore()
    decided, seen, out = set(), set(), []
    for row in trace.runs(limit=scan):
        kind, session_id = row.get("kind"), row.get("session_id")
        if kind not in RUN_KINDS or row.get("project") != project or not session_id \
                or session_id in seen or kind in decided:
            continue
        seen.add(session_id)
        decided.add(kind)
        job = jobs.store().get(job_id_for(row["run_id"]))
        status = row.get("status")
        if status == "running":
            if job is None or job.get("status") in _LIVE_JOB:
                continue          # live -- or run elsewhere, where it may still be
            status = "failed" if job.get("status") == "failed" else "interrupted"
        if status == "done":
            continue
        try:
            engine, _events, schemas, _ = _session_parts(kind)
            store = engine.store()
            if not store.exists(session_id):
                continue
            record = store.load(session_id)
        except Exception:         # a session that cannot be read cannot be resumed
            continue
        if record.get("state") in schemas.FINISHED_STATES:
            continue
        summary = json.loads(row.get("summary_json") or "null") or {}
        snapshot = _session_snapshot(kind, session_id, record=record) or {}
        out.append({"kind": kind, "project": project, "session_id": session_id,
                    "run_id": row["run_id"], "status": status,
                    "reason": summary.get("reason") or ((job or {}).get("error") or {}).get("message"),
                    "stopped_at": row.get("finished_at") or row.get("started_at"),
                    "charged_credits": summary.get("charged_credits"),
                    "phase": snapshot.get("phase"), "progress": snapshot.get("progress"),
                    "resume": {"kind": kind, "project": project, "resume_session": session_id}})
    return out


def _session_snapshot(kind, session_id, record=None) -> dict | None:
    """What a reloaded tab needs to put a running session's card back
    before its next event: the control route, the phase, the counts and
    whether it is paused. None once the session is gone. `record`: the
    session's, when the caller has read it already."""
    try:
        engine, events, schemas, _ = _session_parts(kind)
        store = engine.store()
        if record is None:
            if not store.exists(session_id):
                return None
            record = store.load(session_id)
        control = store.control(session_id)
    except Exception:
        return None
    units = list((record.get("units") or {}).values())
    terminal = set(schemas.TERMINAL_STATES)
    progress = {"units_done": sum(1 for u in units if u.get("state") in terminal),
                "units_total": len(units)}
    if any("type" in u for u in units):
        by_type = {}
        for unit in units:
            bucket = by_type.setdefault(unit.get("type"), {"done": 0, "total": 0})
            bucket["total"] += 1
            bucket["done"] += int(unit.get("state") in terminal)
        progress["by_type"] = by_type
    if record.get("scope") == "dataset" and hasattr(engine, "dataset_progress"):
        # Where each image of a dataset is: the switcher's badges and the
        # panel's "gating 3 of 8 images" before the next event.
        progress["dataset_progress"] = engine.dataset_progress(record)
    mirror = record.get("mirror") or {}
    mirroring = bool(mirror.get("enabled")) and mirror.get("status") != "off"
    # The packets out now, oldest first: a reloaded tab (or one that missed
    # events) lists every look under way, not just the newest.
    held = record.get("outstanding") or {}
    in_flight = [{"packet_id": pid, "kind": entry.get("kind"), "reader": entry.get("reader"),
                  "units": list(entry.get("units") or ())}
                 for pid, entry in sorted(held.items(), key=lambda kv: kv[1].get("seq", 0))]
    progress["in_flight"] = len(in_flight)
    return {"session_id": session_id, "control": events.control_for(session_id),
            "in_flight": in_flight,
            "scope": record.get("scope"), "images": list(record.get("images") or ()),
            "phase": engine.phase_for(record), "progress": progress,
            "paused": bool(control.get("paused")), "paused_by": control.get("paused_by"),
            "stopped": bool(control.get("stopped")),
            "viewer_attached": mirroring and not control.get("viewer_detached"),
            "view_id": mirror.get("view_id"),
            "mirror": {"status": mirror.get("status"), "view_id": mirror.get("view_id"),
                       "follow": mirror.get("follow")}}


# -- ai.run_control --------------------------------------------------------------------------


class ControlInput(AgentModel):
    run_id: str = Field(description="A run id (air_...) or its job id (job_...).")
    action: Literal["pause", "resume", "stop"] = Field(
        description="pause the session at its next packet (the run waits) / resume it; stop "
                    "it (what it wrote so far stays, undoable). A run paused for credit is "
                    "resumed with ai.run_session(resume_session=...) instead.")


def _session_parts(kind):
    if kind == "qc":
        from plexora.plugins.qc.capabilities_session import record_limit_answers
        from plexora.plugins.qc.server import engine, events, schemas
    else:
        from plexora.plugins.gating.capabilities_session import record_limit_answers
        from plexora.plugins.gating.server.autogate import engine, events, schemas
    return engine, events, schemas, record_limit_answers


def run_control(call, inp):
    from plexora.agent.receipts import make_receipt
    from plexora.agent.sessions import control as session_control
    from plexora.ai.harness.trace import TraceStore

    run_id = run_id_for(inp.run_id) if inp.run_id.startswith("job_") else inp.run_id
    row = TraceStore().run(run_id)
    if row is None or row["kind"] not in RUN_KINDS:
        raise AgentError("invalid_input", f"no Plexora AI run {inp.run_id!r}")
    if not row.get("session_id"):
        if inp.action != "stop":
            raise AgentError("conflict", "this run has no session yet", retryable=True)
        # Still collecting its set-up (the user's form) or reading the note:
        # nothing started, quoted or asked of a model, so stopping is
        # cancelling its job; the run ends `stopped` at its next check.
        from plexora.agent import jobs

        before, after = jobs.store().cancel(job_id_for(run_id))
        if after != "cancelling":
            # The cancel reached nothing: the job finished, is gone, or runs
            # in the other Plexora process (MCP vs the viewer server).
            if before in jobs.TERMINAL:
                raise AgentError("conflict", f"the run already ended ({before}); nothing to stop")
            raise AgentError("conflict", "this run's job is not running in this Plexora process, so "
                                         "it could not be stopped from here; stop it where it was "
                                         "started (the viewer's agent panel or the MCP client)")
        receipt = make_receipt(call, changed=True, before=None,
                               after={"session_id": None, "action": "stop"},
                               persistent_state=STATE, reversible=False, extra={"run_id": run_id})
        return {"run_id": run_id, "session_id": None, "action": "stop",
                "control": {"stopped": True, "stopped_by": "viewer"},
                "receipt": receipt.model_dump(mode="json")}
    engine, events, schemas, record_limit_answers = _session_parts(row["kind"])
    store = engine.store()
    session_id = row["session_id"]
    if not store.exists(session_id):
        raise AgentError("invalid_input", f"the run's session {session_id} is gone")

    def tell_tabs(event, record=None, **payload):
        try:
            record = record or store.load(session_id)
        except Exception:
            return
        events.announce(call.notify, record.get("images") or [], session_id, event, **payload)

    try:
        control = session_control.handle(store, session_id, {"action": inp.action}, tell_tabs=tell_tabs,
                                         summary_of=engine.summary_of,
                                         record_limit_answers=record_limit_answers,
                                         limit_decisions=schemas.LIMIT_DECISIONS)
    except session_control.BadRequest as exc:
        raise AgentError("invalid_input", str(exc)) from None
    receipt = make_receipt(call, changed=True, before=None,
                           after={"session_id": session_id, "action": inp.action},
                           persistent_state=STATE, reversible=inp.action != "stop",
                           extra={"run_id": run_id})
    return {"run_id": run_id, "session_id": session_id, "action": inp.action,
            "control": {k: control.get(k) for k in ("paused", "paused_by", "stopped", "stopped_by")},
            "receipt": receipt.model_dump(mode="json")}


# -- ai.balance ------------------------------------------------------------------------------


class BalanceInput(AgentModel):
    project: str | None = Field(None, description="Estimate a run of this project too.")
    kind: Literal["gating", "contrast", "qc", "qc_gating", "both"] = Field(
        "both", description="The run to estimate; both: every kind.")
    markers: list[str] | None = Field(None, max_length=200)
    channels: list[str] | None = Field(None, max_length=200)
    dev: bool = False


class _EstimateSession:
    """`call.session` for one estimate: each project's handles are asked for
    once. `AgentSession.data` checks a held table is still current by walking
    its zarr store's metadata (`_zarr_fingerprint`) -- most of a second on a
    big store -- and the markers, the channel match and each kind asked for
    them again, six walks per launcher open."""

    def __init__(self, session):
        self._session = session
        self._data = {}

    def data(self, name):
        if name not in self._data:
            self._data[name] = self._session.data(name)
        return self._data[name]

    def __getattr__(self, name):
        return getattr(self._session, name)


class _EstimateCall:
    """The capability call with `_EstimateSession` as its session, and the
    marker / channel match gating and contrast share, worked out once."""

    def __init__(self, call):
        self._call = call
        self.session = _EstimateSession(call.session)
        self._matches = {}

    def __getattr__(self, name):
        return getattr(self._call, name)

    def match(self, project):
        """(markers a session would gate, `channel_match` of them)."""
        if project not in self._matches:
            from plexora.plugins.gating.capabilities_session import _markers_for, channel_match

            order, _skipped, _panel = _markers_for(self, [project], None)
            self._matches[project] = (order, channel_match(self, [project], order))
        return self._matches[project]


def _units(call, kind, project, picked) -> tuple[int, str | None, dict]:
    """How many units a run of `kind` on `project` would declare, or (0, why
    not), and what the launcher must say before it starts one: `blocked` (it
    cannot start, and why) or `confirm` (ask first; `start_options` go with a
    yes)."""
    if picked:
        return (contrast_grids(len(picked)) if kind == "contrast" else len(picked)), None, {}
    if not isinstance(call, _EstimateCall):
        call = _EstimateCall(call)
    try:
        if kind == "contrast":
            # No cell table is no refusal: the run finds the cells in the
            # image (`image_cells`), and its markers are the image's channels.
            record = call.session.project(project)
            if record.image.is_blank:
                return 0, "this sample has no image to set the contrast of", {}
            _order, match = call.match(project)
            if match["state"] == "none":
                return 0, None, {"blocked": (
                    f"None of the {match['n_markers']} markers matches an image channel. Name "
                    "the image's channels after the markers (Edit project).")}
            return contrast_grids(match["n_matched"]), None, {}
        if kind == "gating":
            record = call.session.project(project)
            if not record.has_table:
                return 0, "this project has no cell table to gate", {}
            # The markers a session would gate: context markers (a nuclear
            # stain, say) are left out, exactly as gating_session_start does,
            # and so is a marker no image channel matches (`channel_match`).
            order, match = call.match(project)
            return _gating_estimate(order, match)
        record = call.session.project(project)
        if record.image.is_blank:
            return 0, "this sample has no image to check", {}
        return len(list(record.image.real_channels)), None, {}
    except AgentError as exc:
        return 0, exc.message, {}
    except Exception as exc:  # an estimate never fails the balance
        return 0, str(exc), {}


def _gating_estimate(order, match) -> tuple[int, str | None, dict]:
    """Gating's units and launcher words from `channel_match`: automatic
    gating judges every gate on the pixels, so only markers with an image
    channel are gated -- none, and the run cannot start; some, and the user
    is asked first."""
    unmatched = sorted({m for names in match["unmatched"].values() for m in names})
    channels = {"state": match["state"], "n_markers": match["n_markers"],
                "n_matched": match["n_matched"], "unmatched": unmatched}
    if match["state"] == "none":
        return 0, None, {"channels": channels, "blocked": (
            f"None of the {match['n_markers']} markers matches an image channel. Name the "
            "image's channels after the markers (Edit project), then gate.")}
    if match["state"] == "partial":
        shown = ", ".join(unmatched[:8]) + (f" and {len(unmatched) - 8} more"
                                           if len(unmatched) > 8 else "")
        return match["n_matched"], None, {"channels": channels, "confirm": {
            "title": f"Gate {match['n_matched']} of {match['n_markers']} markers?",
            "body": (f"{len(unmatched)} {'marker has' if len(unmatched) == 1 else 'markers have'}"
                     f" no image channel ({shown}). Auto Gating judges every gate on the "
                     "pixels, so those are skipped and left ungated."),
            "confirm": f"Gate {match['n_matched']}",
            "start_options": {"force": True}}}
    return len(order), None, {"channels": channels}


def cap_block(raw, credit: int = CREDIT_MICRO) -> dict | None:
    """One monthly cap from the gateway's balance (`{limit, used, period}`, in
    micro-USD; `limit` null for none), with the credit figures a person reads."""
    if not isinstance(raw, dict):
        return None
    limit, used = raw.get("limit"), int(raw.get("used") or 0)
    block = {"limit_micro": limit, "used_micro": used, "period": raw.get("period"),
             "limit_credits": None if limit is None else round(int(limit) / credit, 2),
             "used_credits": round(used / credit, 2)}
    block["reached"] = limit is not None and used >= int(limit)
    return block


def balance(call, inp):
    from plexora import _work
    from plexora.ai.harness.gateway import GatewayError

    wanted = () if not inp.project else RUN_KINDS if inp.kind == "both" \
        else ("qc", "gating") if inp.kind == "qc_gating" else (inp.kind,)

    try:
        gateway = _gateway(inp.dev)
    except GatewayError as exc:
        raise AgentError("license_required" if exc.code in ("no_license", "ai_not_entitled")
                         else "resource_unavailable", str(exc),
                         detail={"code": exc.code, "retryable": exc.retryable}) from None
    # The balance and the prices are each a round trip to the gateway (up to
    # ~2 s apiece), and the units are read from the project here: all three
    # side by side, so the launcher waits for the slowest, not their sum.
    # `_work.executor`: threads here, each call inline on the website.
    with _work.executor(max_workers=2, thread_name_prefix="ai-balance") as pool:
        asked_balance = pool.submit(gateway.balance)
        asked_prices = pool.submit(gateway.pricing)
        estimating = _EstimateCall(call)
        counted = {}
        for kind in wanted:
            picked = inp.markers if kind in ("gating", "contrast") else inp.channels
            counted[kind] = _units(estimating, kind, inp.project, picked)
        try:
            account, prices = asked_balance.result(), asked_prices.result()
        except GatewayError as exc:
            raise AgentError("license_required" if exc.code in ("no_license", "ai_not_entitled")
                             else "resource_unavailable", str(exc),
                             detail={"code": exc.code, "retryable": exc.retryable}) from None
    credit = int(prices.get("credit_micro") or CREDIT_MICRO)
    action_url = account.get("action_url") or account.get("top_up_url")
    out = {"org_id": account.get("org_id"), "mode": account.get("mode"),
           "available_micro": int(account.get("available_micro") or 0),
           "available_credits": round(int(account.get("available_micro") or 0) / credit, 2),
           "held_micro": account.get("held_micro"),
           # The caps on this caller: the member's own monthly limit and
           # Plexora's within the organisation (B10's balance shape).
           "member": cap_block(account.get("member"), credit),
           "product": cap_block(account.get("product_cap") if isinstance(account.get("product_cap"), dict)
                                else account.get("product"), credit),
           "action_url": action_url, "action_label": account.get("action_label"),
           "top_up_url": action_url,
           "features": {name: {k: f.get(k) for k in ("unit", "credits")}
                        for name, f in (prices.get("features") or {}).items() if name in ("gating", "qc")}}
    if inp.project:
        estimates = {}
        for kind in wanted:
            # A contrast grid is priced as one gating unit (ContrastWorkflow).
            price = (prices.get("features") or {}).get(
                "gating" if kind == "contrast" else kind) or {}
            units, why, words = counted[kind]
            credits = int(price.get("credits") or 0) * units
            unit = "contrast grid" if kind == "contrast" else price.get("unit")
            estimates[kind] = {"units": units, "unit": unit, "credits": credits,
                               "affordable": credits * credit <= out["available_micro"],
                               **({"unavailable": why} if why else {}), **words}
        if inp.kind in ("both", "qc_gating"):
            estimates["qc_gating"] = _both_estimate(estimates.get("qc") or {},
                                                    estimates.get("gating") or {},
                                                    out["available_micro"], credit)
            if inp.kind == "qc_gating":
                estimates = {"qc_gating": estimates["qc_gating"]}
        out["estimates"] = estimates
        out["charging"] = CHARGING
        out["project"] = inp.project
    return out


def _both_estimate(qc: dict, gating: dict, available_micro: int, credit: int) -> dict:
    """A `qc_gating` run's quote: QC's credits and gating's together, counted
    in markers (the launcher's line), with gating's `blocked` or `confirm`
    (the markers without an image channel are gating's question). Either half
    unavailable: the run is too."""
    credits = int(qc.get("credits") or 0) + int(gating.get("credits") or 0)
    out = {"units": int(gating.get("units") or 0), "unit": gating.get("unit") or "marker",
           "credits": credits, "affordable": credits * credit <= available_micro,
           "channels_checked": int(qc.get("units") or 0)}
    why = qc.get("unavailable") or gating.get("unavailable")
    if why or not qc.get("units"):
        out["unavailable"] = why or "QC has no channel to check here"
    for key in ("blocked", "confirm", "channels"):
        if gating.get(key) is not None:
            out[key] = gating[key]
    return out


# -- the capabilities ---------------------------------------------------------------------------


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        kwargs.setdefault("entitlement", ENTITLEMENT)
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        cap(name="ai.run_session", tool_name="ai_run_session",
            purpose="Gate the project's markers or quality-control its image with Plexora AI, inside "
                    "Plexora: the session runs on a job and every decision is a model call, paid from "
                    "the organisation's AI credit wallet. The run is quoted before anything is spent "
                    "(ai_balance's estimate: the price per marker or channel times their number) and "
                    "the quote is a ceiling -- up to that many credits: each call is metered and the "
                    "run is charged what its calls cost, never more. An open viewer's agent panel "
                    "shows it. Resume a session paused for credit with resume_session.",
            permission="reversible_write", input_model=RunInput, handler=run_session, execution="job",
            writes=("gates", "rois", "qc"), persistent=True, egress="rendered_pixels",
            reads=("image", "table", "mask", "rois")),
        cap(name="ai.run_status", tool_name="ai_run_status",
            purpose="A Plexora AI run: its state, the session it drives, packets, credits charged and "
                    "the cache-read share; or the recent runs.",
            permission="read", input_model=StatusInput, handler=run_status, egress="aggregates"),
        cap(name="ai.run_control", tool_name="ai_run_control",
            purpose="Pause, resume or stop a running Plexora AI run's session (what it wrote stays).",
            permission="reversible_write", input_model=ControlInput, handler=run_control,
            persistent=True, egress="metadata"),
        cap(name="ai.balance", tool_name="ai_balance",
            purpose="The organisation's AI credit wallet (every product's AI draws on it), the price "
                    "per marker / channel, and what a Plexora AI run of a project would be quoted: up "
                    "to that many credits, charged as metered.",
            permission="read", input_model=BalanceInput, handler=balance, egress="metadata"),
    ]
