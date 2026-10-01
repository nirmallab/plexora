"""`/ai/v1/conversations`: the chat panel's wire to a Plexora AI conversation.

    POST /ai/v1/conversations                  start one          (ai.chat_start)
    GET  /ai/v1/conversations                  list them          (ai.chat_history)
    GET  /ai/v1/conversations/<id>             one, with events   (ai.chat_history)
    POST /ai/v1/conversations/<id>/messages    the user's message (ai.chat_send)
    GET  /ai/v1/conversations/<id>/events      SSE, or a held poll (?after=&wait=)
    POST /ai/v1/conversations/<id>/control     pause|resume|stop  (ai.chat_control)
    POST /ai/v1/conversations/<id>/approve     approve|deny       (ai.chat_approve)
    POST /ai/v1/conversations/<id>/undo        an Undo chip       (undo_operation)

Every route calls `registry.invoke` on an `ai.chat_*` capability, so the
licence check, validation and audit are those of any other surface.

`/events` streams server-sent events when the client asks for them (`Accept:
text/event-stream`, what `EventSource` sends) and otherwise answers a held
poll -- the agent bridge's idiom (agentBridge.js), and the panel's fallback
when a proxy in front buffers the stream.

Guarded like `/agent/v1`: with no server token, loopback clients only; and
behind the `ai:chat` entitlement (a Free licence answers the structured 403
the paid-feature modal reads). The person at this viewer is the one who
approves, so `/approve` is invoked with the policy that lets an approved call
run; the call itself still needs that approval, one call at a time.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import time

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from plexora.licensing import guards

ai_chat_bp = Blueprint("ai_chat_v1", __name__)
ENTITLEMENT = "ai:chat"
#: How long one SSE response stays open before the client reconnects.
STREAM_S = 300.0
HELD_POLL_S = 20.0


@ai_chat_bp.before_request
def _loopback_unless_token():
    if current_app.config.get("PLEXORA_AUTH_TOKEN"):
        return None
    address = request.remote_addr or ""
    try:
        if ipaddress.ip_address(address).is_loopback:
            return None
    except ValueError:
        pass
    return jsonify(success=False, error="Plexora AI answers this machine only"), 403


guards.guard_blueprint(ai_chat_bp, ENTITLEMENT)

_SESSION = None


def _session():
    global _SESSION
    from plexora.agent import AgentSession, registry

    try:
        registry.get("ai.chat_start")
    except Exception:
        registry.discover_installed(current_app)
    if _SESSION is None:
        _SESSION = AgentSession()
    return _SESSION


def _policy(*, approving: bool = False):
    from plexora.agent.policy import Policy

    policy = dataclasses.replace(Policy(), principal="viewer")
    if approving:
        policy = dataclasses.replace(policy, allow_source_writes=True, allow_destructive=True)
    return policy


def _invoke(tool, arguments, *, approving=False):
    from plexora.agent import registry

    def notify(project, plugin, kind, body):
        from plexora import api

        return api.notify_viewers(project, plugin, kind, body)

    answer = registry.invoke(_session(), tool, arguments, policy=_policy(approving=approving), notify=notify)
    if not answer["ok"]:
        error = answer["error"]
        status = {"conflict": 409, "license_required": 403, "permission_required": 403}.get(error.get("code"), 400)
        return jsonify(success=False, ok=False, error=error), status
    return jsonify(success=True, ok=True, **(answer["result"] or {}))


def _body():
    return request.get_json(silent=True) or {}


def _number(name, default):
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@ai_chat_bp.route("/conversations", methods=["POST"])
def start_conversation():
    body = _body()
    return _invoke("ai.chat_start", {"title": body.get("title"), "viewer": bool(body.get("viewer"))})


@ai_chat_bp.route("/conversations", methods=["GET"])
def list_conversations():
    return _invoke("ai.chat_history", {"limit": int(_number("limit", 50))})


@ai_chat_bp.route("/conversations/<conversation_id>", methods=["GET"])
def get_conversation(conversation_id):
    return _invoke("ai.chat_history", {"conversation_id": conversation_id, "after": int(_number("after", 0))})


@ai_chat_bp.route("/conversations/<conversation_id>/messages", methods=["POST"])
def send_message(conversation_id):
    body = _body()
    return _invoke("ai.chat_send", {"conversation_id": conversation_id, "text": body.get("text") or "",
                                    "images": body.get("images") or []})


@ai_chat_bp.route("/conversations/<conversation_id>/control", methods=["POST"])
def control_conversation(conversation_id):
    return _invoke("ai.chat_control", {"conversation_id": conversation_id, "action": _body().get("action")})


@ai_chat_bp.route("/conversations/<conversation_id>/approve", methods=["POST"])
def approve_call(conversation_id):
    body = _body()
    return _invoke("ai.chat_approve", {"conversation_id": conversation_id,
                                       "approval_id": body.get("approval_id"),
                                       "decision": body.get("decision")}, approving=True)


@ai_chat_bp.route("/conversations/<conversation_id>/undo", methods=["POST"])
def undo_operation(conversation_id):
    """The panel's Undo chip: `undo_operation` on a write the conversation made,
    and a note the model reads at the start of its next turn."""
    from plexora.ai.harness import conversations

    operation_id = str(_body().get("operation_id") or "")
    service = conversations.service()
    if not service.store.exists(conversation_id):
        return jsonify(success=False, error=f"no conversation {conversation_id!r}"), 404
    made = {e.get("operation_id") for e in service.store.decisions(conversation_id) if e.get("event") == "tool_result"}
    if not operation_id or operation_id not in made:
        return jsonify(success=False, error="that operation was not made by this conversation"), 400
    answer = _invoke("undo_operation", {"operation_id": operation_id})
    body = answer[0] if isinstance(answer, tuple) else answer
    if body.get_json().get("ok"):
        service.note(conversation_id, f"[The user undid operation {operation_id} from the chat panel.]")
        service.store.append_event(conversation_id, {"event": "undone", "operation_id": operation_id})
    return answer


@ai_chat_bp.route("/conversations/<conversation_id>/events", methods=["GET"])
def conversation_events(conversation_id):
    after = int(_number("after", 0))
    wants_stream = "text/event-stream" in (request.headers.get("Accept") or "") or request.args.get("stream") == "1"
    if not wants_stream:
        wait = max(0.0, min(_number("wait", 0), HELD_POLL_S))
        return _invoke("ai.chat_history", {"conversation_id": conversation_id, "after": after, "wait_s": wait})
    from plexora.ai.harness import conversations

    service = conversations.service()
    if not service.store.exists(conversation_id):
        return jsonify(success=False, error=f"no conversation {conversation_id!r}"), 404
    last = request.headers.get("Last-Event-ID")
    if last and last.isdigit():
        after = int(last)

    def stream():
        cursor = after
        deadline = time.monotonic() + STREAM_S
        yield "retry: 2000\n\n"
        while time.monotonic() < deadline:
            polled = service.events(conversation_id, cursor, wait_s=10.0)
            for event in polled["events"]:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: {event.get('event', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"
            if not polled["events"]:
                if not polled["running"] and request.args.get("until_idle") == "1":
                    yield f"event: idle\ndata: {json.dumps({'after': cursor})}\n\n"
                    return
                yield ": keep-alive\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
