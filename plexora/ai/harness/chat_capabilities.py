"""`ai.chat_*`: the conversational agent as capabilities.

The HTTP blueprint (plexora/server/routes/ai_chat_routes.py) and the CLI call
these through `registry.invoke`, like every other surface, so validation, the
licence check and the audit log are the same wherever a conversation is
driven from. They write only the conversation's own state; every tool the
model calls is invoked with the CALLER's policy, so a read-scoped token's
conversation can only read, and a source-file write or a delete still waits
for the user's approval (`approvals.py`).

`ai.chat_approve` is the user's hand: approving a call of a class the
caller's policy could not make itself is refused. The panel's route answers
for the person at the viewer and passes the policy that lets them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

OWNER = "ai"
ENTITLEMENT = "ai:chat"
TAGS = ("ai", "chat", "conversation", "assistant")


class StartInput(AgentModel):
    title: str | None = Field(None, max_length=200, description="A title for the conversation list.")
    viewer: bool = Field(False, description="Offer the viewer-control tools (an open viewer is attached).")


class ImageInput(AgentModel):
    data: str = Field(description="Base64 (or a data: URL) of a PNG, JPEG or WebP image.")
    format: Literal["png", "jpeg", "jpg", "webp"] | None = None


class SendInput(AgentModel):
    conversation_id: str
    text: str = Field("", max_length=20_000, description="The user's message.")
    images: list[ImageInput] = Field(default_factory=list, max_length=4)
    wait_s: float = Field(0, ge=0, le=60, description="Wait this long for the turn to finish; 0 returns "
                                                      "at once (read events with ai_chat_history).")


class ApproveInput(AgentModel):
    conversation_id: str
    approval_id: str
    decision: Literal["approve", "deny"]


class ControlInput(AgentModel):
    conversation_id: str
    action: Literal["pause", "resume", "stop"]


class HistoryInput(AgentModel):
    conversation_id: str | None = Field(None, description="Omit to list the conversations.")
    after: int = Field(0, ge=0, description="Only events after this seq.")
    wait_s: float = Field(0, ge=0, le=25, description="Hold the poll this long for a new event.")
    limit: int = Field(500, ge=1, le=5000)


def _service():
    from plexora.ai.harness import conversations

    return conversations.service()


def chat_start(call, inp):
    return _service().start(call.policy, title=inp.title, viewer=inp.viewer)


def chat_send(call, inp):
    service = _service()
    images = [{"data": i.data, "format": i.format} for i in inp.images]
    if not inp.text.strip() and not images:
        raise AgentError("invalid_input", "send some text or an image")
    sent = service.send(inp.conversation_id, inp.text, images, policy=call.policy)
    if inp.wait_s:
        service.wait(inp.conversation_id, inp.wait_s)
    polled = service.events(inp.conversation_id, sent["after"])
    return {**sent, **polled}


def chat_approve(call, inp):
    service = _service()
    control = service.store.control(inp.conversation_id) if service.store.exists(inp.conversation_id) else {}
    pending = (control.get("approvals") or {}).get(inp.approval_id) or {}
    if inp.decision == "approve":
        permission = pending.get("permission")
        if permission == "destructive" and not call.policy.allow_destructive:
            raise AgentError("permission_required", "approving a call that cannot be undone is the user's "
                             "decision, made in Plexora's chat panel",
                             detail={"permission": "destructive"})
        if permission == "source_file_write" and not call.policy.allow_source_writes:
            raise AgentError("permission_required", "approving a write into the user's source file is the "
                             "user's decision, made in Plexora's chat panel",
                             detail={"permission": "source_file_write"})
    approval = service.approve(inp.conversation_id, inp.approval_id, inp.decision == "approve",
                               by=call.policy.principal or "caller")
    return {"approval": approval}


def chat_control(call, inp):
    return {"control": _service().control(inp.conversation_id, inp.action)}


def chat_history(call, inp):
    return _service().history(inp.conversation_id, after=inp.after, limit=inp.limit, wait_s=inp.wait_s)


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, version="1", entitlement=ENTITLEMENT, remote_safe=False, **kwargs)

    return [
        cap(name="ai.chat_start", tool_name="ai_chat_start",
            purpose="Start a Plexora AI conversation: an assistant inside Plexora that answers questions "
                    "about the user's projects by calling Plexora's tools, billed in Plexora AI credits.",
            permission="reversible_write", input_model=StartInput, handler=chat_start,
            writes=("conversation",)),
        cap(name="ai.chat_send", tool_name="ai_chat_send",
            purpose="Send the user's message to a Plexora AI conversation; the answer streams as events "
                    "(text, tool calls, approvals, usage).",
            permission="reversible_write", input_model=SendInput, handler=chat_send,
            writes=("conversation",)),
        cap(name="ai.chat_approve", tool_name="ai_chat_approve",
            purpose="Answer a Plexora AI conversation's request to run a call that writes a source file or "
                    "cannot be undone: approve runs that one call, deny declines it.",
            permission="reversible_write", input_model=ApproveInput, handler=chat_approve,
            writes=("conversation",)),
        cap(name="ai.chat_control", tool_name="ai_chat_control",
            purpose="Pause, resume or stop a Plexora AI conversation.",
            permission="reversible_write", input_model=ControlInput, handler=chat_control,
            writes=("conversation",)),
        cap(name="ai.chat_history", tool_name="ai_chat_history",
            purpose="A Plexora AI conversation's events after a cursor (held poll), or the list of "
                    "conversations.",
            permission="read", input_model=HistoryInput, handler=chat_history, reads=("conversation",)),
    ]
