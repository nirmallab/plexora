"""Driving an open Plexora viewer from an agent.

One interface, two transports. Inside a running Plexora server the session
registry is right here (`InProcessViewerControl`); from the MCP process it is
reached over loopback HTTP with the server's token (`RemoteViewerControl`,
through an `attach.ServerLink`). Either way a command is queued for one tab,
the tab runs it (client/src/js/services/agentBridge.js) and acknowledges, and
the sender gets the acknowledgement or a clear `viewer_not_responding`.

Nothing here opens a browser. With no viewer open, every viewer tool answers
`viewer_not_available` and says how to open one; the headless tools keep
working regardless.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError

NOT_AVAILABLE_HINT = ("open the project in Plexora (desktop app, `plexora`, or the "
                      "notebook viewer); the tab registers itself within seconds")


class InProcessViewerControl:
    """The registry in this process -- for code running inside the server."""

    kind = "in_process"

    def _sessions(self):
        from plexora.server.models import viewer_sessions

        return viewer_sessions

    def list_sessions(self, project=None):
        return self._sessions().list_sessions(project)

    def describe_session(self, view_id):
        return self._sessions().describe(view_id)

    def attach(self, view_id, ttl_s=None):
        sessions = self._sessions()
        return sessions.attach(view_id, ttl_s or sessions.ATTACH_TTL_S)

    def send(self, view_id, type, arguments=None, *, timeout=None, expected_revision=None):
        sessions = self._sessions()
        queued = sessions.enqueue(view_id, type, arguments or {},
                                  expected_revision=expected_revision)
        if queued is None:
            raise AgentError("viewer_not_available", f"no open viewer {view_id!r}",
                             detail={"hint": NOT_AVAILABLE_HINT})
        answered = sessions.wait_for_ack(queued.command_id,
                                         timeout or sessions.DEFAULT_SEND_TIMEOUT_S)
        return _checked(answered)

    def notify(self, project, plugin, kind, payload=None):
        return self._sessions().publish(project, plugin, kind, payload or {}) > 0


class RemoteViewerControl:
    """A running Plexora server's registry, over its `/agent/v1` routes."""

    kind = "remote"

    def __init__(self, link):
        self.link = link

    def _call(self, method, path, body=None, timeout=5.0):
        try:
            return self.link.request(method, path, body, timeout=timeout)
        except OSError as exc:
            raise AgentError("viewer_not_available",
                             f"the Plexora server at {self.link.base_url} did not answer",
                             detail={"hint": NOT_AVAILABLE_HINT}, retryable=True) from exc

    def list_sessions(self, project=None):
        path = "/agent/v1/viewer/sessions"
        if project:
            from urllib.parse import quote

            path += f"?project={quote(project)}"
        status, answer = self._call("GET", path)
        if status == 404:
            raise AgentError("capability_unavailable",
                             "the attached Plexora server has no viewer control plane "
                             "(it predates it)")
        if status != 200 or not isinstance(answer, dict):
            raise AgentError("viewer_not_available", f"listing viewers failed ({status})")
        return answer.get("sessions", [])

    def describe_session(self, view_id):
        status, answer = self._call("GET", f"/agent/v1/viewer/sessions/{view_id}")
        return answer.get("session") if status == 200 and isinstance(answer, dict) else None

    def attach(self, view_id, ttl_s=None):
        status, answer = self._call("POST", f"/agent/v1/viewer/sessions/{view_id}/attach",
                                    {"ttl_s": ttl_s} if ttl_s else {})
        return answer.get("session") if status == 200 and isinstance(answer, dict) else None

    def send(self, view_id, type, arguments=None, *, timeout=None, expected_revision=None):
        from plexora.server.models.viewer_sessions import DEFAULT_SEND_TIMEOUT_S

        wait = float(timeout or DEFAULT_SEND_TIMEOUT_S)
        status, answer = self._call(
            "POST", f"/agent/v1/viewer/sessions/{view_id}/commands",
            {"type": type, "arguments": arguments or {}, "wait_s": wait,
             "expected_revision": expected_revision}, timeout=wait + 5)
        if status == 404:
            raise AgentError("viewer_not_available", f"no open viewer {view_id!r}",
                             detail={"hint": NOT_AVAILABLE_HINT})
        if not isinstance(answer, dict):
            raise AgentError("viewer_not_responding", f"unexpected answer ({status})")
        return _checked(answer.get("command") or {})

    def notify(self, project, plugin, kind, payload=None):
        return self.link.notify(project, plugin, kind, payload or {})


def _checked(command):
    status = command.get("status")
    if status in ("pending", "delivered"):
        raise AgentError("viewer_not_responding",
                         "the viewer did not acknowledge the command in time (a background "
                         "tab is throttled by the browser; bring it to the front)",
                         detail={"command_id": command.get("command_id"),
                                 "status": status}, retryable=True)
    if status == "expired":
        raise AgentError("viewer_not_available", command.get("error") or "the viewer went away",
                         detail={"command_id": command.get("command_id")})
    if status == "unsupported":
        raise AgentError("capability_unavailable",
                         f"this viewer cannot run {command.get('type')!r}"
                         + (f": {command['error']}" if command.get("error") else ""),
                         detail=command)
    if status == "rejected":
        raise AgentError("invalid_input", command.get("error") or "the viewer refused",
                         detail=command)
    return command


def connect(link=None):
    """The control for this process: in-process when this IS the server, the
    link's server when attached, else None."""
    from plexora.agent.registry import _serving_in_process

    if _serving_in_process():
        return InProcessViewerControl()
    if link is not None:
        return RemoteViewerControl(link)
    return None


def require(link=None):
    control = connect(link)
    if control is None:
        raise AgentError("viewer_not_available",
                         "this agent is not attached to a running Plexora server",
                         detail={"hint": "start Plexora, then restart the MCP server (it "
                                         "finds the server) or pass --server URL --token T"})
    return control


def resolve_view(control, view_id=None, project=None):
    """The one tab a command is for: named, or the only one (for `project`)."""
    if view_id:
        found = control.describe_session(view_id)
        if not found:
            raise AgentError("viewer_not_available", f"no open viewer {view_id!r}",
                             detail={"hint": "call list_viewers"})
        return found
    sessions = control.list_sessions(project)
    live = [s for s in sessions if s.get("status") != "stale"] or sessions
    if not live:
        raise AgentError("viewer_not_available",
                         "no Plexora viewer is open" + (f" on {project!r}" if project else ""),
                         detail={"hint": NOT_AVAILABLE_HINT})
    if len(live) > 1:
        raise AgentError("ambiguous_view", f"{len(live)} viewers are open; pass view_id",
                         detail={"viewers": live})
    return live[0]
