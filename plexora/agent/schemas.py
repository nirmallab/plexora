"""The typed shapes the agent layer speaks in.

Pydantic, with unknown fields refused: an agent that misspells an argument is
told so rather than having it silently ignored, which is the failure that looks
like the tool worked. The MCP adapter (plexora/mcp) builds each tool's input
schema from these models, so a field's description here is what the agent
reads.
"""

from __future__ import annotations

import functools
from typing import Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

#: The version of these shapes. Bumped when a field changes meaning, never for
#: an addition.
SCHEMA_VERSION = "1"


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _trim(text: str, limit: int) -> str:
    """`text` cut to `limit` characters: at the last sentence end in the
    second half of the limit, else the last word, then an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    end = max(head.rfind(". "), head.rfind("; "))
    if end >= limit // 2:
        return head[:end + 1]
    space = head.rfind(" ")
    return (head[:space] if space >= limit // 2 else head).rstrip(" ,;:") + "…"


def clipped(limit: int) -> BeforeValidator:
    """A free-text field that is cut to `limit` characters instead of refused.

    For prose an agent writes for the record (`notes`, a reason): a 340-
    character note is a good answer said at length, and refusing it costs a
    whole repair turn of the model. Used as `Annotated[str, clipped(300)]`
    beside `Field(max_length=300)`: the cut runs first, so the limit still
    holds and still shows in the schema."""
    return BeforeValidator(lambda value: _trim(value, limit) if isinstance(value, str) else value)


class ProjectInput(AgentModel):
    project: str = Field(description="Project name, as `list_projects` returns it.")


class NoInput(AgentModel):
    pass


#: What a `qc` field says, wherever an estimate or a sample takes one
#: (plexora/agent/cell_exclusions.py).
QC_FIELD_DESCRIPTION = (
    "Which QC failures to leave out of this estimate, sample or picture (the gate itself "
    "always applies to every cell). strict (the default): cells QC called exclude or warn, "
    "and for this marker the cells QC flagged it unreliable in; exclude: warn calls kept; "
    "off: QC ignored -- only when the user asks, or QC is suspected wrong. Inside a gating "
    "session the session's own setting applies. No effect where QC was never run.")


class QcInput(AgentModel):
    qc: Literal["strict", "exclude", "off"] | None = Field(None,
                                                           description=QC_FIELD_DESCRIPTION)


@functools.lru_cache(maxsize=1)
def plexora_version() -> str:
    from plexora.updates import current_version

    return current_version() or "source"


class Versions(AgentModel):
    plexora: str
    schema_version: str = SCHEMA_VERSION
    capability: str


def versions(capability_version: str) -> Versions:
    return Versions(plexora=plexora_version(), capability=capability_version)


class Problem(AgentModel):
    code: str
    message: str
    detail: Any = None
    retryable: bool = False


class Receipt(AgentModel):
    """What one mutation did, in terms an agent can cite and undo.

    Returned by every capability that writes anything, and appended to the
    audit log (plexora/agent/audit.py) whether or not it succeeded.
    """

    operation_id: str
    project: str
    capability: str
    capability_version: str
    changed: bool
    before: dict | None = None
    after: dict | None = None
    revision_before: str | int | None = None
    revision_after: str | int | None = None
    #: Which store changed: "plugin_store:gating", "plugin_store:roi",
    #: "config", "source_file" or "none".
    persistent_state: str
    source_file_modified: bool = False
    source_path: str | None = None
    reversible: bool = True
    #: The call that would put things back, when there is one.
    undo_hint: dict | None = None
    viewer_notified: bool = False
    timestamp: str
    audit_path: str | None = None
    versions: Versions
