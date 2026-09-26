"""Every capability an agent can call, and the one path that calls them.

The counterpart of `@table_operation` (plexora/server/providers/operations.py)
one level up: a name-keyed registry, collision-checked, JSON in and JSON out.
A capability is a typed operation with a purpose an agent can read, a
permission class, the requirements a project must meet, and a handler. Core
registers its own; a plugin declares its own through
`Plugin.capabilities_factory`, so a new modality brings its capabilities with
it and nothing here has to know it exists.

Every surface calls through `invoke` -- the MCP adapter, the tests, a future
HTTP transport -- so validation, permissions, preconditions, receipts and the
audit log happen in one place and cannot be skipped by a transport that forgot.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from plexora.agent.errors import AgentError, as_agent_error
from plexora.agent.policy import PERMISSIONS, EGRESS, Policy

EXECUTIONS = ("immediate", "job")


@dataclass(frozen=True)
class Capability:
    name: str
    owner: str
    purpose: str
    permission: str
    input_model: type
    handler: Callable
    output_model: type | None = None
    #: The MCP tool name. snake_case, defaulting to the name with dots replaced.
    tool_name: str = ""
    #: What a project must have. A plugin's own `Requires`, usually.
    requires: Any = None
    reads: tuple = ()
    writes: tuple = ()
    reversible: bool = True
    source_file_write: bool = False
    visual_output: bool = False
    remote_safe: bool = True
    viewer_required: bool = False
    persistent: bool = False
    execution: str = "immediate"
    egress: str = "metadata"
    tags: tuple = ()
    version: str = "1"

    def __post_init__(self):
        if self.permission not in PERMISSIONS:
            raise ValueError(f"{self.name}: unknown permission {self.permission!r}")
        if self.egress not in EGRESS:
            raise ValueError(f"{self.name}: unknown egress {self.egress!r}")
        if self.execution not in EXECUTIONS:
            raise ValueError(f"{self.name}: unknown execution {self.execution!r}")
        if not self.tool_name:
            object.__setattr__(self, "tool_name", self.name.replace(".", "_"))

    @property
    def takes_project(self) -> bool:
        return "project" in getattr(self.input_model, "model_fields", {})

    def describe(self) -> dict:
        schema = self.input_model.model_json_schema() if self.input_model else {}
        return {
            "name": self.name, "tool": self.tool_name, "owner": self.owner,
            "purpose": self.purpose, "permission": self.permission,
            "reversible": self.reversible, "source_file_write": self.source_file_write,
            "visual_output": self.visual_output, "viewer_required": self.viewer_required,
            "remote_safe": self.remote_safe, "execution": self.execution,
            "egress": self.egress, "reads": list(self.reads), "writes": list(self.writes),
            "tags": list(self.tags), "version": self.version, "input_schema": schema,
        }


@dataclass
class Call:
    """What a handler is given besides its validated input."""

    capability: Capability
    session: Any
    policy: Policy
    operation_id: str
    audit: Any
    arguments: dict
    project_name: str | None = None
    #: A running Plexora server, when this process is attached to one.
    link: Any = None
    #: `notify(project, plugin, kind, payload) -> bool`: tells an open viewer
    #: that state changed. None when there is nobody to tell.
    notify: Callable | None = None
    receipted: bool = False
    extras: dict = field(default_factory=dict)
    _data: Any = None

    @property
    def data(self):
        """The project's provider-backed handles, built on first use.

        Lazy on purpose: asking where a project's table is must not be the
        thing that tries to read it, and a node that is asleep is exactly when
        an agent asks.
        """
        if self._data is None and self.project_name is not None:
            self._data = self.session.data(self.project_name)
        return self._data


_REGISTRY: dict = {}
_LOCK = threading.Lock()


def register(capability: Capability) -> Capability:
    """Add a capability. Re-registering the same name from the same owner
    replaces it (a module imported twice); from another owner it is refused."""
    with _LOCK:
        existing = _REGISTRY.get(capability.name)
        if existing is not None and existing.owner != capability.owner:
            raise ValueError(
                f"capability {capability.name!r} is already registered by "
                f"{existing.owner!r}; {capability.owner!r} cannot take it")
        clash = next((cap for cap in _REGISTRY.values()
                      if cap.tool_name == capability.tool_name
                      and cap.name != capability.name), None)
        if clash is not None:
            raise ValueError(f"tool name {capability.tool_name!r} is already used by "
                             f"{clash.name!r}")
        _REGISTRY[capability.name] = capability
        return capability


def get(name: str) -> Capability:
    capability = _REGISTRY.get(name)
    if capability is None:
        capability = next((cap for cap in _REGISTRY.values() if cap.tool_name == name), None)
    if capability is None:
        raise AgentError("unknown_capability", f"no capability named {name!r}",
                         detail={"known": sorted(_REGISTRY)})
    return capability


def all_capabilities() -> list:
    return sorted(_REGISTRY.values(), key=lambda cap: (cap.owner != "core", cap.owner, cap.name))


def describe(names=None) -> list:
    return [cap.describe() for cap in all_capabilities()
            if names is None or cap.name in names or cap.tool_name in names]


def _reset_for_tests():
    with _LOCK:
        _REGISTRY.clear()
    global _CORE_REGISTERED
    _CORE_REGISTERED = False


_CORE_REGISTERED = False


def register_core() -> list:
    global _CORE_REGISTERED
    from plexora.agent.core import core_capabilities

    names = []
    for capability in core_capabilities():
        register(capability)
        names.append(capability.name)
    _CORE_REGISTERED = True
    return names


def _plugin_capabilities(plugin) -> list:
    loader = getattr(plugin, "load_capabilities", None)
    if loader is None:
        return []
    return list(loader() or [])


def discover(names=None, *, loader=None) -> list:
    """Register core's capabilities and those of the requested plugins.

    `names` works like PLEXORA_PLUGINS: None means every plugin this process can
    see (or PLEXORA_PLUGINS itself when that is set), a list means exactly
    those. Only the plugins asked for are imported -- a core-only agent does not
    pay for gating's scipy, which is the same promise plugin discovery makes.
    A plugin whose capabilities fail to load is reported and skipped.
    """
    from plexora.server import plugins as plugin_registry

    registered = register_core()
    loader = loader or plugin_registry.load
    if names is None:
        names = plugin_registry.requested()
    available = plugin_registry.available_names()
    wanted = available if names is None else [name for name in names if name in available]
    for name in wanted:
        plugin = loader(name)
        if plugin is None:
            continue
        try:
            for capability in _plugin_capabilities(plugin):
                register(capability)
                registered.append(capability.name)
        except Exception as exc:  # pragma: no cover - a broken third-party plugin
            print(f"WARNING: plugin {name!r} capabilities failed to load: {exc}")
    return registered


def discover_installed(app) -> list:
    """The capabilities of the plugins a running app mounted."""
    from plexora.server import plugins as plugin_registry

    return discover([plugin.name for plugin in plugin_registry.installed(app)])


# -- invocation ----------------------------------------------------------


def _check_requirements(capability, record):
    requires = capability.requires
    if requires is None:
        return
    if not requires.applies_to(record):
        raise AgentError(
            "unsupported_modality",
            f"{capability.name} does not apply to {record.name!r}: its image kind "
            f"({record.image.kind}) or layers do not fit",
            detail={"image_kind": record.image.kind,
                    "layers": [layer.id for layer in record.all_layers]})
    missing = requires.missing_from(record)
    if missing:
        raise AgentError(
            "precondition_missing",
            f"{record.name!r} is missing what {capability.name} needs",
            detail={"missing": [requirement.describe() for requirement in missing]})


def _serving_in_process() -> bool:
    """Whether this process IS a Plexora server (the agent API running inside
    it), in which case viewer commands need no link to reach it."""
    import sys

    module = sys.modules.get("plexora")
    app = getattr(module, "app", None) if module is not None else None
    return bool(app is not None and app.config.get("PLEXORA_SERVING"))


def invoke(session, name, arguments=None, *, policy=None, audit=None, link=None,
           notify=None, operation_id=None):
    """Run one capability; returns `{"ok": True, "result": ...}` or
    `{"ok": False, "error": Problem}` -- never raises for a domain failure.

    Every attempted mutation leaves an audit line, whatever became of it.
    """
    from plexora.agent.audit import AuditLog
    from plexora.agent.receipts import operation_id as new_operation_id

    policy = policy or Policy()
    audit = audit or AuditLog()
    arguments = dict(arguments or {})
    op_id = operation_id or new_operation_id()
    capability = None
    call = None
    try:
        capability = get(name)
        try:
            inp = capability.input_model.model_validate(arguments)
        except Exception as exc:
            raise as_agent_error(exc) from exc
        call = Call(capability=capability, session=session, policy=policy,
                    operation_id=op_id, audit=audit, arguments=arguments,
                    link=link, notify=notify)
        from plexora.agent import policy as policy_rules

        policy_rules.check(capability, inp, policy)
        if capability.viewer_required and link is None and not _serving_in_process():
            raise AgentError(
                "viewer_not_available",
                f"{capability.name} acts on an open Plexora viewer, and this agent is not "
                "attached to a running Plexora server",
                detail={"hint": "start Plexora (or open the notebook viewer), then restart "
                                "the MCP server or pass --server URL --token T"})
        if capability.takes_project and getattr(inp, "project", None) is not None:
            call.project_name = inp.project
            record = session.project(inp.project)
            _check_requirements(capability, record)
        result = capability.handler(call, inp)
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json")
        return {"ok": True, "result": result, "operation_id": op_id}
    except Exception as exc:
        error = as_agent_error(exc)
        if capability is not None and capability.permission != "read" and not (
                call is not None and call.receipted):
            status = {"permission_required": "refused",
                      "conflict": "conflict"}.get(error.code, "failed")
            audit.append({"status": status, "operation_id": op_id,
                          "capability": capability.name,
                          "project": arguments.get("project"),
                          "arguments": arguments, "error": error.to_problem()})
        return {"ok": False, "error": error.to_problem(), "operation_id": op_id}
