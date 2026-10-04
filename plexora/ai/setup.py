"""`plexora ai init` and `plexora ai setup claude|codex|cursor`.

Registers `plexora mcp serve` with an agent client. Where the command is
written depends on who else reads the file:

- A per-user config (`--global`, and Claude Code's `claude mcp add --scope
  local|user`) records THIS interpreter (`sys.executable -m plexora mcp serve`),
  so the client launches the environment Plexora was installed into, not
  whichever `python` is first on the client's PATH.
- A file inside the project (`.mcp.json`, `.cursor/mcp.json`,
  `.codex/config.toml`) is shared -- committed, or synced to another machine --
  so it never carries a path from this one. It names `plexora mcp serve`, which
  each machine resolves on its own PATH. For Claude Code, setup also prints the
  `--scope local` line that pins this machine's interpreter outside the tree;
  Claude Code prefers a local entry over the project's.

Existing configuration is merged, never replaced: other servers in the file
are left exactly as they were, and only Plexora's own entry is written.

Formats (checked against each client's documentation, 2026-09):

- Claude Code: `.mcp.json` in the project, `{"mcpServers": {name: {command,
  args, env}}}`; user-wide via `claude mcp add --scope user`.
- Cursor: `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json`, same shape.
- Codex: `[mcp_servers.<name>]` in `~/.codex/config.toml`, or `.codex/config.toml`
  in a trusted project, with `command`, `args`, `env`, `startup_timeout_sec`,
  `tool_timeout_sec`.

`--http URL` registers a server that is already running over streamable HTTP
(`plexora mcp serve --transport http`, often at the far end of an SSH tunnel)
instead. The token never goes into a config file: every shape below names the
environment variable `PLEXORA_MCP_TOKEN`, which the client expands when it
connects.

- Claude Code: `{"type": "http", "url": URL, "headers": {"Authorization":
  "Bearer ${PLEXORA_MCP_TOKEN}"}}`.
- Cursor: `{"url": URL, "headers": {"Authorization": "Bearer
  ${env:PLEXORA_MCP_TOKEN}"}}`.
- Codex: `url = URL` and `bearer_token_env_var = "PLEXORA_MCP_TOKEN"`.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

SERVER_KEY = "plexora"

#: The environment variable an HTTP client config reads its token from.
TOKEN_ENV = "PLEXORA_MCP_TOKEN"

#: Loading the plugins and opening a project takes longer than a client's
#: default startup timeout on a cold disk; a render can take a while too.
STARTUP_TIMEOUT_S = 60
TOOL_TIMEOUT_S = 300


def server_command(*, allow_source_writes=False, portable=False, profile=None) -> list:
    """The command that runs the server; `portable` for a file others read;
    `profile` (plexora.mcp.profiles) offers only one kind of work's tools."""
    command = ["plexora", "mcp", "serve"] if portable else [
        sys.executable, "-m", "plexora", "mcp", "serve"]
    if allow_source_writes:
        command.append("--allow-source-writes")
    if profile and profile != "full":
        command += ["--profile", profile]
    return command


def _json_entry(command):
    return {"command": command[0], "args": command[1:], "env": {}}


def http_entry(client, url) -> dict:
    """An HTTP server's entry in a JSON client config, token by reference."""
    if client == "cursor":
        return {"url": url, "headers": {"Authorization": f"Bearer ${{env:{TOKEN_ENV}}}"}}
    return {"type": "http", "url": url,
            "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"}}


def _merge_json(path: Path, command, dry_run: bool, entry=None) -> str:
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise SystemExit(f"{path} is not valid JSON ({exc}); fix it first, nothing "
                             "was written")
    servers = existing.setdefault("mcpServers", {})
    servers[SERVER_KEY] = entry if entry is not None else _json_entry(command)
    text = json.dumps(existing, indent=2) + "\n"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def _toml_string(value: str) -> str:
    return json.dumps(value)


def codex_block(command=None, *, url=None) -> str:
    if url is not None:
        return (f"[mcp_servers.{SERVER_KEY}]\n"
                f"url = {_toml_string(url)}\n"
                f"bearer_token_env_var = {_toml_string(TOKEN_ENV)}\n"
                f"startup_timeout_sec = {STARTUP_TIMEOUT_S}\n"
                f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n")
    args = ", ".join(_toml_string(a) for a in command[1:])
    return (f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {_toml_string(command[0])}\n"
            f"args = [{args}]\n"
            f"startup_timeout_sec = {STARTUP_TIMEOUT_S}\n"
            f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n")


def merge_codex(text: str, command, *, url=None) -> str:
    """`text` with Plexora's table replaced or appended, nothing else touched."""
    header = f"[mcp_servers.{SERVER_KEY}]"
    lines = text.splitlines(keepends=True)
    out, skipping, replaced = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped == header or stripped.startswith(f"[mcp_servers.{SERVER_KEY}."):
            if not replaced:
                out.append(codex_block(command, url=url))
                replaced = True
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    merged = "".join(out)
    if not replaced:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += ("\n" if merged else "") + codex_block(command, url=url)
    # Refuse to write something that no longer parses.
    import tomllib

    tomllib.loads(merged)
    return merged


def _merge_codex_file(path: Path, command, dry_run: bool, url=None) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    merged = merge_codex(text, command, url=url)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged, encoding="utf-8")
    return merged


def _install_skills(target: Path, dry_run: bool) -> list:
    from plexora.ai.skills import SKILLS_DIR, list_skills, read_skill

    written = []
    for skill in list_skills():
        source = SKILLS_DIR / skill["name"] / "SKILL.md"
        if not source.exists():
            continue
        destination = target / f"plexora-{skill['name']}" / "SKILL.md"
        written.append(str(destination))
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Rendered, as read_skill serves it: the client's copy carries the
            # numbers this version of Plexora decides with.
            destination.write_text(read_skill(skill["name"]), encoding="utf-8")
    return written


def _install_agents(target: Path, dry_run: bool) -> list:
    """One agent definition per delegated role (`delegation.agent_file`), for
    a client that reads agent files (Claude Code's `.claude/agents/`)."""
    from plexora.ai import delegation

    written = []
    for role in delegation.ROLES:
        destination = target / f"{delegation.agent_name(role)}.md"
        written.append(str(destination))
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(delegation.agent_file(role, SERVER_KEY), encoding="utf-8")
    return written


def tiers_command(pairs=(), *, out=print) -> int:
    """`plexora ai tiers [TIER=MODEL ...]`: show, or set, which of your
    client's models each tier runs on (an empty MODEL clears the tier)."""
    from plexora.ai import delegation

    if pairs:
        mapping = {}
        for pair in pairs:
            tier, sep, model = pair.partition("=")
            if not sep:
                out(f"expected TIER=MODEL, got {pair!r}")
                return 2
            mapping[tier.strip()] = model.strip()
        try:
            delegation.set_models(mapping)
        except KeyError as exc:
            out(exc.args[0])
            return 2
    for tier, info in delegation.describe().items():
        out(f"{tier}: {info['model'] or '(unset: the client picks)'}")
        out(f"  {info['for']}")
    for role, spec in delegation.ROLES.items():
        out(f"role {role}: tier {spec['tier']}, skill {spec['skill']}")
    return 0


def models_command(action="show", *, force=False, out=print) -> int:
    """`plexora ai models [show|init|path]`: the user's task -> model file
    (`plexora.ai.models_config`) -- what it maps, writing a starting one, or
    where it is read from."""
    from plexora.ai import models_config

    if action == "path":
        out(str(models_config.path()))
        return 0
    if action == "init":
        try:
            written = models_config.write_template(force=force)
        except FileExistsError as exc:
            out(f"{exc.args[0]} exists; edit it, or pass --force to start over")
            return 1
        out(f"wrote {written}: fill in a model for the tasks you want to pin")
        return 0
    info = models_config.describe()
    out(f"file: {info['path']}" + ("" if info["exists"] else
                                   " (none: your agent chooses every model; "
                                   "`plexora ai models init` writes one)"))
    for module, tasks in info["tasks"].items():
        out(f"{module}:")
        for task, model in tasks.items():
            out(f"  {task.split('.', 1)[1]}: {model or '(agent chooses)'}")
    for problem in info["problems"]:
        out(f"problem: {problem}")
    return 1 if info["problems"] else 0


def setup(client, *, scope="project", project_dir=None, dry_run=False,
          install_skills=False, allow_source_writes=False, http_url=None, profile=None,
          out=print) -> int:
    if profile:
        from plexora.mcp import profiles

        profile = profiles.check(profile)
    pinned = server_command(allow_source_writes=allow_source_writes, profile=profile)
    command = server_command(allow_source_writes=allow_source_writes,
                             portable=scope != "global", profile=profile)
    project = Path(project_dir or ".").expanduser().resolve()
    home = Path.home()
    verb = "Would write" if dry_run else "Wrote"
    entry = http_entry(client, http_url) if http_url else None
    if http_url and allow_source_writes:
        out("(--allow-source-writes belongs on the HTTP server's own command line; it is "
            "ignored here.)")

    if client == "claude":
        if http_url:
            line = ("claude mcp add --scope user --transport http plexora "
                    f"{shlex.quote(http_url)} --header "
                    f"'Authorization: Bearer ${{{TOKEN_ENV}}}'")
        else:
            line = "claude mcp add --scope user plexora -- " + " ".join(
                shlex.quote(part) for part in pinned)
        if scope == "global":
            out("Claude Code keeps user-wide servers in its own config; run:")
            out(f"  {line}")
        else:
            path = project / ".mcp.json"
            text = _merge_json(path, command, dry_run, entry)
            out(f"{verb} {path}:")
            out(text.rstrip())
            if not http_url:
                out("(Portable: `plexora` must be on the client's PATH. To pin this "
                    "machine's interpreter, outside the project, run:")
                out("  claude mcp add --scope local plexora -- " + " ".join(
                    shlex.quote(part) for part in pinned) + ")")
            out(f"(For every project instead: {line})")
        if install_skills:
            base = home / ".claude" / "skills" if scope == "global" else project / ".claude" / "skills"
            for path in _install_skills(base, dry_run):
                out(f"{verb} {path}")
            agents = base.parent / "agents"
            for path in _install_agents(agents, dry_run):
                out(f"{verb} {path}")
    elif client == "cursor":
        path = (home / ".cursor" / "mcp.json" if scope == "global"
                else project / ".cursor" / "mcp.json")
        text = _merge_json(path, command, dry_run, entry)
        out(f"{verb} {path}:")
        out(text.rstrip())
        if scope != "global" and not http_url:
            out("(Portable: `plexora` must be on Cursor's PATH; --global pins this "
                "machine's interpreter in ~/.cursor/mcp.json instead.)")
    elif client == "codex":
        path = (home / ".codex" / "config.toml" if scope == "global"
                else project / ".codex" / "config.toml")
        text = _merge_codex_file(path, command, dry_run, url=http_url)
        out(f"{verb} {path}:")
        out(codex_block(command, url=http_url).rstrip())
        if scope != "global":
            out("(Codex reads a project's .codex/config.toml only for trusted projects.)")
            if not http_url:
                out("(Portable: `plexora` must be on Codex's PATH; --global pins this "
                    "machine's interpreter in ~/.codex/config.toml instead.)")
    else:  # pragma: no cover - argparse restricts this
        raise SystemExit(f"unknown client {client!r}")
    if http_url:
        out(f"The client reads the token from ${TOKEN_ENV}; set it where the client "
            "runs, once:")
        out(f"  export {TOKEN_ENV}=<the secret `plexora ai token create` printed>")
    out("Restart the client, then ask it: \"What Plexora projects do I have?\"")
    return 0


def token_command(command, *, scope="read", label="", expires_days=None, token_id=None,
                  out=print) -> int:
    """`plexora ai token create|list|revoke`."""
    from plexora.agent.tokens import TokenStore

    store = TokenStore()
    if command == "create":
        secret, record = store.create(scope=scope, label=label, expires_days=expires_days)
        out(f"Token {record['id']} ({record['scope']}"
            + (f", {record['label']}" if record["label"] else "")
            + (f", expires {record['expires']}" if record["expires"] else "") + "):")
        out(f"  {secret}")
        out("Shown once, and not stored -- copy it now. Give it to the client as:")
        out(f"  export {TOKEN_ENV}={secret}")
        return 0
    if command == "list":
        records = store.list()
        if not records:
            out(f"No tokens in {store.path}. Make one: plexora ai token create --scope read")
            return 0
        for record in records:
            state = ("revoked" if record.get("revoked")
                     else "live" if store.live(record) else "expired")
            out(f"{record['id']}  {record['scope']:<5}  {state:<7}  created "
                f"{record['created']}  last used {record.get('last_used') or 'never'}"
                + (f"  {record['label']}" if record.get("label") else ""))
        return 0
    if command == "revoke":
        if store.revoke(token_id):
            out(f"Revoked {token_id}; it is refused from the next request.")
            return 0
        out(f"No token {token_id!r}; `plexora ai token list` shows them.")
        return 1
    out("Usage: plexora ai token create [--scope read|write|admin] [--label L] "
        "[--expires-days N] | list | revoke ID")
    return 2


def init(check=False, out=print) -> int:
    """Report whether this install is ready for agents. Changes nothing."""
    ok = True
    try:
        from plexora.mcp import require_mcp

        require_mcp()
        import mcp  # noqa: F401

        out("MCP SDK: installed")
    except ImportError:
        ok = False
        out("MCP SDK: missing -- pip install 'plexora[ai]'")
    try:
        import yaml  # noqa: F401

        out("PyYAML: installed")
    except ImportError:
        ok = False
        out("PyYAML: missing -- pip install 'plexora[ai]'")
    try:
        from plexora import paths
        from plexora.server.models.project import Project

        out(f"Data directory: {paths.data_root()} ({len(Project.load_all())} projects)")
        from plexora.agent.tokens import TokenStore

        out(f"HTTP tokens: {TokenStore().count()} live (plexora ai token create)")
    except Exception as exc:
        ok = False
        out(f"Data directory: unavailable ({exc})")
    if ok:
        import contextlib

        from plexora.agent import registry
        from plexora.ai import skills

        with contextlib.redirect_stdout(sys.stderr):
            registry.discover()
        caps = registry.all_capabilities()
        out(f"Capabilities: {len(caps)} from {sorted({c.owner for c in caps})}")
        problems = skills.validate({c.tool_name for c in caps})
        out(f"Skills: {len(skills.list_skills())}" + (f", {len(problems)} problem(s)"
                                                    if problems else ", all valid"))
        for problem in problems:
            out(f"  - {problem}")
        ok = not problems
    out("Server command: " + " ".join(shlex.quote(p) for p in server_command()))
    if ok:
        out("Ready. Connect a client with: plexora ai setup claude|codex|cursor")
    return 0 if ok else 1


def skills_command(check=False, out=print) -> int:
    from plexora.ai import skills

    for skill in skills.list_skills():
        out(f"{skill['name']:<20} {skill['title']}")
    if not check:
        return 0
    problems = skills.validate()
    for problem in problems:
        out(f"  - {problem}")
    return 1 if problems else 0
