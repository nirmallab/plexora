"""Open viewer tabs, and the commands and events waiting for them.

The server has never been able to push anything to a browser: every page polls
(`/health` every five seconds). An agent driving a viewer needs the reverse
direction, so each open viewer registers here and then asks, repeatedly, "is
there anything for me?" -- a long poll. Nothing here is a socket or a thread:
a waiting request holds a `Condition` for at most `wait_s`, and everything
expires lazily when somebody next looks.

Two speeds, because a held request is a Waitress worker thread: a tab nobody
is driving polls briefly every few seconds (it doubles as its heartbeat),
and only while an agent is attached (`attach`) does it hold its poll open so
commands arrive at once. `MAX_HELD_REQUESTS` caps how many can be held across
all tabs; past it a poll returns immediately and the tab retries -- slower,
never starved. An Open OnDemand allocation of two cores has eight worker
threads, and this keeps six of them for tiles.

Commands are sequenced per session, acknowledged by the tab with what
happened, and waited on by whoever sent them. Events ("gating changed on
project X") fan out to every tab showing that project, so a gate an agent set
from another process redraws without a reload.

No Flask here; the routes are in server/routes/agent_routes.py.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

#: A tab not heard from for this long is gone.
SESSION_TTL_S = 90
#: A tab not heard from for this long is reported `stale` (probably a
#: background tab the browser is throttling).
STALE_AFTER_S = 20
#: How long an agent's attachment lasts without being renewed.
ATTACH_TTL_S = 120
#: How long a session that said it was navigating keeps its commands for the
#: page it is navigating to.
NAVIGATING_GRACE_S = 30
MAX_QUEUE = 64
MAX_EVENTS = 256
#: Longest a poll is held, and the default time a sender waits for its ack.
MAX_WAIT_S = 15.0
DEFAULT_SEND_TIMEOUT_S = 20.0


def _max_held() -> int:
    try:
        from plexora._resources import worker_threads

        return max(1, int(worker_threads()) // 4)
    except Exception:  # pragma: no cover
        return 2


MAX_HELD_REQUESTS = _max_held()


@dataclass
class Command:
    command_id: str
    seq: int
    type: str
    arguments: dict
    created: float
    expected_revision: int | None = None
    origin: str = "agent"
    status: str = "pending"          # pending | delivered | done | rejected | unsupported | expired
    result: dict | None = None
    error: str | None = None
    warning: str | None = None
    resulting_revision: int | None = None
    acked: float | None = None

    def describe(self) -> dict:
        return {"command_id": self.command_id, "seq": self.seq, "type": self.type,
                "arguments": self.arguments, "status": self.status,
                "result": self.result, "error": self.error, "warning": self.warning,
                "resulting_revision": self.resulting_revision,
                "expected_revision": self.expected_revision}


@dataclass
class Session:
    session_id: str
    project: str | None
    dataset: str | None = None
    client: str = "browser"
    capabilities: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    page_url: str = ""
    title: str = ""
    revision: int = 0
    visible: bool = True
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    attached_until: float = 0.0
    navigating_to: str | None = None
    navigating_until: float = 0.0
    seq: int = 0
    commands: deque = field(default_factory=lambda: deque(maxlen=MAX_QUEUE))
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    event_seq: int = 0
    state: dict | None = None

    def describe(self, now=None) -> dict:
        now = now or time.time()
        age = now - self.last_seen
        return {
            "view_id": self.session_id, "project": self.project, "dataset": self.dataset,
            "client": self.client, "capabilities": list(self.capabilities),
            "tools": list(self.tools), "title": self.title, "revision": self.revision,
            "visible": self.visible, "attached": self.attached_until > now,
            "status": "navigating" if self.navigating_until > now else
                      ("stale" if age > STALE_AFTER_S else "live"),
            "seconds_since_seen": round(age, 1),
            "pending_commands": sum(1 for c in self.commands if c.status == "pending"),
        }


_LOCK = threading.RLock()
_CHANGED = threading.Condition(_LOCK)
_SESSIONS: dict = {}
_COMMANDS: dict = {}
_HELD = 0
_EVENT_COUNTER = itertools.count(1)


def _now():
    return time.time()


def _expire(now=None):
    now = now or _now()
    for sid in [sid for sid, s in _SESSIONS.items()
                if now - s.last_seen > SESSION_TTL_S and s.navigating_until < now]:
        session = _SESSIONS.pop(sid)
        for command in session.commands:
            if command.status in ("pending", "delivered"):
                command.status = "expired"
                command.error = "the viewer went away before running this command"
    for cid in [cid for cid, c in _COMMANDS.items()
                if c.status not in ("pending", "delivered") and now - (c.acked or c.created) > 600]:
        _COMMANDS.pop(cid, None)


def _reset_for_tests():
    global _HELD
    with _LOCK:
        _SESSIONS.clear()
        _COMMANDS.clear()
        _HELD = 0


# -- the tab's side --------------------------------------------------------


def register(*, session_id=None, project=None, dataset=None, client="browser",
             capabilities=(), tools=(), page_url="", title="", revision=0) -> dict:
    """Register (or re-register) a tab. Idempotent on `session_id`: a tab that
    navigated keeps its id -- and the commands queued for it."""
    with _LOCK:
        _expire()
        sid = session_id if session_id and len(str(session_id)) <= 64 else None
        sid = sid or f"view_{uuid.uuid4().hex[:12]}"
        session = _SESSIONS.get(sid)
        if session is None:
            session = _SESSIONS[sid] = Session(session_id=sid, project=project)
        session.project = project
        session.dataset = dataset
        session.client = client or "browser"
        session.capabilities = list(capabilities or [])
        session.tools = list(tools or [])
        session.page_url = page_url or ""
        session.title = title or ""
        session.revision = int(revision or 0)
        session.last_seen = _now()
        session.navigating_to = None
        session.navigating_until = 0.0
        _CHANGED.notify_all()
        return session.describe()


def heartbeat(session_id, *, revision=None, visible=None) -> Session | None:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        session.last_seen = _now()
        if revision is not None:
            session.revision = int(revision)
        if visible is not None:
            session.visible = bool(visible)
        return session


def leave(session_id, *, navigating_to=None) -> bool:
    """A tab closing, or navigating (then its commands are kept briefly)."""
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            return False
        if navigating_to:
            session.navigating_to = navigating_to
            session.navigating_until = _now() + NAVIGATING_GRACE_S
            session.last_seen = _now()
        else:
            _SESSIONS.pop(session_id, None)
            for command in session.commands:
                if command.status in ("pending", "delivered"):
                    command.status = "expired"
                    command.error = "the viewer was closed"
        _CHANGED.notify_all()
        return True


def _pending_for(session, after_seq, after_event):
    commands = [c for c in session.commands if c.status == "pending" and c.seq > after_seq]
    events = [e for e in session.events if e["event_seq"] > after_event]
    return commands, events


def wait_for_work(session_id, *, after=0, after_event=0, wait_s=0.0, revision=None,
                  visible=None):
    """A tab's poll: `{commands, events, hold, attached}`, or None if unknown.

    Returns at once when there is work, when `wait_s` is 0, or when the held
    budget is spent; otherwise holds up to `wait_s` (capped at MAX_WAIT_S)."""
    global _HELD
    wait_s = max(0.0, min(float(wait_s or 0.0), MAX_WAIT_S))
    with _LOCK:
        _expire()
        session = heartbeat(session_id, revision=revision, visible=visible)
        if session is None:
            return None
        commands, events = _pending_for(session, after, after_event)
        attached = session.attached_until > _now()
        hold = bool(wait_s) and not commands and not events and _HELD < MAX_HELD_REQUESTS
        if hold:
            _HELD += 1
            try:
                deadline = _now() + wait_s
                while True:
                    remaining = deadline - _now()
                    if remaining <= 0:
                        break
                    _CHANGED.wait(remaining)
                    session = _SESSIONS.get(session_id)
                    if session is None:
                        return None
                    commands, events = _pending_for(session, after, after_event)
                    if commands or events:
                        break
            finally:
                _HELD -= 1
            session.last_seen = _now()
            attached = session.attached_until > _now()
        for command in commands:
            command.status = "delivered"
        return {
            "commands": [c.describe() for c in commands],
            "events": list(events),
            "held": hold,
            # Tells the tab which speed to poll at next.
            "attached": attached,
            "budget_exhausted": bool(wait_s) and not hold and not commands and not events,
        }


def ack(session_id, command_id, *, status, result=None, error=None, warning=None,
        resulting_revision=None) -> dict | None:
    with _LOCK:
        command = _COMMANDS.get(command_id)
        if command is None:
            return None
        if status not in ("done", "rejected", "unsupported"):
            status = "rejected"
            error = error or "unknown status"
        command.status = status
        command.result = result if isinstance(result, dict) else (
            {"value": result} if result is not None else None)
        command.error = error
        command.warning = warning
        command.resulting_revision = resulting_revision
        command.acked = _now()
        session = _SESSIONS.get(session_id)
        if session is not None:
            session.last_seen = _now()
            if resulting_revision is not None:
                session.revision = int(resulting_revision)
            if command.type == "get_state" and command.result:
                session.state = command.result
        _CHANGED.notify_all()
        return command.describe()


# -- the agent's side --------------------------------------------------------


def list_sessions(project=None) -> list:
    with _LOCK:
        _expire()
        now = _now()
        return [s.describe(now) for s in sorted(_SESSIONS.values(), key=lambda s: -s.last_seen)
                if project is None or s.project == project]


def describe(session_id) -> dict | None:
    with _LOCK:
        _expire()
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        out = session.describe()
        out["last_state"] = session.state
        return out


def attach(session_id, ttl_s=ATTACH_TTL_S) -> dict | None:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        session.attached_until = _now() + max(1.0, float(ttl_s))
        _CHANGED.notify_all()
        return session.describe()


def detach(session_id) -> dict | None:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        session.attached_until = 0.0
        return session.describe()


def enqueue(session_id, type, arguments=None, *, expected_revision=None, origin="agent"):
    """Queue one command; returns it, or None when the session is unknown."""
    with _LOCK:
        _expire()
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        session.seq += 1
        command = Command(command_id=f"cmd_{uuid.uuid4().hex[:12]}", seq=session.seq,
                          type=str(type), arguments=dict(arguments or {}), created=_now(),
                          expected_revision=expected_revision, origin=origin)
        if len(session.commands) == session.commands.maxlen:
            dropped = session.commands[0]
            if dropped.status in ("pending", "delivered"):
                dropped.status = "expired"
                dropped.error = "the viewer's command queue overflowed"
        session.commands.append(command)
        _COMMANDS[command.command_id] = command
        # Sending to a tab is an attachment in itself: it should start holding.
        session.attached_until = max(session.attached_until, _now() + ATTACH_TTL_S)
        _CHANGED.notify_all()
        return command


def command(command_id) -> dict | None:
    with _LOCK:
        found = _COMMANDS.get(command_id)
        return found.describe() if found is not None else None


def wait_for_ack(command_id, timeout_s=DEFAULT_SEND_TIMEOUT_S) -> dict | None:
    """The command once acknowledged, or as it stands when `timeout_s` runs out."""
    deadline = _now() + max(0.0, float(timeout_s))
    with _LOCK:
        while True:
            found = _COMMANDS.get(command_id)
            if found is None:
                return None
            if found.status not in ("pending", "delivered"):
                return found.describe()
            remaining = deadline - _now()
            if remaining <= 0:
                return found.describe()
            _CHANGED.wait(remaining)


def publish(project, plugin, kind, payload=None, *, origin="agent") -> int:
    """An event to every tab showing `project` (every tab when None).
    Returns how many tabs it was queued for."""
    with _LOCK:
        _expire()
        delivered = 0
        for session in _SESSIONS.values():
            if project is not None and session.project != project:
                continue
            session.event_seq += 1
            session.events.append({"event_seq": session.event_seq,
                                   "event_id": next(_EVENT_COUNTER),
                                   "project": project, "plugin": plugin, "kind": kind,
                                   "payload": dict(payload or {}), "origin": origin,
                                   "created": _now()})
            delivered += 1
        if delivered:
            _CHANGED.notify_all()
        return delivered
