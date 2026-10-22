"""`/ai/v1/conversations`: the chat panel's wire to a Plexora AI conversation.

    POST /ai/v1/conversations                  start one          (ai.chat_start)
    GET  /ai/v1/conversations                  list them          (ai.chat_history)
    GET  /ai/v1/conversations/<id>             one, with events   (ai.chat_history)
    POST /ai/v1/conversations/<id>/messages    the user's message (ai.chat_send)
    GET  /ai/v1/conversations/<id>/events      SSE, or a held poll (?after=&wait=)
    POST /ai/v1/conversations/<id>/control     pause|resume|stop  (ai.chat_control)
    POST /ai/v1/conversations/<id>/approve     approve|deny       (ai.chat_approve)
    POST /ai/v1/conversations/<id>/undo        an Undo chip       (undo_operation)

The contract itself -- paths, payloads, status codes, the event stream -- is
spatialagents' (`spatialagents.chat.http.ChatApi`), the one the shared chat
page speaks; this blueprint is Plexora's adapter onto it. What it adds is
Plexora's: every operation is a `registry.invoke` of an `ai.chat_*`
capability (`RegistryOperations`), so the licence check, validation and audit
are those of any other surface, and Undo is `undo_operation`.

`/events` streams server-sent events when the client asks for them (`Accept:
text/event-stream`, what `EventSource` sends) and otherwise answers a held
poll -- the agent bridge's idiom (agentBridge.js), and the panel's fallback
when a proxy in front buffers the stream.

Guarded like `/agent/v1`: with no server token, loopback clients only; and
behind the `ai:chat` entitlement (a Free licence answers the structured 403
the Pro-feature modal reads). The person at this viewer is the one who
approves, so `/approve` is invoked with the policy that lets an approved call
run; the call itself still needs that approval, one call at a time.
"""

from __future__ import annotations

import dataclasses
import ipaddress

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from spatialagents.chat.http import ChatApi
from spatialagents.errors import known

from plexora.agent.errors import AgentError
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


class _Refused(AgentError):
    """A capability's refusal, carried to `ChatApi` exactly as the registry said it."""

    def __init__(self, problem: dict):
        code = problem.get("code")
        super().__init__(code if code in known() else "internal_error", str(problem.get("message") or ""),
                         detail=problem.get("detail"), retryable=bool(problem.get("retryable")))
        self.problem = problem

    def to_problem(self) -> dict:
        return self.problem


def _invoke(tool, arguments, *, approving=False) -> dict:
    """`tool`'s result through `registry.invoke`, or `_Refused` with its problem."""
    from plexora.agent import registry

    def notify(project, plugin, kind, body):
        from plexora import api

        return api.notify_viewers(project, plugin, kind, body)

    answer = registry.invoke(_session(), tool, arguments, policy=_policy(approving=approving), notify=notify)
    if not answer["ok"]:
        raise _Refused(answer["error"])
    return answer["result"] or {}


class RegistryOperations:
    """`ChatApi`'s operations (those of `spatialagents.chat.http.ChatOperations`)
    as the `ai.chat_*` capabilities, for the person at the viewer."""

    def start(self, *, title=None, viewer=False) -> dict:
        return _invoke("ai.chat_start", {"title": title, "viewer": bool(viewer)})

    def history(self, conversation_id=None, **options) -> dict:
        arguments = {"conversation_id": conversation_id} if conversation_id is not None else {}
        return _invoke("ai.chat_history", {**arguments, **options})

    def send(self, conversation_id, text, images) -> dict:
        return _invoke("ai.chat_send", {"conversation_id": conversation_id, "text": text, "images": images})

    def control(self, conversation_id, action) -> dict:
        return _invoke("ai.chat_control", {"conversation_id": conversation_id, "action": action})

    def approve(self, conversation_id, approval_id, decision) -> dict:
        return _invoke("ai.chat_approve", {"conversation_id": conversation_id, "approval_id": approval_id,
                                           "decision": decision}, approving=True)


def _undo(operation_id: str) -> dict:
    return _invoke("undo_operation", {"operation_id": operation_id})


def _api() -> ChatApi:
    from plexora.ai.harness import conversations

    return ChatApi(conversations.service(), operations=RegistryOperations(), undo=_undo, stream_s=STREAM_S,
                   held_poll_s=HELD_POLL_S)


def _dispatch(path: str):
    """This request, answered by `ChatApi`, as Flask sends it."""
    answer = _api().handle(request.method, path, query=request.args.to_dict(),
                           body=request.get_json(silent=True) or {}, headers=dict(request.headers))
    if answer.stream is not None:
        return Response(stream_with_context(answer.stream), mimetype="text/event-stream", headers=answer.headers)
    return jsonify(answer.body), answer.status


@ai_chat_bp.route("/conversations", methods=["POST"])
def start_conversation():
    return _dispatch("/conversations")


@ai_chat_bp.route("/conversations", methods=["GET"])
def list_conversations():
    return _dispatch("/conversations")


@ai_chat_bp.route("/conversations/<conversation_id>", methods=["GET"])
def get_conversation(conversation_id):
    return _dispatch(f"/conversations/{conversation_id}")


@ai_chat_bp.route("/conversations/<conversation_id>/messages", methods=["POST"])
def send_message(conversation_id):
    return _dispatch(f"/conversations/{conversation_id}/messages")


@ai_chat_bp.route("/conversations/<conversation_id>/control", methods=["POST"])
def control_conversation(conversation_id):
    return _dispatch(f"/conversations/{conversation_id}/control")


@ai_chat_bp.route("/conversations/<conversation_id>/approve", methods=["POST"])
def approve_call(conversation_id):
    return _dispatch(f"/conversations/{conversation_id}/approve")


@ai_chat_bp.route("/conversations/<conversation_id>/undo", methods=["POST"])
def undo_operation(conversation_id):
    """The panel's Undo chip: `undo_operation` on a write the conversation made,
    and a note the model reads at the start of its next turn."""
    return _dispatch(f"/conversations/{conversation_id}/undo")


@ai_chat_bp.route("/conversations/<conversation_id>/events", methods=["GET"])
def conversation_events(conversation_id):
    return _dispatch(f"/conversations/{conversation_id}/events")
