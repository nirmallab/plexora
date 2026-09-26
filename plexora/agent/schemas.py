"""The typed shapes the agent layer speaks in.

Pydantic, with unknown fields refused: an agent that misspells an argument is
told so rather than having it silently ignored, which is the failure that looks
like the tool worked. The MCP adapter (plexora/mcp) builds each tool's input
schema from these models, so a field's description here is what the agent
reads.
"""

from __future__ import annotations

import functools
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The version of these shapes. Bumped when a field changes meaning, never for
#: an addition.
SCHEMA_VERSION = "1"


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectInput(AgentModel):
    project: str = Field(description="Project name, as `list_projects` returns it.")


class NoInput(AgentModel):
    pass


@functools.lru_cache(maxsize=1)
def plexora_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover
        return "unknown"
    try:
        return version("plexora")
    except PackageNotFoundError:
        return "source"


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
