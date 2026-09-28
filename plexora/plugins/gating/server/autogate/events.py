"""What an open viewer is told about a gating session, and how it answers.

Every `gating.session` event (`schemas.SESSION_EVENTS`) carries the session id,
the event's name and `control` -- the route a tab posts pause / resume / stop /
take-over to -- so core's agent panel acts on a session without knowing which
plugin runs it, and a reloaded tab can still act on one already running.
Gating's binding of `plexora.agent.sessions.events`.
"""

from __future__ import annotations

from plexora.agent.sessions.events import ACTIONS, Events
from plexora.plugins.gating.server.autogate import schemas

KIND = "gating.session"
OWNER = "gating"


def _prefix():
    from plexora.plugins.gating import PLUGIN

    return PLUGIN.url_prefix


EVENTS = Events(KIND, OWNER, _prefix, schemas.SESSION_EVENTS, ACTIONS)


def control_for(session_id) -> dict:
    return EVENTS.control_for(session_id)


def payload(session_id, event, /, **fields) -> dict:
    return EVENTS.payload(session_id, event, **fields)


def announce(notify, projects, session_id, event, /, **fields) -> int:
    return EVENTS.announce(notify, projects, session_id, event, **fields)
