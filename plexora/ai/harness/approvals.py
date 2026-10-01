"""Human in the loop: a call that writes the user's source file or cannot be
undone waits for the user.

The permission classes decide, exactly as `policy.check` spells them:

    read                 runs
    reversible_write     runs; its receipt's operation_id goes to the panel as an Undo chip
    source_file_write    waits for an approval
    destructive          waits for an approval

`ApprovalGate.ask` writes a pending approval into the conversation's
`control.json` (`approvals.<id>`, and `paused: true, paused_by: "approval"`),
announces `approval_requested`, and waits for the record to change. The
panel's Approve / Deny posts to `/ai/v1/conversations/<id>/approve`, which
calls `decide`. Approve runs THAT one call with the policy flag it needs
(`allow_source_writes` or `allow_destructive`) and `confirm: true`; Deny, a
stop or an expiry return an error tool_result ("the user declined") so the
model knows not to try another way round. Sub-agents share their parent's gate,
so one sub-agent's approval pauses the whole conversation.
"""

from __future__ import annotations

import dataclasses
import time
import uuid

from plexora.agent.errors import AgentError

PENDING = "pending"
DECIDED = ("approved", "denied", "stopped", "expired")
DECLINED = "The user declined this call. Do not try to reach the same result another way; say what " \
           "you would have done and continue without it."


def new_approval_id() -> str:
    return f"apr_{uuid.uuid4().hex[:10]}"


def elevated(policy, permission: str):
    """`policy` with the one flag an approved call of `permission` needs."""
    if permission == "source_file_write":
        return dataclasses.replace(policy, allow_source_writes=True)
    if permission == "destructive":
        return dataclasses.replace(policy, allow_destructive=True)
    return policy


def confirmed(capability, arguments: dict) -> dict:
    """The arguments with `confirm: true` when the capability takes one."""
    fields = getattr(capability.input_model, "model_fields", {})
    if "confirm" in fields:
        return {**arguments, "confirm": True}
    return dict(arguments)


def _pending(control: dict) -> list:
    return [a for a in (control.get("approvals") or {}).values() if a.get("status") == PENDING]


def decide(store, conversation_id: str, approval_id: str, approve: bool, *, by: str = "viewer") -> dict:
    """Record the user's answer. Raises `invalid_input` for an unknown approval
    and `conflict` for one already decided."""
    with store.lock(conversation_id):
        control = store.control(conversation_id)
        approvals = dict(control.get("approvals") or {})
        approval = approvals.get(approval_id)
        if approval is None:
            raise AgentError("invalid_input", f"no approval {approval_id!r} in this conversation")
        if approval.get("status") != PENDING:
            raise AgentError("conflict", f"{approval_id} was already {approval.get('status')}",
                             detail={"status": approval.get("status")})
        approval = {**approval, "status": "approved" if approve else "denied", "decided_by": by,
                    "decided_at": time.time()}
        approvals[approval_id] = approval
        changes = {"approvals": approvals}
        others = [a for a in approvals.values() if a.get("status") == PENDING]
        if not others and control.get("paused_by") == "approval":
            changes.update(paused=False, paused_by=None)
        store.set_control(conversation_id, **changes)
    return approval


class ApprovalGate:
    def __init__(self, store, conversation_id: str, *, poll_s: float = 0.1, timeout_s: float = 3600.0):
        self.store = store
        self.conversation_id = conversation_id
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    def request(self, *, tool: str, capability, arguments: dict, agent: str | None = None) -> dict:
        approval = {"approval_id": new_approval_id(), "tool": tool, "capability": capability.name,
                    "permission": capability.permission, "purpose": capability.purpose[:300],
                    "arguments": arguments, "agent": agent, "status": PENDING, "requested_at": time.time()}
        with self.store.lock(self.conversation_id):
            control = self.store.control(self.conversation_id)
            approvals = dict(control.get("approvals") or {})
            approvals[approval["approval_id"]] = approval
            self.store.set_control(self.conversation_id, approvals=approvals, paused=True,
                                   paused_by="approval")
        return approval

    def wait(self, approval_id: str) -> str:
        deadline = time.monotonic() + self.timeout_s
        while True:
            control = self.store.control(self.conversation_id)
            approval = (control.get("approvals") or {}).get(approval_id) or {}
            status = approval.get("status")
            if status in DECIDED:
                return status
            if control.get("stopped"):
                self._close(approval_id, "stopped")
                return "stopped"
            if time.monotonic() >= deadline:
                self._close(approval_id, "expired")
                return "expired"
            time.sleep(self.poll_s)

    def _close(self, approval_id: str, status: str) -> None:
        try:
            with self.store.lock(self.conversation_id):
                control = self.store.control(self.conversation_id)
                approvals = dict(control.get("approvals") or {})
                if approval_id in approvals and approvals[approval_id].get("status") == PENDING:
                    approvals[approval_id] = {**approvals[approval_id], "status": status,
                                              "decided_at": time.time()}
                changes = {"approvals": approvals}
                if not [a for a in approvals.values() if a.get("status") == PENDING] \
                        and control.get("paused_by") == "approval":
                    changes.update(paused=False, paused_by=None)
                self.store.set_control(self.conversation_id, **changes)
        except OSError:
            pass

    def ask(self, *, tool: str, capability, arguments: dict, emit, agent: str | None = None) -> tuple:
        """`(status, approval)`: request, announce through `emit`, and wait."""
        approval = self.request(tool=tool, capability=capability, arguments=arguments, agent=agent)
        emit({"event": "approval_requested", **{k: approval[k] for k in (
            "approval_id", "tool", "capability", "permission", "purpose", "arguments", "agent")}})
        status = self.wait(approval["approval_id"])
        emit({"event": "approval_decided", "approval_id": approval["approval_id"], "status": status,
              "agent": agent})
        return status, approval
