"""The MCP server: one tool per capability, a few of its own, and resources.

Runs in its own process with the agent layer in-process -- no Plexora web
server is needed for any headless work. Two transports: stdio (`plexora mcp
serve`, launched by the agent's client) and streamable HTTP (`--transport
http`, for an agent that is not on the machine with the data), which needs a
bearer token on every request and narrows the server's policy by the token's
scope. Either way it is its own process, never a Waitress worker. When one is running it can be attached to
(`--server`/`--token`, or found automatically) so a viewer that is open is told
when an agent changed something, and so viewer-control tools have somewhere to
send commands.
"""

from __future__ import annotations

import threading

from plexora.mcp import require_mcp, serialize

SERVER_NAME = "plexora"

#: The server's instructions, as a template: tools are named by capability
#: (`{tool[project.list]}`), so a renamed tool renames itself here, and the
#: skills and `validate_scope`'s answers come from their own lists.
INSTRUCTIONS = """\
Plexora is a viewer and analysis workspace for multiplexed tissue images: image \
pyramids, segmentation masks and per-cell tables, organised as projects.

Start from the user's question, not from a tool name:
1. `{tool[project.list]}`, then `{tool[project.inspect]}` before proposing anything \
-- know the image, channels, pixel size, segmentation and cell count first.
2. `list_skills` and `read_skill` for the kind of work you are about to do \
({skills}).
3. `validate_scope` when unsure whether Plexora can do something; all \
{n_scope} answers ({scope}) are useful.
4. Look before you conclude: `{tool[image.render_region]}` and \
`{tool[gating.render_validation]}` return images of the tissue with segmentation \
and gate overlays, plus a manifest of exactly what was drawn; \
`{tool[cell.gallery]}` shows single cells (the borderline ones nearest a gate, say) \
and `{tool[cell.explain]}` one cell's markers, regions and neighbours.
5. Long work (`{tool[gating.apply_to_dataset]}`) runs as a job: it returns a job_id \
at once; `{tool[job.wait]}` streams its progress, `{tool[job.cancel]}` stops it.
6. "Gate this image / dataset": `{tool[gating.session_start]}`, then \
`{tool[gating.next]}` and `{tool[gating.answer]}` until it says decided. Plexora \
does every deterministic step; you answer small typed decision packets and never \
type a threshold (skill gate-image).

Writes are bounded: gates and regions are Plexora's own reversible state and \
every write returns a receipt with an operation_id -- cite it; \
`{tool[operation.undo]}` reverses one, and `{tool[operation.report]}` writes up what \
was done. Nothing writes into the user's source files unless the server was \
started to allow it AND the user explicitly asked. Errors come back as \
{{code, message, detail, retryable}}.\
"""


class _ToolNames:
    """`{tool[capability.name]}` in a template -> that capability's tool name."""

    def __getitem__(self, capability):
        from plexora.agent import registry

        return registry.tool_name_of(capability)


def instructions() -> str:
    """INSTRUCTIONS with its names filled in from the registry, the skill
    manifest and `validate_scope`'s states."""
    from plexora.agent.policy import SCOPE_STATES
    from plexora.ai import skills

    return INSTRUCTIONS.format(tool=_ToolNames(),
                               skills=", ".join(s["name"] for s in skills.list_skills()),
                               n_scope=len(SCOPE_STATES), scope=", ".join(SCOPE_STATES))


class Runtime:
    """What every tool call shares: the session, the policy, the audit log,
    and the running Plexora server when there is one."""

    def __init__(self, session=None, *, policy=None, audit=None, link=None, names=None,
                 transport="stdio"):
        from plexora.agent import AgentSession, Policy, registry
        from plexora.agent.audit import AuditLog

        self.session = session or AgentSession()
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.link = link
        self.names = names
        self.transport = transport
        self.auth = None
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

    def request_policy(self):
        """The policy the request being handled runs under: the server's,
        narrowed by the request's bearer token over HTTP.

        Reads a context variable, so call it on the event-loop task that is
        handling the request -- before handing work to a thread.
        """
        from plexora.mcp.auth import current_token, policy_for

        return policy_for(self.policy, current_token())

    def invoke(self, name, arguments, *, policy=None):
        from plexora.agent import registry

        return registry.invoke(self.session, name, arguments, policy=policy or self.policy,
                               audit=self.audit, link=self.link, notify=self.notify)


def _server_info(runtime, policy=None):
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
        "transport": runtime.transport,
        "auth": runtime.auth,
        "policy": (policy or runtime.policy).describe(),
        "capability_owners": owners,
        "n_capabilities": len(registry.all_capabilities()),
        "attached_server": runtime.link.describe() if runtime.link is not None else None,
        "skills": [skill["name"] for skill in skills.list_skills()],
        "audit_log": str(runtime.audit.path),
    }


def build_server(session=None, *, policy=None, audit=None, link=None, names=None,
                 runtime=None, token_verifier=None, transport="stdio"):
    """An `MCPServer` with every discovered capability as a tool.

    `token_verifier` turns on the SDK's bearer-token middleware (HTTP only).
    """
    mcpserver = require_mcp()
    from plexora.agent import policy as policy_rules
    from plexora.agent import registry
    from plexora.ai import skills
    from plexora.mcp import resources, tools

    runtime = runtime or Runtime(session, policy=policy, audit=audit, link=link, names=names,
                                 transport=transport)
    # Tool calls run on worker threads; nothing may be compiled for the first
    # time there (plexora/server/utils/jit.py), so every kernel is primed now.
    from plexora.server.utils import jit

    jit.prime()
    auth = None
    if token_verifier is not None:
        from plexora.mcp.auth import auth_settings

        auth = auth_settings()
        runtime.auth = "bearer"
    server = mcpserver.MCPServer(SERVER_NAME, instructions=instructions(),
                                 version=_server_info(runtime)["plexora_version"],
                                 token_verifier=token_verifier, auth=auth)

    for capability in registry.all_capabilities():
        server.add_tool(tools.tool_from_capability(capability, runtime),
                        name=capability.tool_name,
                        description=tools.description_for(capability),
                        annotations=tools.annotations_for(capability))

    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True,
                                open_world_hint=False)

    async def server_info() -> str:
        """What this Plexora MCP server is: versions, data directory, the permissions
        this connection has, which plugins' capabilities are loaded, whether a Plexora
        viewer is attached, and the skills available."""
        return serialize.bound(_server_info(runtime, runtime.request_policy()))

    def list_capabilities(owner: str | None = None) -> str:
        """Every capability (tool) with its purpose, permission class, egress class and
        whether it needs a viewer. `owner` filters to core or one plugin."""
        described = [{k: v for k, v in entry.items() if k != "input_schema"}
                     for entry in registry.describe()
                     if owner is None or entry["owner"] == owner]
        return serialize.bound({"capabilities": described})

    async def validate_scope(request: str | list[str], project: str | None = None) -> str:
        """Whether Plexora can do something: `can_execute`, `can_analyze`,
        `can_recommend` (and what is missing) or `outside_domain`. `request` is the
        user's words, or a list of tool names you intend to call."""
        import anyio

        from plexora.agent.errors import as_agent_error

        policy = runtime.request_policy()
        try:
            answer = await anyio.to_thread.run_sync(lambda: policy_rules.classify_scope(
                runtime.session, request, project=project, policy=policy))
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

    from plexora.mcp import SERVER_TOOLS

    server_tools = (server_info, list_capabilities, validate_scope, list_skills, read_skill)
    assert tuple(fn.__name__ for fn in server_tools) == SERVER_TOOLS
    for fn in server_tools:
        server.add_tool(fn, name=fn.__name__, description=fn.__doc__, annotations=read_only)

    resources.register(server, runtime)
    from plexora.agent import registry as capability_registry

    if any(cap.owner == "gating" for cap in capability_registry.all_capabilities()):
        from plexora.mcp import prompts, resources_gating

        resources_gating.register(server, runtime)
        prompts.register(server, runtime)

    @server.custom_route("/health", ["GET"])
    async def health(request):
        # Unauthenticated on purpose: says the server is up and nothing else.
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True, "name": SERVER_NAME,
                             "transport": runtime.transport})

    server._plexora_runtime = runtime
    return server


LOOPBACK = ("127.0.0.1", "localhost", "::1")

DEFAULT_HTTP_PORT = 8321


def http_security(host, allowed_hosts=()):
    """DNS-rebinding protection: only these Host headers are served. The SDK
    compares `host:port`, so a bare name needs `name:*`."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [f"{host}:*", "127.0.0.1:*", "localhost:*", "[::1]:*"]
    for extra in allowed_hosts or ():
        extra = extra.strip()
        has_port = "]:" in extra if extra.startswith("[") else ":" in extra
        hosts.append(extra if has_port else f"{extra}:*")
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=list(dict.fromkeys(hosts)),
                                     allowed_origins=list(dict.fromkeys(origins)))


def check_http(host, *, require_auth=True, n_tokens=0):
    """Refuse an HTTP server that would be open to whoever can reach it."""
    if not require_auth:
        if host not in LOOPBACK:
            raise SystemExit("--no-auth is only allowed on a loopback host (127.0.0.1); "
                             f"{host} is reachable from other machines.")
        return
    if n_tokens == 0:
        raise SystemExit(
            "Plexora's HTTP MCP server needs a bearer token on every request, and this "
            "data directory has none yet. Make one first:\n"
            "    plexora ai token create --scope read\n"
            "(or, on this machine only, pass --no-auth).")


def serve(*, transport="stdio", session=None, policy=None, link=None, names=None,
          host="127.0.0.1", port=DEFAULT_HTTP_PORT, path="/mcp", require_auth=True,
          allowed_hosts=()):
    """Build the server and run it until the client goes away (stdio) or the
    process is stopped (HTTP)."""
    import contextlib
    import sys

    if transport not in ("stdio", "http", "streamable-http"):
        raise SystemExit(f"--transport {transport} is not supported; use stdio or http.")
    if transport == "stdio":
        # Anything printed while plugins load would land in the protocol stream
        # before the SDK claims stdout, so setup prints to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            server = build_server(session, policy=policy, link=link, names=names)
        server.run("stdio")
        return

    from plexora.agent.tokens import TokenStore

    store = TokenStore()
    check_http(host, require_auth=require_auth, n_tokens=store.count())
    verifier = None
    if require_auth:
        from plexora.mcp.auth import PlexoraTokenVerifier

        verifier = PlexoraTokenVerifier(store)
    with contextlib.redirect_stdout(sys.stderr):
        server = build_server(session, policy=policy, link=link, names=names,
                              token_verifier=verifier, transport="http")
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}{path}"
    print(f"Plexora MCP server (streamable HTTP) on {url}"
          + ("" if require_auth else "  -- NO AUTH, this machine only"), file=sys.stderr)
    if host in LOOPBACK:
        print("From another machine, tunnel to it:\n"
              f"    ssh -N -L {port}:127.0.0.1:{port} <you>@<this host>\n"
              f"then point the client at http://127.0.0.1:{port}{path}", file=sys.stderr)
    server.run("streamable-http", host=host, port=port, streamable_http_path=path,
               transport_security=http_security(host, allowed_hosts))
