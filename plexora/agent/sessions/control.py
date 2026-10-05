"""The viewer's hand on a running session: pause, resume, stop, limits, take
over, and detaching or re-attaching the viewer.

A tab mirroring a session posts one of `events.ACTIONS` to the session's
control route. The route is the user's own control, so nothing licence- or
permission-shaped guards it; it only writes the session's `control.json`,
which the engine reads at its next call. Each workflow mounts this under its
own blueprint and supplies what differs: how a unit is taken over (gating
locks the marker's gate, QC locks the region) and what a finished session's
summary says.

Running and mirroring are separate: `detach_viewer` stops mirroring into the
tab (`viewer_detached` in control) while the session goes on, `attach_viewer`
mirrors it again, and `take_over` is a pause plus a detach. Resume never
re-attaches. Every `control` event the tabs are told carries the whole
picture -- `paused`, `paused_by`, `viewer_attached` and the mirror's
`view_id` -- because it reaches every tab on the project and only the named
one is the tab being mirrored into.
"""

from __future__ import annotations


class BadRequest(ValueError):
    """The posted action cannot be carried out (the route answers 400)."""


def _mirror(store, session_id):
    try:
        return store.load(session_id).get("mirror") or {}
    except Exception:
        return {}


def handle(store, session_id, post, *, tell_tabs, summary_of, record_limit_answers,
           limit_decisions, take_over=None, replay_mirror=None) -> dict:
    """Apply one posted control action; returns the session's control state.

    `tell_tabs(event, record=None, **payload)` announces to the session's tabs;
    `take_over(post, control)` returns the control changes a take-over makes
    (after the user's act on the unit), or None when the workflow has none;
    `replay_mirror(session_id, post, control)` shows the session's current packet in the
    tab again after `attach_viewer` (best effort, may return at once)."""
    action = post.get("action")

    def tell_control(control, **extra):
        # Attached means mirroring AND not detached: a session started with
        # mirror=false was never attached, and pausing it must not say it is.
        mirror = _mirror(store, session_id)
        mirroring = bool(mirror.get("enabled")) and mirror.get("status") != "off"
        tell_tabs("control", paused=bool(control.get("paused")),
                  paused_by=control.get("paused_by"),
                  viewer_attached=mirroring and not control.get("viewer_detached"),
                  view_id=mirror.get("view_id"), **extra)

    if action == "pause":
        control = store.set_control(session_id, paused=True, paused_by="viewer")
        tell_control(control)
    elif action == "resume":
        # The viewer stays as it is: a run the user took over goes on in the
        # background until they choose to watch it again.
        control = store.set_control(session_id, paused=False, paused_by=None)
        tell_control(control)
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
                                    viewer_detached=True, **(changes or {}))
        tell_control(control, taken_over=True)
    elif action == "detach_viewer":
        # Nothing is sent to the tab: the panel gives the user their own view
        # back itself, and a script under way stops at its next command.
        control = store.set_control(session_id, viewer_detached=True)
        tell_control(control)
    elif action == "attach_viewer":
        control = store.set_control(session_id, viewer_detached=False)
        with store.lock(session_id):
            record = store.load(session_id)
            mirror = record.setdefault("mirror", {})
            mirror.update(enabled=True, status="pending",
                          view_id=post.get("view_id") or mirror.get("view_id"))
            store.save(record)
        tell_control(control)
        if replay_mirror is not None:
            replay_mirror(session_id, post, control)
    else:
        raise BadRequest(f"unknown action {action!r}")
    return control


def replayer(status_capability, plugins, *, notify):
    """A `replay_mirror` for `handle`: after `attach_viewer`, show the
    session's current packet in the posting tab again, through the workflow's
    own status capability (`reattach_viewer` + `replay_mirror`) as the
    viewer, on a thread -- the route answers at once, and a packet's script
    takes seconds. Best effort: a refusal leaves the mirror `pending`, and
    the next packet is shown."""
    import dataclasses
    import threading

    def replay(session_id, post, control):
        def run():
            from plexora.agent import AgentSession, registry
            from plexora.agent.policy import Policy

            try:
                try:
                    registry.get(status_capability)
                except Exception:
                    registry.discover(list(plugins))
                policy = dataclasses.replace(Policy.from_flags(), principal="viewer")
                registry.invoke(AgentSession(), status_capability,
                                {"session_id": session_id, "reattach_viewer": True,
                                 "replay_mirror": True, "view_id": post.get("view_id")},
                                policy=policy, notify=notify)
            except Exception:
                pass

        threading.Thread(target=run, name=f"replay-{session_id}", daemon=True).start()

    return replay
