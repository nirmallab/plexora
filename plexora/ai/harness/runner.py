"""The conversational agent (run mode b): a tool loop through the gateway.

    runner = AgentRunner.create(store, gateway=GatewayClient(), policy=Policy())
    for event in runner.turn("Which markers in melanoma_01 look unreliable?"):
        ...   # text_delta, tool_call, tool_result, approval_requested, usage, done, ...

One turn is: the user's message, then model call -> tool calls -> model call
... until the model answers without calling a tool. Every model call goes to
the gateway with capability `text_reasoning` (`vision_routine` once the
conversation holds an image) and feature `chat`.

**The prefix** is byte-stable and frozen at conversation start: tools (the
local tools only; catalog tools are loaded into the history by `load_tool`
and run through `call_tool`, so the array never changes) -> system [identity
and house rules, the `dataset-triage` skill, the tool catalog] with ONE cache
breakpoint on the catalog. A second breakpoint rides on the newest message
(`wire.tail_marked`), so each call reads the conversation so far from cache. The conversation record keeps it, so a resumed
conversation sends the very same bytes. Nothing per-conversation is in it.

**Compaction.** Past `compact_at_tokens` (50k) of history the older turns are
replaced, locally and deterministically, by a summary placed at the start of
the kept window: the prefix is never edited, so it still reads from cache.

**Control.** Before every model call and every tool call the runner reads the
conversation's `control.json`: `stopped` ends the turn; `paused` waits (for the
user's Resume, or for an approval). A credit refusal from the gateway PAUSES
the conversation (`paused_by: "credits"`) and ends the turn with a `paused`
event; the next message continues it.

**Sub-agents** (`spawn_agents`): each forks the parent's exact prefix (same
tools array, same system: the fork rule, so it reads the parent's cache) and
starts with its brief after the breakpoint. It may call only its tool subset,
shares the parent's approvals, and returns a typed summary. They run on the
orchestrator's `Scheduler` (`orchestrator.py`), staggered so the first warms
the cache, to `max_depth` levels; `read_board`/`post_board` share facts.

Events are plain dicts with an `event` key; a sub-agent's carry `agent`.
`turn()` runs the work on a thread and yields events as they happen, so a
consumer sees `approval_requested` while the call waits for the answer.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid

from plexora.ai.harness import cache_plan, prefix
from plexora.ai.harness.approvals import DECLINED, ApprovalGate, confirmed, elevated
from plexora.ai.harness.decision import PAUSE_CODES
from plexora.ai.harness.gateway import GatewayError
from plexora.ai.harness.orchestrator import Blackboard, Scheduler, TaskGraph
from plexora.ai.harness.tools import (AWAIT_AGENTS, CALL_TOOL, POST_BOARD, READ_BOARD, SPAWN_AGENTS, SUBAGENT_LOCAL,
                                      ToolAdapter, error_outcome)
from plexora.ai.harness.wire import ModelRequest, canonical, image_block, text_block

log = logging.getLogger("plexora.ai.harness")

TEXT_CAPABILITY = "text_reasoning"
VISION_CAPABILITY = "vision_routine"
DISCLOSURE = "AI-generated; verify before relying on it."
COMPACT_AT_TOKENS = 50_000
#: The gateway's per-request image limit is 24; older images are dropped past this.
KEEP_IMAGES = 20
TOKENS_PER_IMAGE = 1600
MAX_SUBAGENTS = 8
SUMMARY_CHARS = 2000


def _tokens(messages: list) -> int:
    chars, images = 0, 0

    def walk(content):
        nonlocal chars, images
        if isinstance(content, str):
            chars += len(content)
            return
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                chars += len(block.get("text", ""))
            elif kind == "image":
                images += 1
            elif kind == "tool_use":
                chars += len(canonical(block.get("input") or {})) + 40
            elif kind == "tool_result":
                walk(block.get("content"))
    for message in messages:
        walk(message.get("content"))
    return int(chars / cache_plan.CHARS_PER_TOKEN) + images * TOKENS_PER_IMAGE


def _has_images(messages: list) -> bool:
    def walk(content):
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "image":
                return True
            if block.get("type") == "tool_result" and walk(block.get("content")):
                return True
        return False
    return any(walk(m.get("content")) for m in messages)


def _is_turn_start(message: dict) -> bool:
    content = message.get("content")
    return message.get("role") == "user" and not any(
        b.get("type") == "tool_result" for b in (content if isinstance(content, list) else []))


def summarize_turns(messages: list) -> str:
    """A deterministic summary of `messages` for compaction: what the user
    asked, what was called and what came back, what was answered."""
    lines = []
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for block in blocks:
            kind = block.get("type")
            if kind == "text" and block.get("text", "").strip():
                who = "User" if message.get("role") == "user" else "Assistant"
                text = " ".join(block["text"].split())
                lines.append(f"{who}: {text[:300]}{'...' if len(text) > 300 else ''}")
            elif kind == "tool_use":
                args = canonical(block.get("input") or {})
                lines.append(f"Called {block.get('name')}({args[:160]}{'...' if len(args) > 160 else ''})")
            elif kind == "tool_result":
                inner = block.get("content")
                text = inner if isinstance(inner, str) else " ".join(
                    b.get("text", "") for b in inner or [] if b.get("type") == "text")
                mark = "error" if block.get("is_error") else "result"
                lines.append(f"  -> {mark}: {' '.join(text.split())[:200]}")
    return "\n".join(lines)


def _parse_summary(text: str) -> dict:
    text = (text or "").strip()
    try:
        start, end = text.find("{"), text.rfind("}")
        value = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
    except ValueError:
        value = None
    if isinstance(value, dict) and "summary" in value:
        return {"summary": str(value.get("summary"))[:SUMMARY_CHARS],
                "findings": [str(f)[:400] for f in (value.get("findings") or [])][:20],
                "operation_ids": [str(o) for o in (value.get("operation_ids") or [])][:50],
                "artifact_ids": [str(a) for a in (value.get("artifact_ids") or [])][:50]}
    return {"summary": text[:SUMMARY_CHARS], "findings": [], "operation_ids": [], "artifact_ids": []}


def policy_from(described: dict | None):
    from plexora.agent.policy import DEFAULT_EGRESS, Policy

    d = described or {}
    return Policy(allow_source_writes=bool(d.get("allow_source_writes")),
                  allow_destructive=bool(d.get("allow_destructive")),
                  egress=frozenset(d.get("egress") or DEFAULT_EGRESS),
                  allow_writes=d.get("allow_writes", True) is not False, principal=d.get("principal"))


class Halted(Exception):
    """The user stopped the conversation."""


class TurnFailed(Exception):
    """The gateway refused or failed for a reason a pause does not fix; the
    turn ends with its `error` event and the conversation stays usable."""


class AgentRunner:
    """One agent of a conversation: the main one, or a sub-agent (`parent`)."""

    def __init__(self, conversation_id: str, *, store, gateway, adapter: ToolAdapter, system: list,
                 messages: list | None = None, record: dict | None = None, policy=None, trace=None,
                 capability: str = TEXT_CAPABILITY, vision_capability: str = VISION_CAPABILITY,
                 model: str | None = None, max_tokens: int = 8192, compact_at_tokens: int = COMPACT_AT_TOKENS,
                 max_steps: int = 40, depth: int = 0, max_depth: int = 1, max_parallel: int = 4,
                 agent_id: str = "main", role: str = "main", allowed: set | None = None,
                 parent: "AgentRunner | None" = None, board: Blackboard | None = None,
                 approvals: ApprovalGate | None = None, poll_s: float = 0.1):
        from plexora.ai.harness.trace import TraceStore

        self.conversation_id = conversation_id
        self.store = store
        self.gateway = gateway
        self.adapter = adapter
        self.system = system
        self.messages = list(messages or [])
        self.record = record
        self.policy = policy or adapter.policy
        self.trace = trace or (parent.trace if parent else TraceStore())
        self.capability = capability
        self.vision_capability = vision_capability
        self.model = model
        self.max_tokens = max_tokens
        self.compact_at_tokens = compact_at_tokens
        self.max_steps = max_steps
        self.depth = depth
        self.max_depth = max_depth
        self.max_parallel = max_parallel
        self.agent_id = agent_id
        self.role = role
        self.allowed = allowed
        self.parent = parent
        self.root = parent.root if parent else self
        self.board = board or (parent.board if parent else Blackboard())
        self.approvals = approvals or (parent.approvals if parent else ApprovalGate(store, conversation_id,
                                                                                    poll_s=poll_s))
        self.poll_s = poll_s
        self.monitor = parent.monitor if parent else cache_plan.CacheMonitor()
        self._calls = 0
        self._made = 0
        self._ctx = None
        self._tools_seen = None
        self._pending: dict[str, dict] = {}       # wait=false sub-agents: id -> {graph, thread}
        self._lock = threading.Lock()
        self.tool_calls = 0
        self.charged = 0
        if self.record is not None and self.record.get("board"):
            for key, value in self.record["board"].items():
                self.board.post(key, value, by="restored")

    # -- construction ------------------------------------------------------------------

    @classmethod
    def create(cls, store, *, gateway, policy, session=None, title: str | None = None, viewer: bool = False,
               cache=None, link=None, notify=None, trace=None, **options) -> "AgentRunner":
        """Start a conversation: freeze the catalog and the prefix, write the record."""
        from plexora.agent.audit import now_iso
        from plexora.agent.sessions.store import new_session_id

        session = session or _session()
        adapter = ToolAdapter(session, policy=policy, cache=cache, link=link, notify=notify, viewer=viewer)
        system = prefix.chat_prefix(adapter.catalog_text())
        cid = new_session_id("cv")
        tools = adapter.definitions()
        record = {"session_id": cid, "kind": "chat", "created_at": now_iso(), "title": title or "",
                  "state": "idle", "policy": policy.describe(), "viewer": viewer,
                  "catalog": adapter.catalog, "system": system, "tools_initial": len(tools), "loaded": [],
                  "prefix_fp": cache_plan.fingerprint([tools, system]), "calls": 0, "turns": 0,
                  "charged_micro": 0, "usage": {}, "board": {}, "disclosure": DISCLOSURE}
        store.create(record)
        store.save_messages(cid, [])
        runner = cls(cid, store=store, gateway=gateway, adapter=adapter, system=system, record=record,
                     policy=policy, trace=trace, **options)
        runner.trace.start_run(cid, "chat", session_id=cid, capability=runner.capability,
                               billing="dev" if getattr(gateway, "dev", False) else "credits")
        return runner

    @classmethod
    def open(cls, store, conversation_id: str, *, gateway, session=None, policy=None, cache=None, link=None,
             notify=None, trace=None, **options) -> "AgentRunner":
        """Resume a conversation from its record: the same frozen prefix bytes."""
        record = store.load(conversation_id)
        frozen = policy_from(record.get("policy"))
        adapter = ToolAdapter(session or _session(), policy=frozen, catalog=record.get("catalog") or [],
                              loaded=record.get("loaded") or [], cache=cache, link=link, notify=notify)
        return cls(conversation_id, store=store, gateway=gateway, adapter=adapter, system=record["system"],
                   messages=store.load_messages(conversation_id), record=record, policy=policy or frozen,
                   trace=trace, **options)

    # -- the turn ------------------------------------------------------------------------

    def turn(self, user_text: str, images=()):
        """Yield the events of one turn as they happen."""
        events: queue.Queue = queue.Queue()
        done = object()

        def work():
            try:
                self.run_turn(user_text, images, events.put)
            except Exception as exc:          # noqa: BLE001 -- surfaced as an event
                log.exception("chat turn failed in %s", self.conversation_id)
                events.put({"event": "error", "code": "internal_error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(done)

        thread = threading.Thread(target=work, name=f"plexora-chat-{self.conversation_id}", daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is done:
                break
            yield event
        thread.join()

    def run_turn(self, user_text: str, images, emit) -> dict:
        """One turn, synchronously, announcing through `emit(event)`."""
        cid = self.conversation_id
        self.store.set_control(cid, stopped=False, paused=False, paused_by=None, reason=None)
        content = [image_block(data, fmt) for data, fmt in _images(images)]
        notes = (self.record or {}).pop("notes", None) or []
        if user_text:
            content.append(text_block("\n".join(notes + [user_text]) if notes else user_text))
        if not content:
            emit({"event": "error", "code": "invalid_input", "message": "say something"})
            return {"state": "idle"}
        self._append_user(content)
        self._set_state("running", turns=int((self.record or {}).get("turns") or 0) + 1)
        emit({"event": "turn_started", "turn": (self.record or {}).get("turns"), "images": len(content) - bool(user_text)})
        try:
            outcome = self._loop(emit)
        except Halted:
            outcome = {"state": "stopped"}
            emit({"event": "stopped"})
        except TurnFailed:
            outcome = {"state": "idle"}
        self._save()
        self._set_state(outcome["state"] if outcome["state"] != "done" else "idle")
        return outcome

    def _loop(self, emit) -> dict:
        text = ""
        for _ in range(self.max_steps):
            self._halt_check()
            response = self._model_call(emit)
            if response is None:
                return {"state": "paused"}
            text = "".join(b.get("text", "") for b in response.blocks if b["type"] == "text")
            blocks = [b for b in response.blocks if b["type"] in ("text", "tool_use")] or [text_block("(no answer)")]
            self.messages.append({"role": "assistant", "content": blocks})
            if text:
                emit({"event": "text", "text": text, "agent": self._tag()})
            uses = response.tool_uses
            if not uses:
                self._save()
                emit({"event": "done", "text": text, "agent": self._tag(), "stop_reason": response.stop_reason})
                return {"state": "done", "text": text}
            results = []
            halted = False
            for use in uses:
                if halted:
                    results.append(error_outcome(use["id"], use["name"], "stopped by the user").block())
                    continue
                try:
                    self._halt_check()
                    outcome = self._tool(use, emit)
                except Halted:
                    halted = True
                    results.append(error_outcome(use["id"], use["name"], "stopped by the user").block())
                    continue
                results.append(outcome.block())
            self.messages.append({"role": "user", "content": results})
            self._save()
            if halted:
                raise Halted()
        emit({"event": "error", "code": "max_steps", "message": f"stopped after {self.max_steps} model calls "
                                                               "in one turn", "agent": self._tag()})
        return {"state": "done", "text": text}

    # -- control -------------------------------------------------------------------------------

    def _halt_check(self) -> None:
        """Raise `Halted` on stop; wait while paused (by the user or an approval)."""
        while True:
            control = self.store.control(self.conversation_id)
            if control.get("stopped"):
                raise Halted()
            if not control.get("paused") or control.get("paused_by") == "credits":
                return
            time.sleep(self.poll_s)

    def _set_state(self, state: str, **fields) -> None:
        if self.record is None:
            return
        self.record.update(state=state, **fields)
        self._save_record()

    def _save_record(self) -> None:
        if self.record is None:
            return
        with self.store.lock(self.conversation_id):
            self.record["loaded"] = self.adapter.loaded
            self.record["board"] = self.board.facts()
            self.store.save(self.record)

    def _save(self) -> None:
        if self.parent is None and self.record is not None:
            self.store.save_messages(self.conversation_id, self.messages)
            self._save_record()

    def _tag(self):
        return None if self.parent is None else self.agent_id

    def _append_user(self, content: list) -> None:
        # After a pause the history may end on a user turn (tool results the
        # model never saw): the new words join it, so roles still alternate.
        if self.messages and self.messages[-1]["role"] == "user":
            previous = self.messages[-1]["content"]
            previous = previous if isinstance(previous, list) else [text_block(str(previous))]
            self.messages[-1] = {"role": "user", "content": previous + content}
        else:
            self.messages.append({"role": "user", "content": content})

    # -- the model --------------------------------------------------------------------------------

    def _compact(self, emit) -> None:
        if _tokens(self.messages) <= self.compact_at_tokens:
            return
        budget = self.compact_at_tokens // 2
        cut = None
        for index in range(1, len(self.messages)):
            if _is_turn_start(self.messages[index]) and _tokens(self.messages[index:]) <= budget:
                cut = index
                break
        if cut is None:
            return
        dropped, kept = self.messages[:cut], self.messages[cut:]
        summary = text_block("[Summary of the earlier conversation, made by Plexora when it grew long]\n"
                             + summarize_turns(dropped))
        first = kept[0]
        kept[0] = {"role": "user", "content": [summary] + list(first["content"])}
        before = _tokens(self.messages)
        self.messages = kept
        emit({"event": "compacted", "dropped_messages": len(dropped), "tokens_before": before,
              "tokens_after": _tokens(self.messages), "agent": self._tag()})

    def _limit_images(self) -> None:
        seen = 0
        for message in reversed(self.messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for i in range(len(content) - 1, -1, -1):
                block = content[i]
                if block.get("type") == "image":
                    seen += 1
                    if seen > KEEP_IMAGES:
                        content[i] = text_block("[an earlier image, no longer shown]")
                elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    inner = block["content"]
                    for j in range(len(inner) - 1, -1, -1):
                        if inner[j].get("type") == "image":
                            seen += 1
                            if seen > KEEP_IMAGES:
                                inner[j] = text_block("[an earlier image, no longer shown]")

    def _model_call(self, emit):
        self._compact(emit)
        self._limit_images()
        tools = self.adapter.definitions()
        capability = self.vision_capability if _has_images(self.messages) else self.capability
        root = self.root
        with root._lock:
            if root.record is not None:
                root.record["calls"] = int(root.record.get("calls") or 0) + 1
                number = root.record["calls"]
            else:
                root._calls += 1
                number = root._calls
        context = {"feature": "chat", "agent": "chat" if self.parent is None else "chat_subagent",
                   "workflow": "conversation", "session_id": self.conversation_id, "attempt": 1}
        request = ModelRequest(capability=capability, system=self.system, messages=self.messages,
                               tools=tools, max_tokens=self.max_tokens, context=context, model=self.model)
        key = f"{self.conversation_id}.{self.agent_id}.{number}"

        def delta(piece):
            emit({"event": "text_delta", "text": piece, "agent": self._tag()})

        try:
            response = self.gateway.messages(request, idempotency_key=key, on_delta=delta)
        except GatewayError as exc:
            if exc.code in PAUSE_CODES:
                self.store.set_control(self.conversation_id, paused=True, paused_by="credits", reason=exc.code)
                emit({"event": "paused", "reason": exc.code, "message": str(exc), "agent": self._tag(),
                      "resume": "send another message once credit is available"})
                return None
            emit({"event": "error", "code": exc.code, "message": str(exc), "agent": self._tag()})
            raise TurnFailed(exc.code) from None
        prefix_now = cache_plan.fingerprint([tools, self.system])
        changed = self._tools_seen is not None and self._tools_seen != len(tools)
        history = self.parent is None and any(m["role"] == "assistant" for m in self.messages[:-1])
        warm = (self._made > 0 or history) and not changed
        self._tools_seen = len(tools)
        self._made += 1
        verdict = self.monitor.observe(prefix_now, cache_plan.expected_tokens([tools, self.system]),
                                       response.usage, warm=warm)
        self.charged += response.charged_micro
        if self.parent is not None:
            with root._lock:
                root.charged += response.charged_micro
        if root.record is not None:
            with root._lock:
                root.record["charged_micro"] = int(root.record.get("charged_micro") or 0) + response.charged_micro
                usage = root.record.setdefault("usage", {})
                for name in ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h", "output_tokens"):
                    usage[name] = int(usage.get(name) or 0) + getattr(response.usage, name)
        self.trace.call(self.conversation_id, worker=self.depth, seq=number, kind="chat",
                        packet_id=self.agent_id, capability=capability,
                        prefix_fp=(root.record or {}).get("prefix_fp"), verdict=verdict,
                        input_uncached=response.usage.input_uncached, cache_read=response.usage.cache_read,
                        cache_write=response.usage.cache_write, output_tokens=response.usage.output_tokens,
                        price_micro=response.price_micro, charged_micro=response.charged_micro,
                        cost_micro=response.cost_micro, gateway_request_id=response.gateway_request_id,
                        latency_ms=response.latency_ms)
        emit({"event": "usage", "charged_micro": response.charged_micro,
              "total_charged_micro": int((root.record or {}).get("charged_micro") or root.charged),
              "balance_micro": (response.balance or {}).get("available_micro"), "verdict": verdict,
              "capability": capability, "input_tokens": response.usage.input_total,
              "cache_read": response.usage.cache_read, "output_tokens": response.usage.output_tokens,
              "agent": self._tag()})
        if self.parent is not None and self._made == 1 and self._ctx is not None:
            self._ctx.warm()          # the first sub-agent's first call wrote the cache; release the rest
        return response

    # -- tools ------------------------------------------------------------------------------------

    def _tool(self, use: dict, emit):
        tid = use.get("id") or ""
        name, args = self.adapter.resolve(use.get("name") or "", use.get("input") or {})
        if not name:
            return error_outcome(tid, CALL_TOOL, "call_tool needs the name of a catalog tool.", source="local")
        self.tool_calls += 1
        emit({"event": "tool_call", "tool_use_id": tid, "tool": name, "arguments": args, "agent": self._tag()})
        if name == SPAWN_AGENTS:
            outcome = self._spawn(tid, args, emit)
        elif name == AWAIT_AGENTS:
            outcome = self._await(tid, args)
        elif name == READ_BOARD:
            outcome = self.adapter.wrap(tid, name, self._read_board(args.get("keys") or []))
        elif name == POST_BOARD:
            key = str(args.get("key") or "")
            if not key:
                outcome = error_outcome(tid, name, "post_board needs a key", source="local")
            else:
                version = self.board.post(key, args.get("value"), by=self.agent_id)
                outcome = self.adapter.wrap(tid, name, {"key": key, "version": version})
        elif self.adapter.needs_approval(name) and (self.allowed is None or name in self.allowed):
            outcome = self._approved_call(tid, name, args, emit)
        else:
            outcome = self.adapter.execute(tid, name, args, policy=self.policy, allowed=self.allowed)
        self.trace.tool_call(self.conversation_id, agent=self.agent_id, tool=name, capability=outcome.capability,
                             permission=outcome.permission, source=outcome.source, ok=not outcome.is_error,
                             offloaded=bool(outcome.offloaded), operation_id=outcome.operation_id,
                             latency_ms=outcome.latency_ms)
        emit({"event": "tool_result", "tool_use_id": tid, "tool": name, "ok": not outcome.is_error,
              "source": outcome.source, "permission": outcome.permission, "operation_id": outcome.operation_id,
              "undo": outcome.undo, "offloaded": outcome.offloaded, "summary": outcome.summary,
              "images": [{"format": fmt, "data": _b64(data)} for data, fmt in outcome.images[:2]],
              "agent": self._tag()})
        return outcome

    def _approved_call(self, tid: str, name: str, args: dict, emit):
        cap = self.adapter.capability(name)
        status, approval = self.approvals.ask(tool=name, capability=cap, arguments=args, emit=emit,
                                              agent=self._tag())
        if status == "stopped":
            raise Halted()
        if status != "approved":
            return error_outcome(tid, name, DECLINED, source="declined", capability=cap.name,
                                 permission=cap.permission)
        policy = elevated(self.policy, cap.permission)
        outcome = self.adapter.execute(tid, name, confirmed(cap, args), policy=policy, allowed=self.allowed)
        outcome.source = "approved" if outcome.source == "live" else outcome.source
        return outcome

    def _read_board(self, keys) -> dict:
        out = {}
        for key in keys:
            key = str(key)
            if key.endswith("*"):
                out.update(self.board.facts(key[:-1]))
            else:
                out[key] = self.board.read(key)
        return {"facts": out}

    # -- sub-agents ---------------------------------------------------------------------------------

    def _child(self, spec: dict, agent_id: str, emit) -> "AgentRunner":
        reads = {e["tool"] for e in self.adapter.catalog if e["permission"] == "read"}
        named = {str(t) for t in spec.get("tools") or ()}
        allowed = (named & set(self.adapter.by_tool)) if named else reads
        allowed |= set(SUBAGENT_LOCAL)
        if self.depth + 1 < self.max_depth:
            allowed |= {SPAWN_AGENTS, AWAIT_AGENTS}
        return AgentRunner(self.conversation_id, store=self.store, gateway=self.gateway, adapter=self.adapter,
                           system=self.system, policy=self.policy, capability=self.capability,
                           vision_capability=self.vision_capability, model=self.model, max_tokens=self.max_tokens,
                           compact_at_tokens=self.compact_at_tokens, max_steps=self.max_steps,
                           depth=self.depth + 1, max_depth=self.max_depth, max_parallel=self.max_parallel,
                           agent_id=agent_id, role=str(spec.get("role") or "worker")[:40], allowed=allowed,
                           parent=self, poll_s=self.poll_s)

    def _run_child(self, spec: dict, agent_id: str, emit, ctx) -> dict:
        child = self._child(spec, agent_id, emit)
        child._ctx = ctx
        inputs = dict(spec.get("inputs") or {})
        for dep in spec.get("depends_on") or ():
            inputs.setdefault("results_of", {})[dep] = self.board.read(f"result:{dep}")
        brief = prefix.subagent_brief(child.role, str(spec.get("brief") or ""),
                                      sorted(t for t in child.allowed if t not in SUBAGENT_LOCAL), inputs)
        child.messages = [{"role": "user", "content": [text_block(brief)]}]

        def relay(event):
            emit({**event, "agent": event.get("agent") or agent_id})

        relay({"event": "agent_started", "role": child.role, "brief": str(spec.get("brief") or "")[:300]})
        state, text = "done", ""
        try:
            outcome = child._loop(relay)
            state, text = outcome["state"], outcome.get("text", "")
        except Halted:
            state = "stopped"
        except TurnFailed as exc:
            state, text = "failed", f"the sub-agent's model call failed: {exc}"
        summary = {"agent": agent_id, "role": child.role, "state": state, **_parse_summary(text),
                   "model_calls": child._made, "tool_calls": child.tool_calls,
                   "charged_micro": child.charged}
        relay({"event": "agent_finished", "state": state, "summary": summary["summary"][:300]})
        if state == "stopped":
            raise Halted()
        return summary

    def _graph(self, specs: list, emit):
        graph = TaskGraph()
        ids = []
        known = set()
        for n, spec in enumerate(specs):
            agent_id = str(spec.get("id") or f"{spec.get('role') or 'agent'}_{uuid.uuid4().hex[:4]}")
            agent_id = "".join(c for c in agent_id if c.isalnum() or c in "_-")[:40] or f"agent_{n}"
            deps = [str(d) for d in spec.get("depends_on") or () if str(d) in known]
            graph.add(agent_id, (lambda s, a: (lambda ctx: self._run_child(s, a, emit, ctx)))(spec, agent_id),
                      depends_on=deps, label=str(spec.get("role") or agent_id))
            known.add(agent_id)
            ids.append(agent_id)
        return graph, ids

    def _spawn(self, tid: str, args: dict, emit):
        specs = [s for s in (args.get("agents") or []) if isinstance(s, dict) and s.get("brief")]
        if self.depth >= self.max_depth:
            return error_outcome(tid, SPAWN_AGENTS, f"sub-agents may not spawn below depth {self.max_depth}",
                                 source="local")
        if not specs:
            return error_outcome(tid, SPAWN_AGENTS, "spawn_agents needs agents, each with a role and a brief",
                                 source="local")
        if len(specs) > MAX_SUBAGENTS:
            return error_outcome(tid, SPAWN_AGENTS, f"at most {MAX_SUBAGENTS} sub-agents at once", source="local")
        graph, ids = self._graph(specs, emit)

        def tracer(event):
            task = event.get("task")
            if task:
                self.trace.task(self.conversation_id, task, "running" if event["event"] == "task_started"
                                else event.get("state", "done"), parent=self.agent_id, label=event.get("label"),
                                detail={"error": event.get("error")} if event.get("error") else None)

        scheduler = Scheduler(graph, max_parallel=self.max_parallel, board=self.board, stagger_first=True,
                              on_event=tracer)
        if args.get("wait", True) is False:
            thread = threading.Thread(target=scheduler.run, name=f"plexora-agents-{tid}", daemon=True)
            thread.start()
            with self._lock:
                for agent_id in ids:
                    self._pending[agent_id] = {"graph": graph, "thread": thread}
            return self.adapter.wrap(tid, SPAWN_AGENTS, {"started": ids, "hint": "await_agents with these ids"})
        result = scheduler.run()
        if self.store.control(self.conversation_id).get("stopped"):
            raise Halted()
        return self.adapter.wrap(tid, SPAWN_AGENTS, self._collect(graph, ids, result))

    def _collect(self, graph, ids, result=None) -> dict:
        agents = []
        for agent_id in ids:
            task = graph.tasks[agent_id]
            if task.state == "done":
                agents.append(task.result)
            else:
                agents.append({"agent": agent_id, "role": task.label, "state": task.state, "error": task.error})
        out = {"agents": agents}
        if result:
            out["peak_parallel"] = result.get("peak_parallel")
        return out

    def _await(self, tid: str, args: dict):
        ids = [str(i) for i in args.get("ids") or []]
        timeout = max(1.0, min(float(args.get("timeout_s") or 120), 600.0))
        deadline = time.monotonic() + timeout
        with self._lock:
            known = {i: self._pending.get(i) for i in ids}
        unknown = [i for i, v in known.items() if v is None]
        if unknown:
            return error_outcome(tid, AWAIT_AGENTS, f"no sub-agents {unknown} started with wait=false",
                                 source="local")
        for entry in {id(v["thread"]): v for v in known.values()}.values():
            entry["thread"].join(max(0.0, deadline - time.monotonic()))
        agents = []
        for agent_id, entry in known.items():
            task = entry["graph"].tasks[agent_id]
            if task.state == "done":
                agents.append(task.result)
            else:
                agents.append({"agent": agent_id, "state": task.state, "error": task.error})
        return self.adapter.wrap(tid, AWAIT_AGENTS, {"agents": agents})


def _session():
    from plexora.agent.session import AgentSession

    return AgentSession()


def _images(images) -> list:
    import base64

    out = []
    for item in images or ():
        if isinstance(item, (bytes, bytearray)):
            out.append((bytes(item), "png"))
        elif isinstance(item, tuple):
            out.append((bytes(item[0]), str(item[1] or "png")))
        elif isinstance(item, dict) and item.get("data"):
            data = item["data"]
            if isinstance(data, str):
                if data.startswith("data:"):
                    header, _, data = data.partition(",")
                    item = {**item, "format": item.get("format") or header.split("/")[-1].split(";")[0]}
                data = base64.b64decode(data)
            out.append((bytes(data), str(item.get("format") or "png").replace("jpg", "jpeg")))
    return out


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
