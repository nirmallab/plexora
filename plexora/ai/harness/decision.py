"""The decision loop: an Auto Gating or AutoQC session answered by the harness.

Deterministic Python is the coordinator. The model never sees a tool list:
each packet the session engine serves becomes ONE structured-output call
whose schema is that packet kind's answer model, and the answer is validated
locally before `<kind>_answer` gets it.

One loop serves every decision workflow. A `Workflow` names what differs --
the session's capabilities, the plugins they live in, the answer models, the
cached prefix, what a "unit" of a packet is, the gateway feature -- and
`DecisionRun` is the rest: `GatingRun` (feature `gating`, units = markers)
and `QCRun` (feature `qc`, units = channels) are the two bindings.

Rolling workers keep the cost linear. A worker is a fresh message list that
starts from the cached prefix (identity + skill + reading guide, `prefix.py`)
and carries only its own packets; it is retired after `units_per_worker`
units, `max_packets_per_worker` packets or `context_tokens_per_worker` tokens
of context, whichever comes first. One conversation answering n packets
re-reads ~n^2 tokens (the 2026-10-01 live run: 480k tokens of context by the
end); a worker per marker keeps every call under ~25k.

Money: the session is declared to the gateway as a run (one unit per marker
or channel), so the user is quoted and capped before anything is spent. When
the gateway refuses for credit, the session is PAUSED, not abandoned:
`resume_session=` picks it up where it stopped (and lifts the pause).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from plexora.ai.harness import cache_plan, prefix, schema
from plexora.ai.harness.gateway import GatewayClient, GatewayError
from plexora.ai.harness.trace import TraceStore
from plexora.ai.harness.wire import ModelRequest, canonical, image_block, text_block

log = logging.getLogger("plexora.ai.harness")

#: Packet fields the model does not need: the schema is in the guide and in
#: `output_schema`; the budget, narration and mirror are for the people
#: watching, `answer_with` for an agent that calls tools.
HIDDEN = ("answer_schema", "budget", "narration", "answer_with", "mirror")
#: Refusals that pause the session for the user instead of failing it.
PAUSE_CODES = ("insufficient_credits", "run_envelope_exceeded", "run_closed", "spend_cap_reached",
               "ai_disabled", "ai_not_entitled", "dev_not_allowed", "capability_not_allowed", "no_license")
FINISHED = ("done", "cancelled", "rolled_back", "failed")
#: How a run may end and still be resumed: its gateway run stays open.
KEPT_OPEN = ("paused", "waiting_for_user")
TOKENS_PER_IMAGE = 1600


# -- the workflows -----------------------------------------------------------------------


class Workflow:
    """What one decision workflow binds the loop to."""

    name = ""
    plugins: tuple = ()
    START = NEXT = ANSWER = STATUS = FINISH = ""
    #: The gateway's run feature and the request context's names.
    feature = ""
    agent = ""
    workflow = ""
    unit_noun = "unit"

    def prefix(self) -> list:
        raise NotImplementedError

    def guide_version(self) -> str:
        raise NotImplementedError

    def selected(self, options) -> list | None:
        """The units the user picked (markers, channels), or None for all."""
        return None

    def start_arguments(self, options) -> dict:
        raise NotImplementedError

    def units_started(self, started: dict, options) -> int:
        return int(started.get("n_units") or len(self.selected(options) or ()) or 1)

    def units_of(self, packet: dict) -> set:
        """The names a packet concerns, for worker rotation."""
        raise NotImplementedError

    def answer_models(self):
        raise NotImplementedError

    def events(self):
        """The workflow's `plexora.agent.sessions.events.Events` binding."""
        raise NotImplementedError

    def schema_for(self, kind: str) -> dict | None:
        return schema.for_kind(kind, self.name)

    def model_schema(self, kind: str) -> dict | None:
        return schema.model_schema(kind, self.name)


class GatingWorkflow(Workflow):
    name = "gating"
    plugins = ("gating",)
    START, NEXT, ANSWER = "gating_session_start", "gating_next", "gating_answer"
    STATUS, FINISH = "gating_session_status", "gating_session_finish"
    feature, agent, workflow, unit_noun = "gating", "gating_worker", "auto_gating", "marker"

    def prefix(self):
        return prefix.gating_prefix()

    def guide_version(self):
        return prefix.guide_version()

    def selected(self, options):
        return getattr(options, "markers", None)

    def start_arguments(self, options):
        arguments = {"scope": "project", "project": options.project, "mode": options.mode, "reading": "once",
                     "agent": f"plexora-harness:{options.capability}", "known_guide": self.guide_version()}
        if self.selected(options):
            arguments["markers"] = list(self.selected(options))
        return arguments

    def units_of(self, packet):
        return {str(u.get("marker")) for u in packet.get("units") or () if isinstance(u, dict)}

    def answer_models(self):
        from plexora.plugins.gating.server.autogate import answers

        return answers.Answer

    def events(self):
        from plexora.plugins.gating.server.autogate import events

        return events.EVENTS


class QCWorkflow(Workflow):
    name = "qc"
    plugins = ("roi", "qc")
    START, NEXT, ANSWER = "qc_session_start", "qc_next", "qc_answer"
    STATUS, FINISH = "qc_session_status", "qc_session_finish"
    feature, agent, workflow, unit_noun = "qc", "qc_worker", "auto_qc", "channel"

    def prefix(self):
        return prefix.qc_prefix()

    def guide_version(self):
        return prefix.qc_guide_version()

    def selected(self, options):
        return getattr(options, "channels", None)

    def start_arguments(self, options):
        arguments = {"project": options.project, "mode": options.mode, "reading": "once",
                     "agent": f"plexora-harness:{options.capability}", "known_guide": self.guide_version()}
        if self.selected(options):
            arguments["channels"] = list(self.selected(options))
        return arguments

    def units_started(self, started, options):
        # The gateway's QC unit is a channel (catalog.ts FEATURES.qc); the
        # session's own units also count checks, cell modules and the final look.
        return max(1, len(started.get("channels") or self.selected(options) or ()))

    def units_of(self, packet):
        return {f"{u.get('type')}:{u.get('id')}" for u in packet.get("units") or () if isinstance(u, dict)}

    def answer_models(self):
        from plexora.plugins.qc.server import answers

        return answers.Answer

    def events(self):
        from plexora.plugins.qc.server import events

        return events.EVENTS


GATING = GatingWorkflow()
QC = QCWorkflow()
WORKFLOWS = {"gating": GATING, "qc": QC}


# -- options ---------------------------------------------------------------------------


@dataclass
class DecisionOptions:
    project: str
    mode: str = "apply"
    capability: str = "vision_judgement"
    model: str | None = None                 # dev route only
    units_per_worker: int = 1
    max_packets_per_worker: int = 8
    context_tokens_per_worker: int = 60_000
    max_tokens: int = 4096
    declare_run: bool = True
    max_packets: int = 2000
    wait_s: float = 20.0
    start_options: dict = field(default_factory=dict)
    resume_session: str | None = None


@dataclass
class GatingOptions(DecisionOptions):
    markers: list | None = None


@dataclass
class QCOptions(DecisionOptions):
    channels: list | None = None
    #: A QC packet's units are candidate regions, checks and cell modules,
    #: several per channel: four of them share a worker.
    units_per_worker: int = 4


class _Worker:
    def __init__(self, index: int):
        self.index = index
        self.messages: list = []
        self.units: set = set()
        self.packets = 0
        self.calls = 0
        self.chars = 0
        self.images = 0

    def tokens(self) -> int:
        return int(self.chars / cache_plan.CHARS_PER_TOKEN) + self.images * TOKENS_PER_IMAGE


def _ok(result: dict) -> dict:
    if not result.get("ok"):
        error = result.get("error") or {}
        raise RuntimeError(f"{error.get('code', 'error')}: {error.get('message') or error.get('title') or error}")
    return result["result"]


class Stopped(Exception):
    """Raised by a run's `check` hook: the job running it was cancelled."""


class DecisionRun:
    """One decision session answered by the harness, start (or resume) to finish."""

    WORKFLOW: Workflow = GATING

    def __init__(self, options: DecisionOptions, *, gateway: GatewayClient, trace: TraceStore | None = None,
                 session=None, on_event=None, run_id: str | None = None, workflow: Workflow | None = None,
                 policy=None, audit=None, link=None, notify=None, check=None):
        self.o = options
        self.wf = workflow or self.WORKFLOW
        self.gateway = gateway
        self.trace = trace or TraceStore()
        self.run_id = run_id or f"air_{uuid.uuid4().hex[:12]}"
        self.on_event = on_event or (lambda event: None)
        self.monitor = cache_plan.CacheMonitor()
        self.system = self.wf.prefix()
        self.prefix_fp = cache_plan.fingerprint(self.system)
        self.prefix_tokens = cache_plan.expected_tokens(self.system)
        self.session_id: str | None = options.resume_session
        self.gateway_run: dict | None = None
        self.worker = _Worker(0)
        self.workers = 1
        self.seq = 0
        self.packets = 0
        self.invalid = 0
        self.charged = 0
        self.attempts: dict = {}
        self.units = 0
        # How capabilities are invoked: in-process (the app's job) passes its
        # call's policy, audit, viewer link and notifier, so the session's
        # events reach the open tabs and the receipts are the job's own.
        self.policy, self.audit, self.link, self.notify = policy, audit, link, notify
        self.check = check or (lambda: None)
        if session is None:
            from plexora.agent.session import AgentSession

            session = AgentSession()
        self.session = session

    # -- registry --------------------------------------------------------------

    def _invoke(self, name: str, arguments: dict) -> dict:
        from plexora.agent.registry import invoke

        return invoke(self.session, name, arguments, policy=self.policy, audit=self.audit, link=self.link,
                      notify=self.notify)

    def _emit(self, event: str, **fields) -> None:
        self.on_event({"event": event, "run_id": self.run_id, "session_id": self.session_id,
                       "workflow": self.wf.name, "project": self.o.project, **fields})

    def usage(self) -> dict:
        """What the run has spent so far, for a progress line."""
        return {"packets": self.packets, "model_calls": self.seq, "charged_micro": self.charged,
                "charged_credits": round(self.charged / 10_000, 2),
                "cache_read_share": round(self.monitor.read_share(), 4), "workers": self.workers,
                "invalid_answers": self.invalid}

    def _next(self) -> dict:
        self.check()
        return _ok(self._invoke(self.wf.NEXT, {"session_id": self.session_id, "wait_s": self.o.wait_s}))

    # -- the loop ----------------------------------------------------------------

    def run(self) -> dict:
        from plexora.agent import registry

        registry.discover(list(self.wf.plugins))
        self.trace.start_run(self.run_id, self.wf.name, project=self.o.project, capability=self.o.capability,
                             billing="dev" if self.gateway.dev else "credits", session_id=self.session_id)
        status, reason = "failed", None
        try:
            result = self._start()
            status, reason = self._loop(result)
        except GatewayError as exc:
            status, reason = ("paused", exc.code) if exc.code in PAUSE_CODES else ("failed", exc.code)
            self._pause(reason, error=exc)
        except Stopped as exc:
            status, reason = "paused", str(exc) or "cancelled"
            self._pause(reason)
        except Exception as exc:          # noqa: BLE001 -- the run record must say why
            if type(exc).__name__ == "JobCancelled":
                status, reason = "paused", "cancelled"
                self._pause(reason)
            else:
                log.exception("%s run %s failed", self.wf.name, self.run_id)
                reason = f"{type(exc).__name__}: {exc}"
        finally:
            summary = self._finish(status, reason)
        return summary

    def _start(self) -> dict:
        if self.session_id:
            # Resuming is the user's ask to go on: a pause the harness put on
            # the session (for credit) is lifted before the next packet.
            self._invoke(self.wf.STATUS, {"session_id": self.session_id, "pause": False})
            if self.o.declare_run:
                self.gateway_run = self._open_gateway_run()
            self._emit("resumed", run=(self.gateway_run or {}).get("run_id"))
            return self._next()
        started = _ok(self._invoke(self.wf.START, {**self.wf.start_arguments(self.o), **self.o.start_options}))
        self.session_id = started["session_id"]
        self.trace.update_run(self.run_id, session_id=self.session_id)
        self.units = self.wf.units_started(started, self.o)
        self._emit("started", units=self.units, unit_noun=self.wf.unit_noun)
        if self.o.declare_run:
            self.gateway_run = self.gateway.start_run(self.wf.feature, self.units, self.session_id)
            self.trace.update_run(self.run_id, gateway_run_id=self.gateway_run.get("run_id"))
            self._emit("quoted", quote_credits=self.gateway_run.get("quote_credits"),
                       run=self.gateway_run.get("run_id"))
        if started.get("packet") is not None:
            return {"state": started.get("state", "decision"), "packet": started["packet"],
                    "_images": started.get("_images") or []}
        return self._next()

    def _open_gateway_run(self) -> dict | None:
        """The gateway run a paused session was declared under, when the run
        that paused it left it open: a resume is the same quote, not a second
        one. None when there is none (the resume then runs undeclared, metered
        per call)."""
        for row in self.trace.runs(limit=200):
            if row["run_id"] == self.run_id or row.get("session_id") != self.session_id:
                continue
            if row.get("gateway_run_id") and row.get("status") in KEPT_OPEN:
                self.trace.update_run(self.run_id, gateway_run_id=row["gateway_run_id"])
                return {"run_id": row["gateway_run_id"]}
            break
        return None

    def _loop(self, result: dict) -> tuple[str, str | None]:
        while True:
            state = result.get("state")
            if state in ("decision", "needs_setup"):
                if self.packets >= self.o.max_packets:
                    return "failed", "max_packets"
                result = self._decide(result)
                continue
            if state == "bulk_running":
                self._emit("bulk_running", progress=result.get("progress"))
                result = self._next()
                continue
            if state == "decided":
                return "done", None
            if state in FINISHED:
                return ("done" if state == "done" else state), None
            if state == "waiting_for_user":
                self._emit("waiting_for_user", requests=result.get("requests"))
                return "waiting_for_user", "the session needs a person's answer"
            if state in ("paused", "stopped"):
                return state, result.get("reason") or result.get("note") or state
            return "failed", f"unexpected state {state!r}"

    # -- one packet ----------------------------------------------------------------

    def _rotate_if_due(self, packet: dict) -> None:
        w = self.worker
        units = self.wf.units_of(packet)
        due = w.packets >= self.o.max_packets_per_worker or w.tokens() >= self.o.context_tokens_per_worker or (
            w.units and not units <= w.units and len(w.units) >= self.o.units_per_worker)
        if due:
            self.worker = _Worker(self.workers)
            self.workers += 1
            self._emit("worker", worker=self.worker.index)

    def _content(self, packet: dict, images: list) -> list:
        shown = {k: v for k, v in packet.items() if k not in HIDDEN}
        blocks = []
        for item in images:
            data, fmt = (item.get("data"), item.get("format", "png")) if isinstance(item, dict) else (item, "png")
            if data:
                blocks.append(image_block(data, fmt))
        text = canonical(shown)
        if self.wf.schema_for(packet.get("kind", "")) is None and packet.get("answer_schema"):
            text += "\nANSWER SCHEMA: " + canonical(packet["answer_schema"])
        blocks.append(text_block(text))
        return blocks

    def _call(self, packet: dict, messages: list):
        self.check()
        kind = packet.get("kind", "")
        pid = packet.get("packet_id", "pk")
        n = self.attempts[pid] = self.attempts.get(pid, 0) + 1
        context = {"feature": self.wf.feature, "agent": self.wf.agent, "workflow": self.wf.workflow,
                   "session_id": self.session_id, "attempt": min(n, 99)}
        if self.gateway_run:
            context["run_id"] = self.gateway_run["run_id"]
        request = ModelRequest(capability=self.o.capability, system=self.system, messages=messages,
                               max_tokens=self.o.max_tokens, output_schema=self.wf.schema_for(kind),
                               context=context, model=self.o.model)
        response = self.gateway.messages(request, idempotency_key=f"{self.run_id}.{pid}.{n}")
        warm = self.worker.calls > 0
        self.worker.calls += 1
        verdict = self.monitor.observe(self.prefix_fp, self.prefix_tokens, response.usage, warm=warm)
        self.charged += response.charged_micro
        if response.run:
            self.gateway_run = {**(self.gateway_run or {}), **response.run}
        self.seq += 1
        return response, verdict

    def _validate(self, packet: dict, response) -> tuple[dict | None, str | None]:
        from pydantic import TypeAdapter, ValidationError

        try:
            answer = response.json()
        except ValueError:
            return None, "the reply was not JSON"
        if not isinstance(answer, dict):
            return None, "the reply was not a JSON object"
        answer.setdefault("kind", packet.get("kind"))
        if answer.get("kind") != packet.get("kind"):
            return answer, f"kind must be {packet.get('kind')!r}"
        source = self.wf.model_schema(answer["kind"])
        if source is not None:
            answer = schema.decode(answer, source)
        try:
            TypeAdapter(self.wf.answer_models()).validate_python(answer)
        except ValidationError as exc:
            return answer, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:6])
        return answer, None

    def _decide(self, result: dict) -> dict:
        packet = result["packet"]
        images = result.get("_images") or []
        self._rotate_if_due(packet)
        w = self.worker
        content = self._content(packet, images)
        w.messages.append({"role": "user", "content": content})
        w.chars += sum(len(b.get("text", "")) for b in content if b["type"] == "text")
        w.images += sum(1 for b in content if b["type"] == "image")

        answer, problem, response = None, None, None
        for attempt in range(2):
            response, verdict = self._call(packet, w.messages)
            answer, problem = self._validate(packet, response)
            self.trace.call(self.run_id, worker=w.index, seq=self.seq, packet_id=packet.get("packet_id"),
                            kind=packet.get("kind"), capability=self.o.capability, prefix_fp=self.prefix_fp,
                            verdict=verdict, input_uncached=response.usage.input_uncached,
                            cache_read=response.usage.cache_read, cache_write=response.usage.cache_write,
                            output_tokens=response.usage.output_tokens, price_micro=response.price_micro,
                            charged_micro=response.charged_micro, cost_micro=response.cost_micro,
                            gateway_request_id=response.gateway_request_id, latency_ms=response.latency_ms,
                            valid=problem is None)
            if problem is None or attempt == 1:
                break
            # One repair turn inside the same worker: it costs a call, not an engine strike.
            self.invalid += 1
            w.messages.append({"role": "assistant", "content": [text_block(response.text or "{}")]})
            w.messages.append({"role": "user", "content": [text_block(
                f"That answer is not valid: {problem}. Reply again with only the corrected JSON object.")]})
        if problem is not None:
            self.invalid += 1
        reply = canonical(answer) if isinstance(answer, dict) else (response.text or "{}")
        w.messages.append({"role": "assistant", "content": [text_block(reply)]})
        w.chars += len(reply)
        w.packets += 1
        w.units |= self.wf.units_of(packet)
        self.packets += 1
        self._emit("answered", packet_id=packet.get("packet_id"), kind=packet.get("kind"),
                   markers=sorted(self.wf.units_of(packet)), valid=problem is None, worker=w.index,
                   charged_micro=response.charged_micro if response else 0, usage=self.usage())

        submitted = self._invoke(self.wf.ANSWER, {"session_id": self.session_id,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": answer if isinstance(answer, dict) else {},
                                                  "include_next": True})
        if submitted.get("ok"):
            body = submitted["result"]
            following = body.get("next")
            if isinstance(following, dict) and following.get("state"):
                if "_images" in body and "_images" not in following:
                    following = {**following, "_images": body["_images"]}
                return following
        # An invalid answer (an engine strike), a conflict or an already-applied
        # packet: ask the engine what is next rather than guessing.
        return self._next()

    # -- ending --------------------------------------------------------------------

    def _pause(self, reason: str | None, error: GatewayError | None = None) -> None:
        if not self.session_id:
            return
        try:
            self._invoke(self.wf.STATUS, {"session_id": self.session_id, "pause": True})
        except Exception:                 # noqa: BLE001 -- pausing is best effort
            log.warning("could not pause session %s", self.session_id)
        detail = error.detail if error is not None and isinstance(error.detail, dict) else {}
        self._emit("paused", reason=reason, message=str(error) if error is not None else None,
                   top_up_url=detail.get("top_up_url"), usage=self.usage())

    def _finish(self, status: str, reason: str | None) -> dict:
        finished = None
        if self.session_id and status == "done":
            try:
                finished = _ok(self._invoke(self.wf.FINISH, {"session_id": self.session_id, "action": "close"}))
            except Exception as exc:      # noqa: BLE001
                reason = f"finish failed: {exc}"
        run = None
        if self.gateway_run and status not in KEPT_OPEN:
            try:
                run = self.gateway.finish_run(self.gateway_run["run_id"])
            except GatewayError as exc:
                log.warning("could not close gateway run %s: %s", self.gateway_run.get("run_id"), exc)
        summary = {
            "run_id": self.run_id, "workflow": self.wf.name, "status": status, "reason": reason,
            "project": self.o.project, "session_id": self.session_id, "packets": self.packets,
            "model_calls": self.seq, "workers": self.workers, "invalid_answers": self.invalid,
            "charged_micro": (run or {}).get("charged_micro", self.charged),
            "charged_credits": round(((run or {}).get("charged_micro", self.charged)) / 10_000, 2),
            "gateway_run": (run or self.gateway_run or {}).get("run_id"),
            "cache": {"verdicts": dict(self.monitor.counts), "read_share": round(self.monitor.read_share(), 4),
                      "prefix": self.prefix_fp, "bte": round(self.monitor.usage.bte())},
            "tokens": {"input": self.monitor.usage.input_total, "cache_read": self.monitor.usage.cache_read,
                       "cache_write": self.monitor.usage.cache_write,
                       "output": self.monitor.usage.output_tokens},
            "finish": {k: (finished or {}).get(k) for k in ("state", "progress") if (finished or {}).get(k)},
            "seconds": None,
        }
        if (finished or {}).get("result") is not None:
            summary["result"] = finished["result"]
        row = self.trace.run(self.run_id) or {}
        if row.get("started_at"):
            summary["seconds"] = round(time.time() - row["started_at"], 1)
        self.trace.finish_run(self.run_id, status, summary)
        self._emit("finished", status=status, reason=reason, usage=self.usage())
        return summary


class GatingRun(DecisionRun):
    WORKFLOW = GATING


class QCRun(DecisionRun):
    WORKFLOW = QC


RUNS = {"gating": GatingRun, "qc": QCRun}


def run_many(projects: list, options: DecisionOptions, *, gateway: GatewayClient, parallel: int = 4,
             trace: TraceStore | None = None, on_event=None, workflow: str | None = None) -> dict:
    """Run one workflow on several projects at once, one session (and one
    rolling-worker loop) each, `parallel` at a time. The first session's first
    answer warms the shared prefix in the provider cache before the others
    start, so N sessions write the prefix once, not N times."""
    from dataclasses import replace

    from plexora.ai.harness.orchestrator import Scheduler, TaskGraph

    name = workflow or ("qc" if isinstance(options, QCOptions) else "gating")
    runner = RUNS[name]
    trace = trace or TraceStore()
    graph = TaskGraph()
    parent = f"air_{uuid.uuid4().hex[:12]}"
    trace.start_run(parent, f"{name}_many", capability=options.capability,
                    billing="dev" if gateway.dev else "credits")
    emit = on_event or (lambda event: None)

    def body(project):
        def task(ctx):
            def relay(event):
                if event.get("event") == "answered":
                    ctx.warm()
                emit({**event, "task": ctx.task.id})
            trace.task(parent, ctx.task.id, "running", label=project)
            summary = runner(replace(options, project=project), gateway=gateway, trace=trace,
                             on_event=relay).run()
            trace.task(parent, ctx.task.id, "done" if summary["status"] == "done" else "failed",
                       detail=summary)
            if summary["status"] not in ("done", "waiting_for_user", "paused"):
                raise RuntimeError(summary.get("reason") or summary["status"])
            return summary
        return task

    for project in projects:
        graph.add(f"{name}:{project}", body(project), label=project)
    result = Scheduler(graph, max_parallel=parallel, stagger_first=True, on_event=emit).run()
    summaries = {tid: graph.tasks[tid].result for tid in graph.tasks}
    report = {"run_id": parent, **{k: result[k] for k in ("done", "failed", "cancelled", "peak_parallel")},
              "tasks": result["tasks"], "sessions": summaries,
              "charged_micro": sum((s or {}).get("charged_micro", 0) for s in summaries.values())}
    trace.finish_run(parent, "done" if not result["failed"] else "failed", report)
    return report
