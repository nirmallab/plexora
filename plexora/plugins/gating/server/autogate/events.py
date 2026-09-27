"""What an open viewer is told about a gating session, and how it answers.

Every `gating.session` event (`schemas.SESSION_EVENTS`) carries the session id,
the event's name and `control` -- the route a tab posts pause / resume / stop /
take-over to -- so core's agent panel acts on a session without knowing which
plugin runs it, and a reloaded tab can still act on one already running.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import schemas

KIND = "gating.session"
OWNER = "gating"
ACTIONS = ("pause", "resume", "stop", "take_over", "limit")


def control_for(session_id) -> dict:
    from plexora.plugins.gating import PLUGIN

    return {"url": f"{PLUGIN.url_prefix.lstrip('/')}/agent_session/{session_id}/control",
            "actions": list(ACTIONS)}


def payload(session_id, event, /, **fields) -> dict:
    if event not in schemas.SESSION_EVENTS:
        raise ValueError(f"unknown session event {event!r}")
    return {"session_id": session_id, "event": event, "control": control_for(session_id),
            **fields}


def announce(notify, projects, session_id, event, /, **fields) -> int:
    """Send one event to every tab open on one of the session's images (a
    dataset session moves the tab between them). Best effort: returns how
    many projects were told."""
    if notify is None:
        return 0
    body = payload(session_id, event, **fields)
    told = 0
    for project in dict.fromkeys(p for p in projects or () if p):
        try:
            notify(project, OWNER, KIND, body)
            told += 1
        except Exception:
            pass
    return told
