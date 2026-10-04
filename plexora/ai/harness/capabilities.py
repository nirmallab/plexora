"""Plexora AI runs as capabilities: start one, read it, steer it, see the balance.

    ai.run_session  (job)  gate or QC the open project with the harness: the
                           decision loop of `decision.py` on a job thread,
                           its calls billed through the gateway
    ai.run_status          one run (by run id or job id), or the recent ones
    ai.run_control         pause / resume / stop the run's session
    ai.balance             the account's credit, the price list, and what a
                           run of this project would be quoted

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
AI_EVENTS = ("ai_run", "ai_context", "ai_usage", "ai_paused", "ai_finished")
#: Credits are 10,000 micro-dollars (the gateway's `credit_micro`).
CREDIT_MICRO = 10_000


def run_id_for(job_id: str) -> str:
    return "air_" + str(job_id).removeprefix("job_")


def job_id_for(run_id: str) -> str:
    return "job_" + str(run_id).removeprefix("air_")


def _gateway(dev: bool = False):
    from plexora.ai.harness.gateway import GatewayClient

    return GatewayClient(dev=True if dev else None)


def _workflow(kind):
    from plexora.ai.harness.decision import WORKFLOWS

    return WORKFLOWS[kind]


# -- ai.run_session ------------------------------------------------------------------------


class RunInput(AgentModel):
    kind: Literal["gating", "qc"] = Field(description="gating: Auto Gating of the project's "
                                                      "markers; qc: AutoQC of its image.")
    project: str
    markers: list[str] | None = Field(None, max_length=200, description="gating: the markers "
                                      "(default: every marker of the table).")
    channels: list[str] | None = Field(None, max_length=200, description="qc: the channels "
                                       "(default: every channel of the image).")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: gates / regions are written as they are decided; propose: "
                             "nothing is written until the session is committed.")
    resume_session: str | None = Field(None, description="Continue this paused session (after "
                                       "a credit pause, say) instead of starting one.")
    dev: bool = Field(False, description="The gateway's dev route (internal testing accounts "
                                         "only; billed at provider cost).")
    model: str | None = Field(None, max_length=80, description="With dev only: the model to use.")
    units_per_worker: int | None = Field(None, ge=1, le=16, description="Units one rolling "
                                         "worker covers (default: 1 marker, 4 QC units).")
    parallel_markers: int | None = Field(None, ge=1, le=8, description="gating: markers answered "
                                         "side by side, each lane with its own workers (default 1). "
                                         "The gates are the serial run's; more lanes finish "
                                         "sooner.")
    start_options: dict | None = Field(None, description="Extra options for the session's start "
                                       "(the workflow's own, e.g. QC's map_cell_um).")
    context: str | None = Field(None, max_length=1000, description="gating: the user's note about "
                                "the sample, as typed (\"melanoma from skin\", \"only gate CD3 and "
                                "CD8\"). A cheap text model normalises it (plexora.ai.context) into the "
                                "session's biology; it narrows the markers only when it says so "
                                "explicitly. Empty: no call, every marker.")


class _Relay:
    """The harness's events, told to the job's progress and to the tabs."""

    def __init__(self, call, inp):
        self.call = call
        self.inp = inp
        self.wf = _workflow(inp.kind)
        self.job_id = (call.job or {}).get("job_id")

    def _tell(self, event, session_id, **fields):
        notify = self.call.notify
        if notify is None or not session_id:
            return
        try:
            channel = self.wf.events()
            body = {"session_id": session_id, "event": event, "control": channel.control_for(session_id),
                    "workflow": self.wf.name, "job_id": self.job_id, **fields}
            notify(self.inp.project, channel.owner, channel.kind, body)
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
            self._tell("ai_run", session_id, quote_credits=event.get("quote_credits"),
                       resumed=name == "resumed", **run)
        elif name == "answered":
            self.call.progress(usage.get("packets"), None,
                               f"{usage.get('packets')} packets, {usage.get('charged_credits')} credits")
            self._tell("ai_usage", session_id, usage=usage, **run)
        elif name == "parked":
            # The user's pause, in the viewer: the plugin's control route has
            # told the tabs already; the run waits on its thread.
            self.call.progress(None, None, "paused in the viewer")
        elif name == "paused":
            self._tell("ai_paused", session_id, reason=event.get("reason"), message=event.get("message"),
                       top_up_url=event.get("top_up_url"), usage=usage,
                       resume={"kind": self.inp.kind, "project": self.inp.project,
                               "resume_session": session_id}, **run)
        elif name == "finished":
            self._tell("ai_finished", session_id, status=event.get("status"), reason=event.get("reason"),
                       usage=usage, resume={"kind": self.inp.kind, "project": self.inp.project,
                                            "resume_session": session_id}, **run)


def run_session(call, inp):
    from dataclasses import replace

    from plexora.ai.harness.decision import RUNS, GatingOptions, QCOptions
    from plexora.ai.harness.gateway import GatewayError

    if inp.model and not inp.dev:
        raise AgentError("invalid_input", "a model may be named only with dev=true")
    common = {"project": inp.project, "mode": inp.mode, "model": inp.model,
              "resume_session": inp.resume_session, "start_options": dict(inp.start_options or {}),
              "context": inp.context if inp.kind == "gating" else None}
    if inp.kind == "gating":
        options = GatingOptions(markers=inp.markers, parallel_markers=inp.parallel_markers or 1, **common)
    else:
        options = QCOptions(channels=inp.channels, **common)
    if inp.units_per_worker:
        options = replace(options, units_per_worker=inp.units_per_worker)
    run_id = run_id_for(call.job["job_id"]) if call.job else None
    relay = _Relay(call, inp)
    try:
        gateway = _gateway(inp.dev)
    except GatewayError as exc:
        raise AgentError("license_required" if exc.code == "no_license" else "resource_unavailable",
                         str(exc)) from None
    summary = RUNS[inp.kind](options, gateway=gateway, session=call.session, on_event=relay, run_id=run_id,
                             policy=call.policy, audit=call.audit, link=call.link, notify=call.notify,
                             check=call.check_cancelled).run()
    return {**summary, "job_id": relay.job_id}


# -- ai.run_status --------------------------------------------------------------------------


class StatusInput(AgentModel):
    run_id: str | None = Field(None, description="A run id (air_...) or the job id that runs it "
                               "(job_...). Omit to list the recent runs.")
    limit: int = Field(20, ge=1, le=100)


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
    if not inp.run_id:
        runs = [_row(r) for r in trace.runs(limit=inp.limit) if r["kind"] in ("gating", "qc")]
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


def _session_snapshot(kind, session_id) -> dict | None:
    """What a reloaded tab needs to put a running session's card back
    before its next event: the control route, the phase, the counts and
    whether it is paused. None once the session is gone."""
    try:
        engine, events, schemas, _ = _session_parts(kind)
        store = engine.store()
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
    mirror = record.get("mirror") or {}
    mirroring = bool(mirror.get("enabled")) and mirror.get("status") != "off"
    return {"session_id": session_id, "control": events.control_for(session_id),
            "phase": engine.phase_for(record), "progress": progress,
            "paused": bool(control.get("paused")), "paused_by": control.get("paused_by"),
            "stopped": bool(control.get("stopped")),
            "viewer_attached": mirroring and not control.get("viewer_detached"),
            "view_id": mirror.get("view_id"),
            "mirror": {"status": mirror.get("status"), "view_id": mirror.get("view_id")}}


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
    if row is None or row["kind"] not in ("gating", "qc"):
        raise AgentError("invalid_input", f"no Plexora AI run {inp.run_id!r}")
    if not row.get("session_id"):
        raise AgentError("conflict", "this run has no session yet", retryable=True)
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
    kind: Literal["gating", "qc", "both"] = "both"
    markers: list[str] | None = Field(None, max_length=200)
    channels: list[str] | None = Field(None, max_length=200)
    dev: bool = False


def _units(call, kind, project, picked) -> tuple[int, str | None]:
    """How many units a run of `kind` on `project` would declare, or (0, why not)."""
    if picked:
        return len(picked), None
    try:
        if kind == "gating":
            record = call.session.project(project)
            if not record.has_table:
                return 0, "this project has no cell table to gate"
            # The markers a session would gate: context markers (a nuclear
            # stain, say) are left out, exactly as gating_session_start does.
            from plexora.plugins.gating.capabilities_session import _markers_for

            order, _skipped, _panel = _markers_for(call, [project], None)
            return len(order), None
        record = call.session.project(project)
        if record.image.is_blank:
            return 0, "this sample has no image to check"
        return len(list(record.image.real_channels)), None
    except AgentError as exc:
        return 0, exc.message
    except Exception as exc:  # an estimate never fails the balance
        return 0, str(exc)


def balance(call, inp):
    from plexora.ai.harness.gateway import GatewayError

    try:
        gateway = _gateway(inp.dev)
        account = gateway.balance()
        prices = gateway.pricing()
    except GatewayError as exc:
        raise AgentError("license_required" if exc.code in ("no_license", "ai_not_entitled")
                         else "resource_unavailable", str(exc),
                         detail={"code": exc.code, "retryable": exc.retryable}) from None
    credit = int(prices.get("credit_micro") or CREDIT_MICRO)
    out = {"account_id": account.get("account_id"), "mode": account.get("mode"),
           "available_micro": int(account.get("available_micro") or 0),
           "available_credits": round(int(account.get("available_micro") or 0) / credit, 2),
           "held_micro": account.get("held_micro"), "top_up_url": account.get("top_up_url"),
           "features": {name: {k: f.get(k) for k in ("unit", "credits")}
                        for name, f in (prices.get("features") or {}).items() if name in ("gating", "qc")}}
    if inp.project:
        estimates = {}
        for kind in (("gating", "qc") if inp.kind == "both" else (inp.kind,)):
            price = (prices.get("features") or {}).get(kind) or {}
            picked = inp.markers if kind == "gating" else inp.channels
            units, why = _units(call, kind, inp.project, picked)
            credits = int(price.get("credits") or 0) * units
            estimates[kind] = {"units": units, "unit": price.get("unit"), "credits": credits,
                               "affordable": credits * credit <= out["available_micro"],
                               **({"unavailable": why} if why else {})}
        out["estimates"] = estimates
        out["project"] = inp.project
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
                    "Plexora: the session runs on a job and every decision is a model call billed in "
                    "Plexora AI credits (quoted per marker or channel before anything is spent). An "
                    "open viewer's agent panel shows it. Resume a session paused for credit with "
                    "resume_session.",
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
            purpose="The account's Plexora AI credit, the price per marker / channel, and what a run "
                    "of a project would be quoted.",
            permission="read", input_model=BalanceInput, handler=balance, egress="metadata"),
    ]
