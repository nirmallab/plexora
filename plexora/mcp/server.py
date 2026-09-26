"""The MCP server: one tool per capability, a few of its own, and resources.

Runs in its own process (`plexora mcp serve`, launched by the agent's client
over stdio) with the agent layer in-process -- no Plexora web server is needed
for any headless work. When one is running it can be attached to
(`--server`/`--token`, or found automatically) so a viewer that is open is told
when an agent changed something, and so viewer-control tools have somewhere to
send commands.
"""

from __future__ import annotations

import threading

from plexora.mcp import require_mcp, serialize

SERVER_NAME = "plexora"

INSTRUCTIONS = """\
Plexora is a viewer and analysis workspace for multiplexed tissue images: image \
pyramids, segmentation masks and per-cell tables, organised as projects.

Start from the user's question, not from a tool name:
1. `list_projects`, then `inspect_project` before proposing anything -- know the \
image, channels, pixel size, segmentation and cell count first.
2. `list_skills` and `read_skill` for the kind of work you are about to do \
(dataset-triage, visual-inspection, marker-qc, visual-gating).
3. `validate_scope` when unsure whether Plexora can do something; all four \
answers (can_execute, can_analyze, can_recommend, outside_domain) are useful.
4. Look before you conclude: `render_region` and `render_gate_validation` return \
images of the tissue with segmentation and gate overlays, plus a manifest of \
exactly what was drawn.

Writes are bounded: gates and regions are Plexora's own reversible state and \
every write returns a receipt with an operation_id -- cite it. Nothing writes \
into the user's source files unless the server was started to allow it AND the \
user explicitly asked. Errors come back as {code, message, detail, retryable}.\
"""


class Runtime:
    """What every tool call shares: the session, the policy, the audit log,
    and the running Plexora server when there is one."""

    def __init__(self, session=None, *, policy=None, audit=None, link=None, names=None):
        from plexora.agent import AgentSession, Policy, registry
        from plexora.agent.audit import AuditLog

        self.session = session or AgentSession()
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.link = link
        self.names = names
        self._lock = threading.Lock()
        self.registered = registry.discover(names)

    def notify(self, project, plugin, kind, payload=None):
        """Tell an attached server's open viewers that state changed."""
        if self.link is None:
            return False
        try:
            return bool(self.link.notify(project, plugin, kind, payload or {}))
        except Exception:
            return False

    def invoke(self, name, arguments):
        from plexora.agent import registry

        return registry.invoke(self.session, name, arguments, policy=self.policy,
                               audit=self.audit, link=self.link, notify=self.notify)


def _server_info(runtime):
    from plexora import paths
    from plexora.agent import registry
    from plexora.agent.schemas import SCHEMA_VERSION, plexora_version
    from plexora.ai import skills

    owners = sorted({cap.owner for cap in registry.all_capabilities()})
    try:
        data_root = str(paths.data_root())
    except Exception as exc:  # pragma: no cover - a conflicted account
        data_root = f"unavailable: {exc}"
    return {
        "name": SERVER_NAME,
        "plexora_version": plexora_version(),
        "schema_version": SCHEMA_VERSION,
        "data_root": data_root,
        "policy": runtime.policy.describe(),
        "capability_owners": owners,
        "n_capabilities": len(registry.all_capabilities()),
        "attached_server": runtime.link.describe() if runtime.link is not None else None,
        "skills": [skill["name"] for skill in skills.list_skills()],
        "audit_log": str(runtime.audit.path),
    }


def build_server(session=None, *, policy=None, audit=None, link=None, names=None,
                 runtime=None):
    """An `MCPServer` with every discovered capability as a tool."""
    mcpserver = require_mcp()
    from plexora.agent import policy as policy_rules
    from plexora.agent import registry
    from plexora.ai import skills
    from plexora.mcp import resources, tools

    runtime = runtime or Runtime(session, policy=policy, audit=audit, link=link, names=names)
    server = mcpserver.MCPServer(SERVER_NAME, instructions=INSTRUCTIONS,
                                 version=_server_info(runtime)["plexora_version"])

    for capability in registry.all_capabilities():
        server.add_tool(tools.tool_from_capability(capability, runtime),
                        name=capability.tool_name,
                        description=tools.description_for(capability),
                        annotations=tools.annotations_for(capability))

    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True,
                                open_world_hint=False)

    def server_info() -> str:
        """What this Plexora MCP server is: versions, data directory, the permissions
        it was started with, which plugins' capabilities are loaded, whether a Plexora
        viewer is attached, and the skills available."""
        return serialize.bound(_server_info(runtime))

    def list_capabilities(owner: str | None = None) -> str:
        """Every capability (tool) with its purpose, permission class, egress class and
        whether it needs a viewer. `owner` filters to core or one plugin."""
        described = [{k: v for k, v in entry.items() if k != "input_schema"}
                     for entry in registry.describe()
                     if owner is None or entry["owner"] == owner]
        return serialize.bound({"capabilities": described})

    def validate_scope(request: str | list[str], project: str | None = None) -> str:
        """Whether Plexora can do something: `can_execute`, `can_analyze`,
        `can_recommend` (and what is missing) or `outside_domain`. `request` is the
        user's words, or a list of tool names you intend to call."""
        from plexora.agent.errors import as_agent_error

        try:
            answer = policy_rules.classify_scope(runtime.session, request, project=project,
                                                 policy=runtime.policy)
        except Exception as exc:
            raise ToolError(serialize.bound({"error": as_agent_error(exc).to_problem()}))
        return serialize.bound(answer)

    def list_skills() -> str:
        """The scientific skills: how to combine Plexora's tools for a kind of work.
        Read the one that fits with `read_skill` before starting."""
        return serialize.bound({"skills": skills.list_skills()})

    def read_skill(name: str) -> str:
        """One skill's full instructions (markdown)."""
        try:
            return skills.read_skill(name)
        except KeyError as exc:
            raise ToolError(serialize.bound({"error": {
                "code": "invalid_input", "message": str(exc.args[0]), "detail": None,
                "retryable": False}}))

    for fn in (server_info, list_capabilities, validate_scope, list_skills, read_skill):
        server.add_tool(fn, name=fn.__name__, description=fn.__doc__, annotations=read_only)

    resources.register(server, runtime)
    server._plexora_runtime = runtime
    return server


def serve(*, transport="stdio", session=None, policy=None, link=None, names=None):
    """Build the server and run it until the client goes away."""
    import contextlib
    import sys

    if transport != "stdio":
        raise SystemExit(
            f"--transport {transport} is not in this release; Plexora's MCP server "
            "speaks stdio (the client launches it).")
    # Anything printed while plugins load would land in the protocol stream
    # before the SDK claims stdout, so setup prints to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        server = build_server(session, policy=policy, link=link, names=names)
    server.run("stdio")
