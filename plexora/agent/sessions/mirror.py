"""Showing an open viewer what a session is looking at -- best effort, always.

A mirrored session sends the open tab a short script of viewer commands per
packet (switch image, set the calibrated channels, fly to the cells, preview
the candidate gate on the slider, highlight the cells shown). Mirroring must
never be the reason a session fails: every command has a short timeout, a
failed command marks the mirror `degraded` and the script goes on, and a
viewer that has gone away turns mirroring `off` for the session -- packets
keep coming, headless.
"""

from __future__ import annotations

import time

#: Commands after which the viewer is given longer to settle before the next.
DWELL_AFTER = ("fit_region", "focus_cell", "preview_gate", "open_project")


def run_script(send, script, *, delay_ms=600, dwell_factor=2.0, sleep=time.sleep) -> dict:
    """Send `script` = [{type, arguments}] through `send(type, arguments)`.

    Returns {status: ok|degraded|off, sent, errors}. `send` raises an
    AgentError for a refused or unanswered command.
    """
    from plexora.agent.errors import AgentError

    sent, errors = 0, []
    status = "ok"
    for index, command in enumerate(script):
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
        if index < len(script) - 1 and delay_ms:
            pause = delay_ms / 1000.0
            if command["type"] in DWELL_AFTER:
                pause *= dwell_factor
            sleep(pause)
    return {"status": status, "sent": sent, "errors": errors[:10]}
