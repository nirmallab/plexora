"""Conversations on disk, and the service that runs their turns in the background.

    <data_root>/.agent/ai/conversations/<conversation_id>/
        session.json      the record: frozen catalog and prefix, loaded tools,
                          totals, board (SessionStore's atomic writes)
        messages.json     the model-facing history (what the next call sends)
        decisions.jsonl   the transcript: every event, numbered (`seq`)
        control.json      pause / stop / approvals, written by the routes
        .lock             the process driving it

`ConversationStore` is a `SessionStore` of kind `chat` rooted there, plus the
messages and the numbered transcript. `ChatService` is the process's one
driver: `send` starts a turn on a thread and returns at once; `events(after,
wait_s)` is a held poll over the transcript (what the SSE route and the poll
fallback both read); `control` and `approve` write `control.json`, which the
running turn reads before its next call.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from plexora.agent.errors import AgentError
from plexora.agent.sessions.store import SessionStore, _atomic

#: Events of a turn that are not kept in the transcript once the turn is over:
#: the deltas are replaced by the turn's `text` events.
TRANSIENT = ("text_delta",)
MAX_WAIT_S = 25.0


def default_root() -> Path:
    from plexora import paths

    return paths.agent_root() / "ai" / "conversations"


class ConversationStore(SessionStore):
    def __init__(self, root: Path | str | None = None):
        super().__init__("chat", Path(root) if root else default_root())
        self._seq: dict = {}
        self._live: dict = {}
        self._cond = threading.Condition()

    # -- messages ---------------------------------------------------------------

    def save_messages(self, conversation_id: str, messages: list) -> None:
        _atomic(self.folder(conversation_id) / "messages.json",
                json.dumps(messages, ensure_ascii=False, separators=(",", ":")))

    def load_messages(self, conversation_id: str) -> list:
        path = self.folder(conversation_id) / "messages.json"
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    # -- the transcript -----------------------------------------------------------

    def _next_seq(self, conversation_id: str) -> int:
        if conversation_id not in self._seq:
            events = self.decisions(conversation_id)
            self._seq[conversation_id] = max((int(e.get("seq") or 0) for e in events), default=0)
        self._seq[conversation_id] += 1
        return self._seq[conversation_id]

    def append_event(self, conversation_id: str, event: dict) -> dict:
        with self._cond:
            numbered = {"seq": self._next_seq(conversation_id), "at": time.time(), **event}
            if event.get("event") not in TRANSIENT:
                self.log(conversation_id, numbered)
                if event.get("event") == "turn_finished":
                    # The turn's `text` events carry what its deltas spelled.
                    self._live.pop(conversation_id, None)
            else:
                self._live.setdefault(conversation_id, []).append(numbered)
                del self._live[conversation_id][:-2000]
            self._cond.notify_all()
        return numbered

    def events(self, conversation_id: str, after: int = 0, *, wait_s: float = 0.0, still=None) -> list:
        """Events with `seq > after`, oldest first; waits up to `wait_s` for one
        while `still()` (default: always) says it is worth waiting."""
        deadline = time.monotonic() + max(0.0, min(float(wait_s or 0), MAX_WAIT_S))
        with self._cond:
            while True:
                kept = [e for e in self.decisions(conversation_id) if int(e.get("seq") or 0) > after]
                live = [e for e in self._live.get(conversation_id, []) if int(e["seq"]) > after]
                found = sorted(kept + live, key=lambda e: e["seq"])
                remaining = deadline - time.monotonic()
                if found or remaining <= 0 or (still is not None and not still()):
                    return found
                self._cond.wait(min(remaining, 1.0))

    def last_seq(self, conversation_id: str) -> int:
        with self._cond:
            if conversation_id not in self._seq:
                self._next_seq(conversation_id)
                self._seq[conversation_id] -= 1
            return self._seq[conversation_id]


class ChatService:
    """Every conversation this process drives."""

    def __init__(self, store: ConversationStore | None = None, *, gateway_factory=None, session=None,
                 cache=None, trace=None, link=None, notify=None, runner_options: dict | None = None):
        self.store = store or ConversationStore()
        self.gateway_factory = gateway_factory or _default_gateway
        self._session = session
        self.cache = cache
        self.trace = trace
        self.link = link
        self.notify = notify
        self.runner_options = dict(runner_options or {})
        self._runners: dict = {}
        self._threads: dict = {}
        self._lock = threading.Lock()

    @property
    def session(self):
        if self._session is None:
            from plexora.agent.session import AgentSession

            self._session = AgentSession()
        return self._session

    def _cache(self):
        if self.cache is None:
            from plexora.ai.harness.toolcache import ToolResultCache

            self.cache = ToolResultCache()
        return self.cache

    # -- conversations ---------------------------------------------------------------

    def start(self, policy, *, title: str | None = None, viewer: bool = False) -> dict:
        from plexora.ai.harness.runner import AgentRunner

        runner = AgentRunner.create(self.store, gateway=self.gateway_factory(), policy=policy,
                                    session=self.session, title=title, viewer=viewer, cache=self._cache(),
                                    link=self.link, notify=self.notify, trace=self.trace, **self.runner_options)
        with self._lock:
            self._runners[runner.conversation_id] = runner
        record = runner.record
        self.store.append_event(runner.conversation_id, {"event": "disclosure", "text": record["disclosure"]})
        return self.describe(runner.conversation_id)

    def runner(self, conversation_id: str, policy=None):
        from plexora.ai.harness.runner import AgentRunner

        with self._lock:
            runner = self._runners.get(conversation_id)
        if runner is None:
            if not self.store.exists(conversation_id):
                raise AgentError("invalid_input", f"no conversation {conversation_id!r}",
                                 detail={"hint": "ai_chat_history lists the conversations"})
            self.store.claim(conversation_id)
            runner = AgentRunner.open(self.store, conversation_id, gateway=self.gateway_factory(),
                                      session=self.session, cache=self._cache(), link=self.link,
                                      notify=self.notify, trace=self.trace, **self.runner_options)
            with self._lock:
                runner = self._runners.setdefault(conversation_id, runner)
        if policy is not None:
            runner.policy = policy
        return runner

    def running(self, conversation_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(conversation_id)
        return bool(thread and thread.is_alive())

    def send(self, conversation_id: str, text: str, images=(), *, policy=None) -> dict:
        runner = self.runner(conversation_id, policy)
        with self._lock:
            thread = self._threads.get(conversation_id)
            if thread and thread.is_alive():
                raise AgentError("conflict", "this conversation is still answering the last message",
                                 detail={"hint": "wait for its done event, or stop it"}, retryable=True)
            after = self.store.last_seq(conversation_id)
            self.store.append_event(conversation_id, {"event": "user_message", "text": text,
                                                      "images": len(images or ())})

            def work():
                try:
                    runner.run_turn(text, images, lambda e: self.store.append_event(conversation_id, e))
                except Exception as exc:        # noqa: BLE001 -- the transcript says why
                    self.store.append_event(conversation_id, {"event": "error", "code": "internal_error",
                                                              "message": f"{type(exc).__name__}: {exc}"})
                finally:
                    self.store.append_event(conversation_id, {"event": "turn_finished",
                                                              "state": (runner.record or {}).get("state")})

            thread = threading.Thread(target=work, name=f"plexora-chat-{conversation_id}", daemon=True)
            self._threads[conversation_id] = thread
            thread.start()
        return {"conversation_id": conversation_id, "after": after, "running": True}

    def note(self, conversation_id: str, text: str) -> None:
        """Something the user did outside the conversation (an Undo chip) that the
        model reads at the start of its next turn."""
        with self.store.lock(conversation_id):
            record = self.store.load(conversation_id)
            record.setdefault("notes", []).append(text)
            self.store.save(record)
        with self._lock:
            runner = self._runners.get(conversation_id)
        if runner is not None and runner.record is not None:
            runner.record.setdefault("notes", [])
            if text not in runner.record["notes"]:
                runner.record["notes"].append(text)

    def wait(self, conversation_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._threads.get(conversation_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def events(self, conversation_id: str, after: int = 0, wait_s: float = 0.0) -> dict:
        if not self.store.exists(conversation_id):
            raise AgentError("invalid_input", f"no conversation {conversation_id!r}")
        found = self.store.events(conversation_id, after, wait_s=wait_s,
                                  still=lambda: self.running(conversation_id) or wait_s > 0)
        return {"conversation_id": conversation_id, "events": found,
                "after": found[-1]["seq"] if found else after, "running": self.running(conversation_id),
                "control": _public_control(self.store.control(conversation_id))}

    def control(self, conversation_id: str, action: str) -> dict:
        if not self.store.exists(conversation_id):
            raise AgentError("invalid_input", f"no conversation {conversation_id!r}")
        if action == "pause":
            control = self.store.set_control(conversation_id, paused=True, paused_by="viewer")
        elif action == "resume":
            control = self.store.set_control(conversation_id, paused=False, paused_by=None)
        elif action == "stop":
            from plexora.agent.audit import now_iso

            control = self.store.set_control(conversation_id, stopped=True, stopped_by="viewer",
                                             stopped_at=now_iso(), paused=False)
        else:
            raise AgentError("invalid_input", f"unknown action {action!r}; pause, resume or stop")
        self.store.append_event(conversation_id, {"event": "control", "action": action})
        return _public_control(control)

    def approve(self, conversation_id: str, approval_id: str, approve: bool, *, by: str = "viewer") -> dict:
        from plexora.ai.harness.approvals import decide

        if not self.store.exists(conversation_id):
            raise AgentError("invalid_input", f"no conversation {conversation_id!r}")
        return decide(self.store, conversation_id, approval_id, approve, by=by)

    def describe(self, conversation_id: str) -> dict:
        record = self.store.load(conversation_id)
        return {"conversation_id": conversation_id, "title": record.get("title") or "",
                "state": record.get("state"), "created_at": record.get("created_at"),
                "turns": record.get("turns", 0), "tools": len(record.get("catalog") or []),
                "loaded_tools": record.get("loaded") or [], "prefix_fingerprint": record.get("prefix_fp"),
                "charged_micro": record.get("charged_micro", 0),
                "charged_credits": round(int(record.get("charged_micro") or 0) / 10_000, 2),
                "usage": record.get("usage") or {}, "disclosure": record.get("disclosure"),
                "running": self.running(conversation_id),
                "control": _public_control(self.store.control(conversation_id))}

    def history(self, conversation_id: str | None = None, *, after: int = 0, limit: int = 500,
                wait_s: float = 0.0) -> dict:
        if conversation_id is None:
            return {"conversations": [
                {k: r.get(k) for k in ("session_id", "title", "state", "created_at", "turns", "charged_micro")}
                for r in self.store.list(limit=limit)]}
        out = self.describe(conversation_id)
        polled = self.events(conversation_id, after, wait_s)
        out.update(events=polled["events"][-limit:], after=polled["after"])
        return out


def _public_control(control: dict) -> dict:
    approvals = control.get("approvals") or {}
    return {"paused": bool(control.get("paused")), "paused_by": control.get("paused_by"),
            "stopped": bool(control.get("stopped")), "reason": control.get("reason"),
            "pending_approvals": [a for a in approvals.values() if a.get("status") == "pending"]}


def _default_gateway():
    from plexora.ai.harness.gateway import GatewayClient

    return GatewayClient()


_SERVICE: ChatService | None = None
_SERVICE_LOCK = threading.Lock()


def service() -> ChatService:
    """This process's chat service (the server's, or a CLI's)."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ChatService()
        return _SERVICE


def set_service(value: ChatService | None) -> None:
    """Replace the process's service (tests; a notebook with its own gateway)."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = value
