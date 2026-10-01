"""The gating decision loop: an Auto Gating session answered by the harness.

Deterministic Python is the coordinator. The model never sees a tool list:
each packet the session engine serves becomes ONE structured-output call
whose schema is that packet kind's answer model, and the answer is validated
locally before `gating_answer` gets it.

Rolling workers keep the cost linear. A worker is a fresh message list that
starts from the cached prefix (identity + reading guide, `prefix.py`) and
carries only its own packets; it is retired after `units_per_worker` markers,
`max_packets_per_worker` packets or `context_tokens_per_worker` tokens of
context, whichever comes first. One conversation answering n packets re-reads
~n^2 tokens (the 2026-10-01 live run: 480k tokens of context by the end);
a worker per marker keeps every call under ~25k.

Money: the session is declared to the gateway as a run (feature `gating`,
one unit per marker), so the user is quoted and capped before anything is
spent. When the gateway refuses for credit, the session is PAUSED, not
abandoned: `resume_session=` picks it up where it stopped.
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
#: `output_schema`; the budget and narration are for the people watching.
HIDDEN = ("answer_schema", "budget", "narration")
#: Refusals that pause the session for the user instead of failing it.
PAUSE_CODES = ("insufficient_credits", "run_envelope_exceeded", "run_closed", "spend_cap_reached",
               "ai_disabled", "ai_not_entitled", "dev_not_allowed", "capability_not_allowed", "no_license")
FINISHED = ("done", "cancelled", "rolled_back", "failed")
TOKENS_PER_IMAGE = 1600


@dataclass
class GatingOptions:
    project: str
    markers: list | None = None
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


class _Worker:
    def __init__(self, index: int):
        self.index = index
        self.messages: list = []
        self.markers: set = set()
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


def _markers(packet: dict) -> set:
    return {str(u.get("marker")) for u in packet.get("units") or () if isinstance(u, dict)}


class GatingRun:
    def __init__(self, options: GatingOptions, *, gateway: GatewayClient, trace: TraceStore | None = None,
                 session=None, on_event=None, run_id: str | None = None):
        self.o = options
        self.gateway = gateway
        self.trace = trace or TraceStore()
        self.run_id = run_id or f"air_{uuid.uuid4().hex[:12]}"
        self.on_event = on_event or (lambda event: None)
        self.monitor = cache_plan.CacheMonitor()
        self.system = prefix.gating_prefix()
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
        if session is None:
            from plexora.agent.session import AgentSession

            session = AgentSession()
        self.session = session

    # -- registry --------------------------------------------------------------

    def _invoke(self, name: str, arguments: dict) -> dict:
        from plexora.agent.registry import invoke

        return invoke(self.session, name, arguments)

    def _emit(self, event: str, **fields) -> None:
        self.on_event({"event": event, "run_id": self.run_id, "session_id": self.session_id, **fields})

    # -- the loop ----------------------------------------------------------------

    def run(self) -> dict:
        from plexora.agent import registry

        registry.discover(["gating"])
        self.trace.start_run(self.run_id, "gating", project=self.o.project, capability=self.o.capability,
                             billing="dev" if self.gateway.dev else "credits")
        status, reason = "failed", None
        try:
            result = self._start()
            status, reason = self._loop(result)
        except GatewayError as exc:
            status, reason = ("paused", exc.code) if exc.code in PAUSE_CODES else ("failed", exc.code)
            self._pause(reason)
        except Exception as exc:          # noqa: BLE001 -- the run record must say why
            log.exception("gating run %s failed", self.run_id)
            reason = f"{type(exc).__name__}: {exc}"
        finally:
            summary = self._finish(status, reason)
        return summary

    def _start(self) -> dict:
        if self.session_id:
            self._emit("resumed")
            return _ok(self._invoke("gating_next", {"session_id": self.session_id, "wait_s": self.o.wait_s}))
        arguments = {"scope": "project", "project": self.o.project, "mode": self.o.mode, "reading": "once",
                     # The memo keeps answers per agent: a dev run naming a model must not replay another's.
                     "agent": f"plexora-harness:{self.o.capability}" + (f":{self.o.model}" if self.o.model else ""),
                     "known_guide": prefix.guide_version(),
                     **self.o.start_options}
        if self.o.markers:
            arguments["markers"] = list(self.o.markers)
        started = _ok(self._invoke("gating_session_start", arguments))
        self.session_id = started["session_id"]
        self.trace.update_run(self.run_id, session_id=self.session_id)
        units = int(started.get("n_units") or len(self.o.markers or ()) or 1)
        self._emit("started", units=units)
        if self.o.declare_run:
            self.gateway_run = self.gateway.start_run("gating", units, self.session_id)
            self.trace.update_run(self.run_id, gateway_run_id=self.gateway_run.get("run_id"))
            self._emit("quoted", quote_credits=self.gateway_run.get("quote_credits"),
                       run=self.gateway_run.get("run_id"))
        if started.get("packet") is not None:
            return {"state": started.get("state", "decision"), "packet": started["packet"],
                    "_images": started.get("_images") or []}
        return _ok(self._invoke("gating_next", {"session_id": self.session_id, "wait_s": self.o.wait_s}))

    def _loop(self, result: dict) -> tuple[str, str | None]:
        while True:
            state = result.get("state")
            if state in ("decision", "needs_setup"):
                if self.packets >= self.o.max_packets:
                    return "failed", "max_packets"
                result = self._decide(result)
                continue
            if state == "bulk_running":
                result = _ok(self._invoke("gating_next", {"session_id": self.session_id, "wait_s": self.o.wait_s}))
                continue
            if state == "decided":
                return "done", None
            if state in FINISHED:
                return ("done" if state == "done" else state), None
            if state == "waiting_for_user":
                self._emit("waiting_for_user", requests=result.get("requests"))
                return "waiting_for_user", "the session needs a person's answer"
            if state in ("paused", "stopped"):
                return state, result.get("reason") or state
            return "failed", f"unexpected state {state!r}"

    # -- one packet ----------------------------------------------------------------

    def _rotate_if_due(self, packet: dict) -> None:
        w = self.worker
        markers = _markers(packet)
        due = w.packets >= self.o.max_packets_per_worker or w.tokens() >= self.o.context_tokens_per_worker or (
            w.markers and not markers <= w.markers and len(w.markers) >= self.o.units_per_worker)
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
        if schema.for_kind(packet.get("kind", "")) is None and packet.get("answer_schema"):
            text += "\nANSWER SCHEMA: " + canonical(packet["answer_schema"])
        blocks.append(text_block(text))
        return blocks

    def _call(self, packet: dict, messages: list):
        kind = packet.get("kind", "")
        pid = packet.get("packet_id", "pk")
        n = self.attempts[pid] = self.attempts.get(pid, 0) + 1
        context = {"feature": "gating", "agent": "gating_worker", "workflow": "auto_gating",
                   "session_id": self.session_id, "attempt": min(n, 99)}
        if self.gateway_run:
            context["run_id"] = self.gateway_run["run_id"]
        request = ModelRequest(capability=self.o.capability, system=self.system, messages=messages,
                               max_tokens=self.o.max_tokens, output_schema=schema.for_kind(kind),
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

        from plexora.plugins.gating.server.autogate import answers

        try:
            answer = response.json()
        except ValueError:
            return None, "the reply was not JSON"
        if not isinstance(answer, dict):
            return None, "the reply was not a JSON object"
        answer.setdefault("kind", packet.get("kind"))
        if answer.get("kind") != packet.get("kind"):
            return answer, f"kind must be {packet.get('kind')!r}"
        try:
            TypeAdapter(answers.Answer).validate_python(answer)
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
        w.markers |= _markers(packet)
        self.packets += 1
        self._emit("answered", packet_id=packet.get("packet_id"), kind=packet.get("kind"),
                   markers=sorted(_markers(packet)), valid=problem is None, worker=w.index,
                   charged_micro=response.charged_micro if response else 0)

        submitted = self._invoke("gating_answer", {"session_id": self.session_id,
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
        return _ok(self._invoke("gating_next", {"session_id": self.session_id, "wait_s": self.o.wait_s}))

    # -- ending --------------------------------------------------------------------

    def _pause(self, reason: str | None) -> None:
        if not self.session_id:
            return
        try:
            self._invoke("gating_session_status", {"session_id": self.session_id, "pause": True})
        except Exception:                 # noqa: BLE001 -- pausing is best effort
            log.warning("could not pause session %s", self.session_id)
        self._emit("paused", reason=reason)

    def _finish(self, status: str, reason: str | None) -> dict:
        finished = None
        if self.session_id and status == "done":
            try:
                finished = _ok(self._invoke("gating_session_finish", {"session_id": self.session_id,
                                                                      "action": "close"}))
            except Exception as exc:      # noqa: BLE001
                reason = f"finish failed: {exc}"
        run = None
        if self.gateway_run and status in ("done", "failed"):
            try:
                run = self.gateway.finish_run(self.gateway_run["run_id"])
            except GatewayError as exc:
                log.warning("could not close gateway run %s: %s", self.gateway_run.get("run_id"), exc)
        summary = {
            "run_id": self.run_id, "status": status, "reason": reason, "project": self.o.project,
            "session_id": self.session_id, "packets": self.packets, "model_calls": self.seq,
            "workers": self.workers, "invalid_answers": self.invalid,
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
        row = self.trace.run(self.run_id) or {}
        if row.get("started_at"):
            summary["seconds"] = round(time.time() - row["started_at"], 1)
        self.trace.finish_run(self.run_id, status, summary)
        self._emit("finished", status=status, reason=reason)
        return summary


def run_many(projects: list, options: GatingOptions, *, gateway: GatewayClient, parallel: int = 4,
             trace: TraceStore | None = None, on_event=None) -> dict:
    """Gate several projects at once, one session (and one rolling-worker loop)
    each, `parallel` at a time. The first session's first answer warms the
    shared prefix in the provider cache before the others start, so N sessions
    write the prefix once, not N times."""
    from dataclasses import replace

    from plexora.ai.harness.orchestrator import Scheduler, TaskGraph

    trace = trace or TraceStore()
    graph = TaskGraph()
    parent = f"air_{uuid.uuid4().hex[:12]}"
    trace.start_run(parent, "gating_many", capability=options.capability,
                    billing="dev" if gateway.dev else "credits")
    emit = on_event or (lambda event: None)

    def body(project):
        def task(ctx):
            def relay(event):
                if event.get("event") == "answered":
                    ctx.warm()
                emit({**event, "task": ctx.task.id})
            trace.task(parent, ctx.task.id, "running", label=project)
            summary = GatingRun(replace(options, project=project), gateway=gateway, trace=trace,
                                on_event=relay).run()
            trace.task(parent, ctx.task.id, "done" if summary["status"] == "done" else "failed",
                       detail=summary)
            if summary["status"] not in ("done", "waiting_for_user", "paused"):
                raise RuntimeError(summary.get("reason") or summary["status"])
            return summary
        return task

    for project in projects:
        graph.add(f"gating:{project}", body(project), label=project)
    result = Scheduler(graph, max_parallel=parallel, stagger_first=True, on_event=emit).run()
    summaries = {tid: graph.tasks[tid].result for tid in graph.tasks}
    report = {"run_id": parent, **{k: result[k] for k in ("done", "failed", "cancelled", "peak_parallel")},
              "tasks": result["tasks"], "sessions": summaries,
              "charged_micro": sum((s or {}).get("charged_micro", 0) for s in summaries.values())}
    trace.finish_run(parent, "done" if not result["failed"] else "failed", report)
    return report
