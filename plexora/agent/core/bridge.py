"""The shared protocol's tools, as Plexora capabilities: `bridge_*` and `workspace_*`.

SCIMAP Pro and Plexora share one dataset through spatialbridge. Each offers
the same thirteen bridge tools on its own MCP server, so an agent holding only
one of them reaches the other: `bridge_route` says which application should do
a task, `bridge_handoff` hands it over and brings the result back,
`bridge_collect` returns gates, regions, QC or a selection as the protocol's
objects. SCIMAP Pro mounts them with `spatialbridge.tools.mount`; Plexora's
MCP tools are all capabilities, so here each is a capability whose handler
calls the protocol's `BridgeTools` over Plexora's own adapter
(`plexora/agent/bridge_provider.py`). That way the in-app chat, the HTTP
capability route and an outside agent reach them the same way, with the same
receipts, audit lines and policy.

Present whether or not the optional package is installed, so skills can name
them and `validate_scope` can route through them: without it each answers
`capability_unavailable` with the install line. The one exception is
`bridge_dataset_changed`, which needs nothing from the package -- a peer that
cannot reach the HTTP event route can still say "the table changed" over MCP.

Origins are not laundered here: a bridge tool called by an outside agent runs
its nested calls as that agent (`mcp`); only the transports that establish the
bridge origin (`plexora mcp serve --origin bridge` with its nonce, the HTTP
route with a `bridge` token or on loopback) give it.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

OWNER = "bridge"
PROTOCOL = "1.0"
INSTALL_HINT = "pip install 'plexora[bridge]'"
TAGS = ("bridge", "handoff", "peer", "workspace", "spatialbridge")


def available() -> bool:
    """Whether the protocol package can be imported here."""
    import importlib.util

    try:
        return importlib.util.find_spec("spatialbridge") is not None
    except (ImportError, ValueError):
        return False


def describe() -> dict:
    """The `bridge` block of `server_info` and `servers.json`: protocol,
    provider, whether the package is here and its version. No tokens."""
    out = {"protocol": PROTOCOL, "provider": "plexora", "available": available(),
           "capabilities_path": "agent/v1/capabilities"}
    if out["available"]:
        try:
            import spatialbridge

            out["protocol"] = spatialbridge.PROTOCOL
            out["version"] = spatialbridge.__version__
        except Exception:  # an install broken half-way still lets server_info answer
            out["available"] = False
    if not out["available"]:
        out["install_hint"] = INSTALL_HINT
    return out


def _unavailable():
    return AgentError(
        "capability_unavailable",
        "the bridge to other spatial tools (SCIMAP Pro) needs the spatialbridge package, "
        "which is not installed",
        detail={"hint": INSTALL_HINT})


def provider_for(call):
    """Plexora's adapter for this call: its session, policy, audit, link,
    notifier and origin."""
    from plexora.agent.registry import CALL_ORIGIN

    try:
        from plexora.agent.bridge_provider import PlexoraProvider
    except ImportError as exc:
        raise _unavailable() from exc
    return PlexoraProvider(call.session, policy=call.policy, audit=call.audit,
                           link=call.link, notify=call.notify,
                           origin=call.extras.get("origin") or CALL_ORIGIN.get())


def _tools(call):
    from spatialbridge.tools import BridgeTools

    return BridgeTools(provider_for(call), read_only=not call.policy.allow_writes)


def _run(tool):
    """A handler calling `BridgeTools.<tool>` with the validated arguments it
    takes (`expected_revision`, which the protocol's MCP transport adds to
    every bridge call, is dropped where the method has no use for it)."""

    def handler(call, inp):
        tools = _tools(call)
        method = getattr(tools, tool)
        accepted = set(inspect.signature(method).parameters)
        arguments = {k: v for k, v in inp.model_dump().items() if k in accepted}
        return tools.call(tool, arguments)

    handler.__name__ = f"bridge_{tool}"
    return handler


class _BridgeInput(AgentModel):
    expected_revision: int | None = Field(None, description="The workspace revision the "
                                          "caller last saw; a write against an older one "
                                          "is refused.")


class InfoInput(_BridgeInput):
    pass


class PeersInput(_BridgeInput):
    provider: str | None = Field(None, description="Only this provider, e.g. 'scimappro'.")


class CapabilitiesInput(_BridgeInput):
    role: str | None = Field(None, description="Only capabilities with this role, e.g. "
                             "'spatial.neighborhood', 'gate.auto', 'image.inspect'.")
    needs: str | None = Field(None, description="Only those needing this: image, mask, "
                              "table, viewer, coords, human.")
    provider: str | None = Field(None, description="Only this provider's.")
    include_peers: bool = Field(True, description="Ask every reachable peer too (never "
                                "starts one unless spawn).")
    spawn: bool = Field(False, description="Start a peer's MCP server to ask it.")


class RouteInput(_BridgeInput):
    task: str | None = Field(None, description="The user's words for the task.")
    role: str | None = Field(None, description="Or a role from the shared vocabulary.")
    visual_check: bool = Field(False, description="The result must be checked by eye.")
    spawn: bool = False


class StatusInput(_BridgeInput):
    job_id: str | None = Field(None, description="A job's id, from a bridge call.")
    workspace_id: str | None = None
    table: str | None = Field(None, description="Path of the shared table.")


class WorkspaceGetInput(_BridgeInput):
    workspace_id: str | None = None
    table: str | None = Field(None, description="Path of the shared table.")
    since: int | None = Field(None, description="Also every change after this revision.")
    observe: bool = Field(True, description="Look for writes that bypassed the bridge.")


class CollectInput(_BridgeInput):
    kind: str = Field(description="gates, regions, qc or selection.")
    image_id: str | None = None
    operation_id: str | None = None
    arguments: dict[str, Any] | None = Field(None, description="`project` (from bridge_bind) "
                                             "and, for a selection, `name`.")


class EventInput(_BridgeInput):
    event: dict[str, Any] = Field(description="{kind, workspace_id, revision, sections, "
                                  "producer, operation_id?, execution_id?}")


class BindInput(_BridgeInput):
    table: str = Field(description="Path of the shared table on this machine.")
    workspace_id: str | None = None
    image_id: str | None = None
    image: str | None = Field(None, description="The image, the first time this table "
                              "is shown.")
    mask: str | None = None
    roles: dict[str, Any] | None = Field(None, description="cell_id, x, y, image_id, "
                                         "celltype columns.")
    table_name: str = ""


class InvokeInput(_BridgeInput):
    to: str = Field(description="The provider to run it on, e.g. 'scimappro'.")
    capability: str = Field(description="Its tool name or capability id.")
    arguments: dict[str, Any] | None = None
    wait: bool = Field(True, description="Wait for a job to finish.")


class HandoffInput(_BridgeInput):
    to: str = Field(description="The provider to hand the work to.")
    capability: str = Field(description="Its tool name or capability id.")
    arguments: dict[str, Any] | None = None
    context: dict[str, Any] | None = Field(None, description="{image_id, image?, mask?, "
                                           "selection?, regions?, color_by?, highlight?}")
    returns: list[str] | None = Field(None, description="What to bring back: gates, "
                                      "regions, qc, selection, cell_labels, result.")
    dataset: str | None = Field(None, description="This side's dataset: a Plexora project.")


class PublishInput(_BridgeInput):
    sections: list[str] = Field(description="What was written: obs:<column>, uns:<key>, X...")
    workspace_id: str | None = None
    table: str | None = None
    kind: str = "dataset.changed"
    operation_id: str | None = None
    execution_id: str | None = None


class WorkspaceSetInput(_BridgeInput):
    workspace_id: str | None = None
    table: str | None = None
    active: dict[str, Any] | None = Field(None, description="{image_id, project, selection, "
                                          "color_by}")


class DatasetChangedInput(AgentModel):
    project: str = Field(description="The project whose table changed.")
    sections: list[str] = Field(default_factory=list, description="What changed: "
                                "obs:<column>, uns:<key>, X, layers:<name>, var, file.")
    revision: int | None = None
    producer: str | None = None
    operation_id: str | None = None
    execution_id: str | None = None


def tabs_notifier(notify):
    """How open tabs are told from this process: straight onto the session
    registry when this process is the server -- its caches were just refreshed,
    and going through `api.notify_viewers` would refresh them twice -- else
    `notify`, the attached server's link, whose own hook refreshes its caches."""
    from plexora.agent.registry import _serving_in_process

    if _serving_in_process():
        from plexora.server.models import viewer_sessions

        def publish(project, plugin, kind, payload):
            return viewer_sessions.publish(project, plugin, kind, payload, origin="server")
        return publish
    return notify


def dataset_changed(call, inp):
    """A table changed underneath a project: refresh what depends on it and
    tell the open tabs. No protocol package needed."""
    from plexora.server.models import dataset_events

    call.session.project(inp.project)
    payload = inp.model_dump(exclude={"project"}, exclude_none=True)
    answer = dataset_events.on_dataset_changed(inp.project, payload,
                                               notify=tabs_notifier(call.notify))
    call.session.invalidate(inp.project)
    return answer


def _guarded(handler):
    def run(call, inp):
        if not available():
            raise _unavailable()
        return handler(call, inp)

    run.__name__ = handler.__name__
    return run


#: (tool, input model, permission, purpose). The tool names are the protocol's
#: own and must not change; the capability is `bridge.<tool without prefix>`.
_TOOLS = (
    ("bridge_info", InfoInput, "read",
     "What this side of the bridge is: provider, versions, protocol, where shared "
     "workspaces live, which peers are connected."),
    ("bridge_peers", PeersInput, "read",
     "Other spatial tools (SCIMAP Pro) running now that Plexora can reach, without tokens."),
    ("bridge_capabilities", CapabilitiesInput, "read",
     "The merged catalogue: Plexora's capabilities and every reachable peer's, each with "
     "the roles it serves, what it needs and produces. Filter by role, need or provider."),
    ("bridge_route", RouteInput, "read",
     "Which application should run a task (the user's words, or a role): ranked candidates "
     "with the reason and whether each is reachable. Pixels and a person's eye go to the "
     "viewer; the cell table across samples and statistics go to the analysis side."),
    ("bridge_status", StatusInput, "read",
     "A bridge job's state, or the shared workspace's summary: revision, active image, "
     "bound projects, the lease."),
    ("workspace_get", WorkspaceGetInput, "read",
     "The shared workspace record of a table both applications hold: dataset, revision, "
     "active image, who wrote which section, the lease; writes that bypassed the bridge "
     "are reported as external_change."),
    ("bridge_collect", CollectInput, "read",
     "One result object from Plexora, in the protocol's shape: gates, regions (ROI "
     "GeoJSON), qc (excluded cells, reasons, regions, summary) or a stored selection."),
    ("bridge_event", EventInput, "read",
     "A peer says the shared table changed: every project showing it re-reads its "
     "columns and its open tabs are told."),
    ("bridge_bind", BindInput, "reversible_write",
     "Make Plexora show a shared table: the project that reads it, found, or registered "
     "from the image the first time."),
    ("bridge_invoke", InvokeInput, "reversible_write",
     "Run one capability in another application (SCIMAP Pro) by its tool name, waiting "
     "for a job; refusals come back with the peer's own reason."),
    ("bridge_handoff", HandoffInput, "reversible_write",
     "Hand work to another application on this dataset and bring the result back: make "
     "sure it holds the table, run the capability, collect what was asked for."),
    ("bridge_publish", PublishInput, "reversible_write",
     "Record that Plexora wrote sections of the shared table, bump the workspace "
     "revision, and tell running peers."),
    ("workspace_set", WorkspaceSetInput, "reversible_write",
     "Change the shared workspace's active image, project, selection or colour-by column."),
)


def capabilities():
    out = []
    for tool, model, permission, purpose in _TOOLS:
        name = "bridge." + tool.removeprefix("bridge_")
        out.append(Capability(
            name=name, tool_name=tool, owner=OWNER, purpose=purpose, permission=permission,
            input_model=model, handler=_guarded(_run(tool)), egress="aggregates",
            reversible=permission == "read", tags=TAGS))
    out.append(Capability(
        name="bridge.dataset_changed", tool_name="bridge_dataset_changed", owner=OWNER,
        purpose="Say a project's cell table changed underneath it (sections: obs:<column>, "
                "uns:<key>, X...): Plexora re-reads its columns and caches, and the open "
                "tabs pick the change up. Works without the bridge package.",
        permission="read", input_model=DatasetChangedInput, handler=dataset_changed,
        tags=TAGS))
    return out
