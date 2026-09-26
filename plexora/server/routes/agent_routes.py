"""`/agent/v1`: the viewer control plane.

Two audiences on one blueprint. An open viewer tab (client/src/js/services/
agentBridge.js) registers, long-polls for commands and events, and
acknowledges what it did. An agent's MCP process (plexora/agent/viewer.py)
lists the tabs, sends commands and waits for the acknowledgement, and
publishes "this project's state changed" events. The state lives in
server/models/viewer_sessions.py; this module is only the wire.

Guarded twice over. When the server has a token (Open OnDemand, a notebook
sidecar, the desktop app) the global guard has already demanded it by the
time a request gets here. When it has none -- a plain local launch -- these
routes answer loopback clients only: driving somebody's viewer is not
something a neighbour on the network gets to do just because the tiles are
public.
"""

from __future__ import annotations

import ipaddress

from flask import Blueprint, Response, abort, current_app, jsonify, request

from plexora.server.models import viewer_sessions

agent_bp = Blueprint("agent_v1", __name__)


@agent_bp.before_request
def _loopback_unless_token():
    if current_app.config.get("PLEXORA_AUTH_TOKEN"):
        return None
    address = request.remote_addr or ""
    try:
        if ipaddress.ip_address(address).is_loopback:
            return None
    except ValueError:
        pass
    return jsonify(success=False, error="the agent API answers this machine only"), 403


def _body():
    return request.get_json(silent=True) or {}


def _float(name, default=0.0):
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name, default=0):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


# -- the tab's side --------------------------------------------------------


@agent_bp.route("/viewer/sessions", methods=["POST"])
def register_session():
    body = _body()
    described = viewer_sessions.register(
        session_id=body.get("session_id"), project=body.get("project"),
        dataset=body.get("dataset"), client=body.get("client") or "browser",
        capabilities=body.get("capabilities") or [], tools=body.get("tools") or [],
        page_url=body.get("page_url") or "", title=body.get("title") or "",
        revision=body.get("revision") or 0)
    return jsonify(success=True, session=described,
                   idle_poll_s=5, held_poll_s=viewer_sessions.MAX_WAIT_S)


@agent_bp.route("/viewer/sessions/<session_id>/commands", methods=["GET"])
def poll_commands(session_id):
    visible = request.args.get("visible")
    work = viewer_sessions.wait_for_work(
        session_id, after=_int("after"), after_event=_int("after_event"),
        wait_s=_float("wait"), revision=request.args.get("revision", type=int),
        visible=None if visible is None else visible in ("1", "true"))
    if work is None:
        return jsonify(success=False, error="unknown viewer session"), 404
    return jsonify(success=True, **work)


@agent_bp.route("/viewer/sessions/<session_id>/acks", methods=["POST"])
def ack_command(session_id):
    body = _body()
    acked = viewer_sessions.ack(
        session_id, body.get("command_id"), status=body.get("status") or "done",
        result=body.get("result"), error=body.get("error"), warning=body.get("warning"),
        resulting_revision=body.get("resulting_revision"))
    if acked is None:
        return jsonify(success=False, error="unknown command"), 404
    return jsonify(success=True, command=acked)


@agent_bp.route("/viewer/sessions/<session_id>/leave", methods=["POST"])
def leave_session(session_id):
    body = _body()
    return jsonify(success=viewer_sessions.leave(
        session_id, navigating_to=body.get("navigating_to")))


@agent_bp.route("/viewer/sessions/<session_id>/captures", methods=["POST"])
def post_capture(session_id):
    """A PNG of what the tab is showing, kept in the artifact store."""
    from plexora.agent import artifacts

    data = request.get_data(cache=False)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return jsonify(success=False, error="a capture must be a PNG"), 400
    if len(data) > 32 * 1024 * 1024:
        return jsonify(success=False, error="capture too large"), 413
    described = viewer_sessions.describe(session_id) or {}
    project = request.args.get("project") or described.get("project") or "_viewer"
    manifest = {"kind": "plexora.viewer_capture", "project": project,
                "view_id": session_id, "command_id": request.args.get("command_id"),
                "state": described.get("last_state"), "egress": "rendered_pixels"}
    artifact = artifacts.put(project, data, manifest, kind="viewer_capture")
    return jsonify(success=True, artifact=artifact)


# -- the agent's side --------------------------------------------------------


@agent_bp.route("/viewer/sessions", methods=["GET"])
def list_sessions():
    return jsonify(success=True,
                   sessions=viewer_sessions.list_sessions(request.args.get("project")))


@agent_bp.route("/viewer/sessions/<session_id>", methods=["GET"])
def describe_session(session_id):
    described = viewer_sessions.describe(session_id)
    if described is None:
        return jsonify(success=False, error="unknown viewer session"), 404
    return jsonify(success=True, session=described)


@agent_bp.route("/viewer/sessions/<session_id>/commands", methods=["POST"])
def send_command(session_id):
    body = _body()
    if not body.get("type"):
        return jsonify(success=False, error="a command needs a type"), 400
    queued = viewer_sessions.enqueue(session_id, body["type"], body.get("arguments") or {},
                                     expected_revision=body.get("expected_revision"),
                                     origin=body.get("origin") or "agent")
    if queued is None:
        return jsonify(success=False, error="unknown viewer session",
                       code="viewer_not_available"), 404
    wait_s = min(float(body.get("wait_s", viewer_sessions.DEFAULT_SEND_TIMEOUT_S) or 0),
                 viewer_sessions.DEFAULT_SEND_TIMEOUT_S)
    if wait_s <= 0:
        return jsonify(success=True, pending=True, command=queued.describe()), 202
    answered = viewer_sessions.wait_for_ack(queued.command_id, wait_s)
    if answered["status"] in ("pending", "delivered"):
        return jsonify(success=False, pending=True, code="viewer_not_responding",
                       command=answered), 504
    return jsonify(success=True, command=answered)


@agent_bp.route("/viewer/commands/<command_id>", methods=["GET"])
def get_command(command_id):
    found = viewer_sessions.command(command_id)
    if found is None:
        abort(404)
    return jsonify(success=True, command=found)


@agent_bp.route("/viewer/sessions/<session_id>/attach", methods=["POST"])
def attach(session_id):
    body = _body()
    described = viewer_sessions.attach(session_id, body.get("ttl_s")
                                       or viewer_sessions.ATTACH_TTL_S)
    if described is None:
        return jsonify(success=False, error="unknown viewer session"), 404
    return jsonify(success=True, session=described)


@agent_bp.route("/viewer/sessions/<session_id>/detach", methods=["POST"])
def detach(session_id):
    described = viewer_sessions.detach(session_id)
    if described is None:
        return jsonify(success=False, error="unknown viewer session"), 404
    return jsonify(success=True, session=described)


@agent_bp.route("/events", methods=["POST"])
def publish_event():
    body = _body()
    if not body.get("plugin") or not body.get("kind"):
        return jsonify(success=False, error="an event needs a plugin and a kind"), 400
    delivered = viewer_sessions.publish(body.get("project"), body["plugin"], body["kind"],
                                        body.get("payload") or {},
                                        origin=body.get("origin") or "agent")
    return jsonify(success=True, delivered=delivered)


@agent_bp.route("/captures/<artifact_id>", methods=["GET"])
def get_capture(artifact_id):
    from plexora.agent import artifacts

    try:
        png, _sidecar = artifacts.get(artifact_id)
    except KeyError:
        abort(404)
    return Response(png, mimetype="image/png")
