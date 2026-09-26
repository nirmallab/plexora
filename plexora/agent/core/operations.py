"""Operations the audit log already knows about: undoing one, and reporting them.

Every write's receipt carries an `undo_hint` -- the call that would put things
back. `undo_operation` is what consumes it: it finds the receipted operation
in the audit log, checks the state has not moved on since, and replays the
hint through `invoke`, so the reversal is validated, permission-checked,
receipted and audited exactly like any other call. It never guesses: an
operation with no hint, one that was not reversible, one already undone, or
one whose store has changed since are all refused, with the reason.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

#: How to read a store's current revision, by the receipt's `persistent_state`:
#: (capability, result key). Named, not imported -- core knows nothing of the
#: plugins that own these stores, and a store with no reader here is still
#: protected by the revision its own undo hint carries.
REVISION_READERS = {
    "plugin_store:gating": ("gating.get_all", "revision"),
    "plugin_store:gating_provenance": ("gating.provenance", "revision"),
    "plugin_store:roi": ("roi.list", "revision"),
}

#: Arguments that pin a call to the revision it was made against.
_REVISION_ARGUMENTS = ("expected_revision", "base_revision")


class UndoInput(AgentModel):
    operation_id: str = Field(description="The receipt's operation_id, as a write returned it "
                                          "or the audit log records it.")


def _replay(call, tool, arguments, undo_of):
    from plexora.agent import registry

    return registry.invoke(call.session, tool, arguments, policy=call.policy,
                           audit=call.audit, link=call.link, notify=call.notify,
                           operation_id=f"{call.operation_id}.replay", undo_of=undo_of)


def _current_revision(call, state, project):
    """The store's revision now, or None when this store has no reader."""
    reader = REVISION_READERS.get(state)
    if reader is None or project is None:
        return None
    from plexora.agent import registry

    name, key = reader
    try:
        registry.get(name)
    except AgentError:
        return None
    answer = registry.invoke(call.session, name, {"project": project}, policy=call.policy,
                             audit=call.audit, link=call.link)
    if not answer["ok"]:
        error = answer["error"]
        raise AgentError(error["code"], error["message"], detail=error.get("detail"),
                         retryable=error.get("retryable", False))
    return answer["result"].get(key)


def undo_operation(call, inp):
    line = call.audit.find(inp.operation_id)
    if line is None:
        raise AgentError("invalid_input",
                         f"no completed operation {inp.operation_id!r} in the audit log",
                         detail={"audit_log": str(call.audit.path)})
    receipt = line.get("receipt") or {}
    hint = receipt.get("undo_hint")
    if not hint or not hint.get("tool"):
        raise AgentError("invalid_input",
                         f"{line.get('capability')} left no undo hint; there is nothing to "
                         "replay", detail={"operation_id": inp.operation_id})
    if not receipt.get("reversible", True):
        raise AgentError(
            "invalid_input",
            f"{line.get('capability')} is not reversible: its hint only approximates the "
            "state before it (a recreated region gets a new id), so Plexora will not call "
            "it an undo",
            detail={"operation_id": inp.operation_id, "undo_hint": hint,
                    "hint": "if the user wants it, call the hint's tool yourself"})
    if not receipt.get("changed", True):
        raise AgentError("invalid_input", f"{inp.operation_id} changed nothing",
                         detail={"operation_id": inp.operation_id})
    undone = call.audit.undo_of(inp.operation_id)
    if undone is not None:
        raise AgentError("conflict", f"{inp.operation_id} was already undone",
                         detail={"undone_by": undone.get("operation_id")}, retryable=False)

    project = line.get("project")
    state = receipt.get("persistent_state")
    current = _current_revision(call, state, project)
    expected = receipt.get("revision_after")
    if current is not None and expected is not None and str(current) != str(expected):
        raise AgentError(
            "conflict",
            f"{state} has changed since {inp.operation_id} (revision {expected} then, "
            f"{current} now); undoing it would overwrite the later change",
            detail={"revision_after": expected, "current_revision": current},
            retryable=False)

    undo_of = {"operation_id": inp.operation_id, "undo_hint": hint}
    replayed = _replay(call, hint["tool"], dict(hint.get("arguments") or {}), undo_of)
    if not replayed["ok"]:
        error = replayed["error"]
        raise AgentError(error["code"], f"the undo hint failed: {error['message']}",
                         detail={"undo_hint": hint, "error": error},
                         retryable=error.get("retryable", False))
    replay_receipt = (replayed["result"] or {}).get("receipt") or {}

    # Redo: the original call again, pinned to the revision the undo left.
    redo_arguments = dict(line.get("arguments") or {})
    for key in _REVISION_ARGUMENTS:
        if key in redo_arguments or key in (hint.get("arguments") or {}):
            redo_arguments[key] = replay_receipt.get("revision_after")
    redo_arguments = {k: v for k, v in redo_arguments.items() if v is not None}

    from plexora.agent.receipts import make_receipt

    call.project_name = project
    receipt_out = make_receipt(
        call, changed=bool(replay_receipt.get("changed", True)),
        before=receipt.get("after"), after=receipt.get("before"),
        revision_before=replay_receipt.get("revision_before"),
        revision_after=replay_receipt.get("revision_after"),
        persistent_state=state or "none",
        undo_hint={"tool": line.get("capability"), "arguments": redo_arguments},
        extra={"undo_of": inp.operation_id,
               "replayed": {"operation_id": replayed.get("operation_id"),
                            "tool": hint["tool"]}})
    return {"receipt": receipt_out.model_dump(mode="json"),
            "undone": inp.operation_id,
            "replayed": {"tool": hint["tool"], "operation_id": replayed.get("operation_id"),
                         "receipt": replay_receipt}}


class ReportInput(AgentModel):
    since: str | None = Field(None, description="ISO timestamp (or date) to start from; "
                                                "default: the whole log.")
    operation_ids: list[str] | None = Field(None, description="Only these operations, with "
                                            "their undos and per-image parts.")
    project: str | None = Field(None, description="Only this project's operations.")
    artifact_ids: list[str] | None = Field(None, description="Artifacts to cite as evidence "
                                           "for their project's operations.")
    format: Literal["md", "html"] = Field("md", description="md links the pictures; html "
                                          "embeds them in one file.")


def session_report(call, inp):
    from plexora.agent import report
    from plexora.agent.limits import MAX_TOOL_CHARS

    built = report.build(call.audit, since=inp.since, operation_ids=inp.operation_ids,
                         project=inp.project, artifact_ids=inp.artifact_ids or ())
    path = report.write(built, fmt=inp.format)
    out = {"path": str(path), "format": inp.format,
           "operations": len(built["operations"]), "artifacts": len(built["artifacts"]),
           "counts": built["counts"]}
    if inp.format == "md":
        text = path.read_text(encoding="utf-8")
        if len(text) < MAX_TOOL_CHARS // 2:
            out["text"] = text
    return out


def capabilities():
    return [
        Capability(
            name="operation.undo", tool_name="undo_operation", owner="core",
            purpose="Undo one earlier write by its operation_id: replays the receipt's undo "
                    "hint, receipted like any write. Refused when the state has changed "
                    "since, when it was already undone, or when the operation was not "
                    "reversible.",
            permission="reversible_write", input_model=UndoInput, handler=undo_operation,
            reads=("audit",), writes=("plugin_store",),
            tags=("undo", "revert", "operation", "audit", "history"),
        ),
        Capability(
            name="operation.report", tool_name="session_report", owner="core",
            purpose="A report of what the agent did -- each operation with its arguments, "
                    "before/after numbers, revisions, undos and the renders it rested on -- "
                    "written as Markdown or self-contained HTML. The provenance a methods "
                    "section needs.",
            permission="read", input_model=ReportInput, handler=session_report,
            reads=("audit", "artifacts"), egress="rendered_pixels",
            tags=("report", "provenance", "methods", "audit", "history", "summary"),
        ),
    ]
