"""What an open viewer is told about a session, and where it answers.

Every session event carries the session id, the event's name and `control` --
the route a tab posts pause / resume / stop / take-over / detach / attach to -- so core's agent
panel acts on a session without knowing which plugin runs it, and a reloaded
tab can still act on one already running. A workflow binds one `Events` to its
kind (`gating.session`, `qc.session`), its owner plugin and its event names.
"""

from __future__ import annotations

#: `detach_viewer` lets the session go on without the tab ("Continue in
#: background"); `attach_viewer` mirrors it into the posting tab again.
ACTIONS = ("pause", "resume", "stop", "take_over", "limit", "detach_viewer", "attach_viewer")


class Events:
    def __init__(self, kind, owner, url_prefix, allowed, actions=ACTIONS):
        self.kind = kind
        self.owner = owner
        self._url_prefix = url_prefix
        self.allowed = tuple(allowed)
        self.actions = tuple(actions)

    def url_prefix(self) -> str:
        prefix = self._url_prefix() if callable(self._url_prefix) else self._url_prefix
        return str(prefix).lstrip("/")

    def control_for(self, session_id) -> dict:
        return {"url": f"{self.url_prefix()}/agent_session/{session_id}/control",
                "actions": list(self.actions)}

    def payload(self, session_id, event, /, **fields) -> dict:
        if event not in self.allowed:
            raise ValueError(f"unknown session event {event!r}")
        return {"session_id": session_id, "event": event,
                "control": self.control_for(session_id), **fields}

    def announce(self, notify, projects, session_id, event, /, **fields) -> int:
        """Send one event to every tab open on one of the session's images.
        Best effort: returns how many projects were told."""
        if notify is None:
            return 0
        body = self.payload(session_id, event, **fields)
        told = 0
        for project in dict.fromkeys(p for p in projects or () if p):
            try:
                notify(project, self.owner, self.kind, body)
                told += 1
            except Exception:
                pass
        return told
