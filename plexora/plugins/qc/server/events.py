"""What an open viewer is told about a QC session (`qc.session` events).

QC's binding of `plexora.agent.sessions.events`: every event carries the
session id and the control route a tab posts pause / resume / stop /
take-over to, so core's agent panel acts on a QC session exactly as on a
gating one.
"""

from __future__ import annotations

from plexora.agent.sessions.events import ACTIONS, Events
from plexora.plugins.qc.server import schemas

KIND = "qc.session"
OWNER = "qc"


def _prefix():
    from plexora.plugins.qc import PLUGIN

    return PLUGIN.url_prefix


EVENTS = Events(KIND, OWNER, _prefix, schemas.SESSION_EVENTS, ACTIONS)


def control_for(session_id) -> dict:
    return EVENTS.control_for(session_id)


def payload(session_id, event, /, **fields) -> dict:
    return EVENTS.payload(session_id, event, **fields)


def announce(notify, projects, session_id, event, /, **fields) -> int:
    return EVENTS.announce(notify, projects, session_id, event, **fields)
