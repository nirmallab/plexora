"""The viewer's hand on a running session: pause, resume, stop, limits, take over.

A tab mirroring a session posts one of `events.ACTIONS` to the session's
control route. The route is the user's own control, so nothing licence- or
permission-shaped guards it; it only writes the session's `control.json`,
which the engine reads at its next call. Each workflow mounts this under its
own blueprint and supplies what differs: how a unit is taken over (gating
locks the marker's gate, QC locks the region) and what a finished session's
summary says.
"""

from __future__ import annotations


class BadRequest(ValueError):
    """The posted action cannot be carried out (the route answers 400)."""


def handle(store, session_id, post, *, tell_tabs, summary_of, record_limit_answers,
           limit_decisions, take_over=None) -> dict:
    """Apply one posted control action; returns the session's control state.

    `tell_tabs(event, record=None, **payload)` announces to the session's tabs;
    `take_over(post, control)` returns the control changes a take-over makes
    (after the user's act on the unit), or None when the workflow has none."""
    action = post.get("action")
    if action == "pause":
        control = store.set_control(session_id, paused=True, paused_by="viewer")
        tell_tabs("control", paused=True, paused_by="viewer")
    elif action == "resume":
        control = store.set_control(session_id, paused=False, paused_by=None)
        tell_tabs("control", paused=False, paused_by=None)
    elif action == "stop":
        # The driving process stops at its next call, and its bulk pass at its
        # next unit; the tabs are told now, so the panel does not wait for an
        # agent that may never call again.
        from plexora.agent.audit import now_iso

        control = store.set_control(session_id, stopped=True, stopped_by="viewer",
                                    stopped_at=now_iso(), paused=False)
        record = store.load(session_id)
        tell_tabs("finished", record=record, reason="stopped", state=record.get("state"),
                  summary=summary_of(record), phase="summarizing")
    elif action == "limit":
        unit = post.get("unit") or post.get("marker")
        decision = post.get("decision")
        if not unit or decision not in limit_decisions:
            raise BadRequest("limit needs a unit and a decision")
        try:
            answered = record_limit_answers(store, session_id, store.load(session_id),
                                            {unit: decision})
        except Exception as exc:
            raise BadRequest(str(exc)) from exc
        control = store.control(session_id)
        tell_tabs("limit_answered", by="viewer",
                  answers={k.split("::", 1)[-1]: v for k, v in answered.items()})
    elif action == "take_over":
        control = store.control(session_id)
        changes = take_over(post, control) if take_over is not None else {}
        control = store.set_control(session_id, paused=True, paused_by="viewer",
                                    **(changes or {}))
    else:
        raise BadRequest(f"unknown action {action!r}")
    return control
