"""A session on disk: the record, its packets, its decisions, and who holds it.

    <data_root>/.agent/sessions/<kind>/<session_id>/
        session.json      the record (atomic writes)
        control.json      pause / take-over, written by the viewer's route
        decisions.jsonl   every packet issued and every answer applied
        packets/<id>.json the outstanding (and recent) packets
        packets/<id>/     their images, as sent
        report.html       the review report, once written
        .lock             the process driving the session (pid)

The thread that runs a session's bulk job and the tool calls answering its
packets are in one process; `lock` serialises them. A second process that
tries to drive the same session is refused (`conflict`) while the first is
alive, and takes over when it is not.

The store is spatialagents' (`spatialagents.sessions`), which Plexora's
conversations are kept in too; this is Plexora's binding of it: rooted under
Plexora's data directory, naming Plexora in what a second driver is told, and
pointing an unknown session id at `gating_session_status`.
"""

from __future__ import annotations

from pathlib import Path

from spatialagents import sessions as _sessions
from spatialagents.sessions import KEEP_DAYS, KEEP_SESSIONS, SessionBusy, atomic_write, new_session_id

__all__ = ["KEEP_DAYS", "KEEP_SESSIONS", "SessionBusy", "SessionStore", "atomic_write", "new_session_id"]


class SessionStore(_sessions.SessionStore):
    """The sessions of one kind under one data root."""

    HOLDER = "another Plexora process"
    LIST_HINT = "gating_session_status lists the sessions"

    def __init__(self, kind: str, root: Path | None = None):
        if root is None:
            from plexora import paths

            root = paths.agent_root() / "sessions" / kind
        super().__init__(kind, root)
