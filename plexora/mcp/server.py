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
import time

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
type a threshold (skill gate-image). The session's delegate block hands the packet \
loop to fresh worker conversations a few markers at a time; every skill names the \
model tier it needs (`list_skills`), so routine work can run on a cheaper model. \
When the user's models file maps tasks to models (`server_info.ai_models`), the \
block lists one worker per model -- launch each on its model.
7. "QC this image" / "is it in focus, aligned, well segmented": local checks score \
the tissue first; you judge places sampled across each score \
(`{tool[qc.sample_examples]}`, or `{tool[qc.session_start]}`'s packets) and move a \
bar only a step at a time, then write the artifacts as regions and cell flags \
(skills qc-image, qc-checks).

Writes are bounded: gates and regions are Plexora's own reversible state and \
every write returns a receipt with an operation_id -- cite it; \
`{tool[operation.undo]}` reverses one, and `{tool[operation.report]}` writes up what \
was done. Nothing writes into the user's source files unless the server was \
started to allow it AND the user explicitly asked. Errors come back as \
{{code, message, detail, retryable}}.

Licensing: Free tools always answer. A Paid tool called from here needs its own \
grant and external MCP access (`server_info.license.mcp`); without them it answers \
license_required with a hint to pass on to the user.\
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

    #: How often an automatically found link is looked for again.
    REDISCOVER_S = 15.0

    def __init__(self, session=None, *, policy=None, audit=None, link=None, names=None,
                 transport="stdio", rediscover=False, origin=None):
        from plexora.agent import AgentSession, Policy, registry
        from plexora.agent.audit import AuditLog

        #: How this connection's calls are marked (`registry.CALL_ORIGIN`):
        #: an outside agent (`mcp`) unless the process was started by another
        #: application through the bridge and proved it (`--origin bridge`
        #: with a matching nonce; see `bridge_wire.mcp_origin`).
        self.origin = origin or registry.ORIGIN_MCP

        self.session = session or AgentSession()
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self._link = link
        # Found automatically (not --server / PLEXORA_SERVER_URL): looked for
        # again when it stops answering or a newer server announces itself,
        # so an MCP process outlives the Plexora it first saw.
        self.rediscover = rediscover
        self._link_checked = time.monotonic()
        self.started_at = time.time()
        self.names = names
        self.transport = transport
        self.auth = None
        #: The tool profile offered (plexora.mcp.profiles); `build_server` sets it.
        self.profile = "full"
        self._lock = threading.Lock()
        #: The licence recheck's stop event (`start_license_recheck`), or None.
        self.license_recheck = None
        self.registered = registry.discover(names)

    @property
    def link(self):
        if self.rediscover and time.monotonic() - self._link_checked >= self.REDISCOVER_S:
            self._link_checked = time.monotonic()
            self._link = _rediscovered(self._link)
        return self._link

    @link.setter
    def link(self, value):
        self._link = value

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
        from plexora.telemetry import telemetry

        # Which transport this call came in on, for telemetry's capability
        # counts. A context variable, so a capability that calls another sees
        # it too -- and is told apart as nested by the registry's own depth.
        token = telemetry.call_source.set(self.transport)
        # And how it came, for the licence: from an external MCP client a Paid
        # capability also needs `mcp` (guards.check_origin); from another
        # application through the bridge it does not.
        try:
            return registry.invoke(self.session, name, arguments,
                                   policy=policy or self.policy, audit=self.audit,
                                   link=self.link, notify=self.notify, origin=self.origin)
        finally:
            telemetry.call_source.reset(token)


def _rediscovered(link):
    """The newest running server that answers -- `link` itself when it is
    still that one (so its probed control plane is kept)."""
    try:
        from plexora.agent.attach import find_server

        found = find_server()
    except Exception:  # attaching is optional
        return link
    if found is None:
        return None
    if link is not None and found.base_url == link.base_url:
        return link
    return found


def _stale_code(started_at):
    """Plexora source files changed since this process started: a restart
    (an MCP reconnect) is needed for the tools to see them."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    changed = []
    for path in root.rglob("*.py"):
        try:
            if path.stat().st_mtime > started_at:
                changed.append(str(path.relative_to(root.parent)))
        except OSError:
            continue
        if len(changed) >= 5:
            break
    return changed


def _offered(runtime):
    """The capabilities this connection offers as tools: the runtime's profile of them."""
    from plexora.agent import registry
    from plexora.mcp import profiles

    return [cap for cap in registry.all_capabilities()
            if profiles.allows(runtime.profile, cap.tool_name, cap.owner)]


def _server_info(runtime, policy=None):
    from plexora import paths
    from plexora.agent import registry
    from plexora.agent.schemas import SCHEMA_VERSION, plexora_version
    from plexora.ai import delegation, models_config, skills

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
        "origin": runtime.origin,
        "auth": runtime.auth,
        "policy": (policy or runtime.policy).describe(),
        "capability_owners": owners,
        "n_capabilities": len(registry.all_capabilities()),
        # `plexora mcp serve --profile`: the tools this connection offers (plexora.mcp.profiles).
        "profile": runtime.profile,
        "n_tools": len(_offered(runtime)),
        "attached_server": runtime.link.describe() if runtime.link is not None else None,
        "started_at": _iso(runtime.started_at),
        "code_changed_since_start": _stale_code(runtime.started_at),
        "skills": [skill["name"] for skill in skills.list_skills()],
        # Who runs what (`plexora.ai.delegation`): no vendor's model names --
        # the user maps each tier to a model of their client's.
        "model_tiers": delegation.describe(),
        # The user's task -> model file (`plexora.ai.models_config`): which
        # model answers each AI task's packets; absent, the agent chooses.
        "ai_models": models_config.describe(),
        "audit_log": str(runtime.audit.path),
        "license": _license_info(),
        # Whether this server speaks the shared protocol other applications
        # (SCIMAP Pro) use, and its version -- never a token.
        "bridge": _bridge_info(),
    }


def _bridge_info():
    """The bridge block of `server_info`: the protocol and whether the optional
    package is installed. Imports nothing heavy."""
    from plexora.agent.core import bridge

    return bridge.describe()


def _iso(seconds):
    import datetime

    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def _license_info():
    """Plan, state and grants -- enough for an agent to say "that is a Paid
    feature" before trying, and `mcp`: whether Paid tools answer over this
    connection at all. Never the certificate; no network call."""
    try:
        from plexora import licensing
        from plexora.licensing import guards

        state = licensing.peek()
        info = {"plan": state.plan, "state": state.state, "entitlements": list(state.entitlements),
                "mcp": state.allows(guards.MCP)}
        if not info["mcp"]:
            info["hint"] = guards.hint_for(guards.MCP, state.state)
        return info
    except Exception:  # pragma: no cover - licensing never breaks server_info
        return {"plan": "free", "state": "free", "entitlements": [], "mcp": False}


#: How often a running MCP server asks the licence service about its
#: certificate, so a revocation or a grant change lands within minutes.
LICENSE_RECHECK_ENV = "PLEXORA_MCP_LICENSE_RECHECK_S"
LICENSE_RECHECK_DEFAULT_S = 900.0
LICENSE_RECHECK_FLOOR_S = 60.0
#: The refresh at start waits this long at most, then the cached certificate stands.
LICENSE_START_TIMEOUT_S = 3.0


def license_recheck_interval() -> float:
    """`PLEXORA_MCP_LICENSE_RECHECK_S`, at least a minute; 15 minutes when unset or unreadable."""
    import os

    try:
        value = float(os.environ.get(LICENSE_RECHECK_ENV) or LICENSE_RECHECK_DEFAULT_S)
    except ValueError:
        return LICENSE_RECHECK_DEFAULT_S
    if value != value:  # NaN
        return LICENSE_RECHECK_DEFAULT_S
    return max(LICENSE_RECHECK_FLOOR_S, value)


def start_license_recheck(*, interval: float | None = None):
    """Refresh the licence now (briefly, failing open to the cached
    certificate), then keep refreshing it on a daemon thread. Returns the
    thread's stop event, or None when the licence is never refreshed here (Free,
    offline, an offline licence file): then nothing runs at all."""
    from plexora.licensing import state

    if state.refresh_now(reason="mcp_start", timeout=LICENSE_START_TIMEOUT_S) == "skipped":
        return None
    interval = license_recheck_interval() if interval is None else interval
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                if state.refresh_now(reason="mcp_recheck") == "skipped":
                    return
            except Exception:  # noqa: BLE001 - a recheck never takes the server down
                continue

    threading.Thread(target=loop, name="plexora-mcp-license", daemon=True).start()
    return stop


def build_server(session=None, *, policy=None, audit=None, link=None, names=None,
                 runtime=None, token_verifier=None, transport="stdio", rediscover=False, profile=None,
                 license_recheck=False, origin=None):
    """An `MCPServer` with every discovered capability as a tool -- or, with a
    `profile` (plexora.mcp.profiles), the ones that kind of work needs.

    `token_verifier` turns on the SDK's bearer-token middleware (HTTP only).
    `license_recheck` (what `serve` passes) refreshes the licence now and then
    every `license_recheck_interval()`; tests that build a server leave it off.
    """
    mcpserver = require_mcp()
    from plexora.agent import policy as policy_rules
    from plexora.agent import registry
    from plexora.ai import skills
    from plexora.mcp import resources, tools

    from plexora.mcp import profiles

    runtime = runtime or Runtime(session, policy=policy, audit=audit, link=link, names=names,
                                 transport=transport, rediscover=rediscover, origin=origin)
    runtime.profile = profiles.check(profile or getattr(runtime, "profile", None))
    # Tool calls run on worker threads; nothing may be compiled for the first
    # time there (plexora/server/utils/jit.py), so every kernel is primed now.
    from plexora.server.utils import jit

    jit.prime()
    if license_recheck:
        runtime.license_recheck = start_license_recheck()
    auth = None
    if token_verifier is not None:
        from plexora.mcp.auth import auth_settings

        auth = auth_settings()
        runtime.auth = "bearer"
    server = mcpserver.MCPServer(SERVER_NAME, instructions=instructions(),
                                 version=_server_info(runtime)["plexora_version"],
                                 token_verifier=token_verifier, auth=auth)

    for capability in _offered(runtime):
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

        def classify():
            # Asked over this connection, so `license_required` lists what this
            # path refuses (over MCP, Paid tools without the `mcp` add-on).
            origin = registry.CALL_ORIGIN.set(runtime.origin)
            try:
                return policy_rules.classify_scope(runtime.session, request, project=project,
                                                   policy=policy)
            finally:
                registry.CALL_ORIGIN.reset(origin)

        try:
            answer = await anyio.to_thread.run_sync(classify)
        except Exception as exc:
            raise ToolError(serialize.bound({"error": as_agent_error(exc).to_problem()}))
        return serialize.bound(answer)

    def list_skills() -> str:
        """The scientific skills: how to combine Plexora's tools for a kind of work.
        Read the one that fits with `read_skill` before starting. Each names the
        model `tier` it needs (`tiers`): routine work can go to a cheaper model."""
        from plexora.ai import delegation

        return serialize.bound({"skills": skills.list_skills(), "tiers": delegation.describe()})

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

    owners = {cap.owner for cap in capability_registry.all_capabilities()}
    from plexora.mcp import prompts

    if "gating" in owners:
        from plexora.mcp import resources_gating

        resources_gating.register(server, runtime)
    # Plugins add their own resources (`Plugin.mcp_factory`), for the ones
    # whose capabilities this server offers.
    for name, contribution in prompts.plugin_contributions():
        if name in owners and contribution.get("resources"):
            contribution["resources"](server, runtime)
    if owners & {"gating", "qc"}:
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
          allowed_hosts=(), rediscover=False, profile=None, origin=None):
    """Build the server and run it until the client goes away (stdio) or the
    process is stopped (HTTP). `origin` is what `bridge_wire.mcp_origin`
    decided from `--origin`/`--bridge-nonce`; None is an outside agent."""
    import contextlib
    import sys

    if transport not in ("stdio", "http", "streamable-http"):
        raise SystemExit(f"--transport {transport} is not supported; use stdio or http.")
    if transport == "stdio":
        # Anything printed while plugins load would land in the protocol stream
        # before the SDK claims stdout, so setup prints to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            server = build_server(session, policy=policy, link=link, names=names,
                                  rediscover=rediscover, profile=profile, license_recheck=True,
                                  origin=origin)
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
                              token_verifier=verifier, transport="http", rediscover=rediscover,
                              profile=profile, license_recheck=True)
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}{path}"
    print(f"Plexora MCP server (streamable HTTP) on {url}"
          + ("" if require_auth else "  -- NO AUTH, this machine only"), file=sys.stderr)
    if host in LOOPBACK:
        print("From another machine, tunnel to it:\n"
              f"    ssh -N -L {port}:127.0.0.1:{port} <you>@<this host>\n"
              f"then point the client at http://127.0.0.1:{port}{path}", file=sys.stderr)
    server.run("streamable-http", host=host, port=port, streamable_http_path=path,
               transport_security=http_security(host, allowed_hosts))
