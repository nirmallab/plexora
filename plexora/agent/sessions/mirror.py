"""Showing an open viewer what a session is looking at -- best effort, always.

A mirrored session sends the open tab a short script of viewer commands per
packet (switch image, set the calibrated channels, fly to the cells, preview
the candidate gate on the slider, highlight the cells shown). Mirroring must
never be the reason a session fails: every command has a short timeout, a
failed command marks the mirror `degraded` and the script goes on, and a
viewer that has gone away turns mirroring `off` for the session -- packets
keep coming, headless.

The user may also detach the viewer while the session runs ("Continue in
background"): the session's control then says `viewer_detached`, and a
script already under way stops at its next command (`keep_going`).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

#: Milliseconds between two commands of a script (a session may change it).
DEFAULT_DELAY_MS = 600

#: Commands after which the viewer is given longer to settle before the next.
DWELL_AFTER = ("fit_region", "focus_cell", "preview_gate", "open_project")


_SEND_GUARD = threading.Lock()
_SEND_LOCKS: dict = {}


@contextmanager
def send_lock(session_id):
    """One script at a time into a session's viewer: the harness's own send
    and a replay after the user re-attaches must not interleave."""
    with _SEND_GUARD:
        lock = _SEND_LOCKS.setdefault(str(session_id), threading.Lock())
    with lock:
        yield


def run_script(send, script, *, delay_ms=DEFAULT_DELAY_MS, dwell_factor=2.0,
               sleep=time.sleep, keep_going=None) -> dict:
    """Send `script` = [{type, arguments}] through `send(type, arguments)`.

    Returns {status: ok|degraded|off, sent, errors}, plus `aborted: true`
    when `keep_going()` turned false before a command (the user detached the
    viewer). `send` raises an AgentError for a refused or unanswered command.
    """
    from plexora.agent.errors import AgentError

    sent, errors = 0, []
    status = "ok"
    for index, command in enumerate(script):
        if keep_going is not None and not keep_going():
            return {"status": status, "sent": sent, "errors": errors[:10], "aborted": True}
        try:
            send(command["type"], command.get("arguments") or {})
            sent += 1
        except AgentError as exc:
            errors.append({"command": command["type"], "code": exc.code,
                           "message": exc.message})
            if exc.code == "viewer_not_available":
                return {"status": "off", "sent": sent, "errors": errors}
            status = "degraded"
        except Exception as exc:  # a broken tab must not break the session
            errors.append({"command": command["type"], "code": "internal_error",
                           "message": str(exc)})
            status = "degraded"
        if index < len(script) - 1 and keep_going is not None and not keep_going():
            return {"status": status, "sent": sent, "errors": errors[:10], "aborted": True}
        if index < len(script) - 1 and delay_ms:
            pause = delay_ms / 1000.0
            if command["type"] in DWELL_AFTER:
                pause *= dwell_factor
            sleep(pause)
    return {"status": status, "sent": sent, "errors": errors[:10]}


#: Seconds a command may take to acknowledge (switching image or HD mode
#: rebuilds every tile and is acknowledged when that finishes).
COMMAND_TIMEOUT_S = {"open_project": 20.0, "set_hd_mode": 20.0, "show_evidence": 10.0,
                     "restore_viewer": 20.0}
DEFAULT_TIMEOUT_S = 5.0
#: The first command of a script: long enough for a background tab to wake.
WAKE_TIMEOUT_S = 10.0


def open_view(call, view_id):
    """(control, view, state) of the viewer a session mirrors into, or a
    result dict {status: off, ...} when there is none. `state` is the tab's
    `get_state` (None when it did not answer), which also wakes a throttled
    background tab."""
    from plexora.agent import viewer
    from plexora.agent.errors import AgentError

    try:
        control = viewer.require(call.link)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    state = None
    try:
        state = (control.send(view["view_id"], "get_state", {},
                              timeout=WAKE_TIMEOUT_S) or {}).get("result")
    except AgentError as exc:
        if exc.code == "viewer_not_available":
            return {"status": "off", "sent": 0, "view_id": view["view_id"],
                    "errors": [{"code": exc.code, "message": exc.message}]}
    return control, view, state


def send_script(control, view, script, *, delay_ms=DEFAULT_DELAY_MS, timeouts=None,
                keep_going=None):
    """Run `script` against an opened view (`open_view`); the result carries
    the view id."""
    timeouts = {**COMMAND_TIMEOUT_S, **(timeouts or {})}

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=timeouts.get(type, DEFAULT_TIMEOUT_S))

    result = run_script(send, script, delay_ms=delay_ms, keep_going=keep_going)
    result["view_id"] = view["view_id"]
    return result


def attached(store, session_id):
    """A `keep_going` for `run_script`: false once the user has detached the
    session's viewer."""
    return lambda: not store.control(session_id).get("viewer_detached")
