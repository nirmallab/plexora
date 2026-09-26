"""`plexora ai init` and `plexora ai setup claude|codex|cursor`.

Registers `plexora mcp serve` with an agent client, recording the command as
THIS interpreter (`sys.executable -m plexora mcp serve`) so the client launches
the same environment the user installed Plexora into -- not whichever `python`
happens to be first on the client's PATH.

Existing configuration is merged, never replaced: other servers in the file
are left exactly as they were, and only Plexora's own entry is written.

Formats (checked against each client's documentation, 2026-09):

- Claude Code: `.mcp.json` in the project, `{"mcpServers": {name: {command,
  args, env}}}`; user-wide via `claude mcp add --scope user`.
- Cursor: `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json`, same shape.
- Codex: `[mcp_servers.<name>]` in `~/.codex/config.toml`, or `.codex/config.toml`
  in a trusted project, with `command`, `args`, `env`, `startup_timeout_sec`,
  `tool_timeout_sec`.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path

SERVER_KEY = "plexora"

#: Loading the plugins and opening a project takes longer than a client's
#: default startup timeout on a cold disk; a render can take a while too.
STARTUP_TIMEOUT_S = 60
TOOL_TIMEOUT_S = 300


def server_command(*, allow_source_writes=False) -> list:
    command = [sys.executable, "-m", "plexora", "mcp", "serve"]
    if allow_source_writes:
        command.append("--allow-source-writes")
    return command


def _json_entry(command):
    return {"command": command[0], "args": command[1:], "env": {}}


def _merge_json(path: Path, command, dry_run: bool) -> str:
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise SystemExit(f"{path} is not valid JSON ({exc}); fix it first, nothing "
                             "was written")
    servers = existing.setdefault("mcpServers", {})
    servers[SERVER_KEY] = _json_entry(command)
    text = json.dumps(existing, indent=2) + "\n"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def _toml_string(value: str) -> str:
    return json.dumps(value)


def codex_block(command) -> str:
    args = ", ".join(_toml_string(a) for a in command[1:])
    return (f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {_toml_string(command[0])}\n"
            f"args = [{args}]\n"
            f"startup_timeout_sec = {STARTUP_TIMEOUT_S}\n"
            f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n")


def merge_codex(text: str, command) -> str:
    """`text` with Plexora's table replaced or appended, nothing else touched."""
    header = f"[mcp_servers.{SERVER_KEY}]"
    lines = text.splitlines(keepends=True)
    out, skipping, replaced = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped == header or stripped.startswith(f"[mcp_servers.{SERVER_KEY}."):
            if not replaced:
                out.append(codex_block(command))
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
        merged += ("\n" if merged else "") + codex_block(command)
    # Refuse to write something that no longer parses.
    import tomllib

    tomllib.loads(merged)
    return merged


def _merge_codex_file(path: Path, command, dry_run: bool) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    merged = merge_codex(text, command)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged, encoding="utf-8")
    return merged


def _install_skills(target: Path, dry_run: bool) -> list:
    from plexora.ai.skills import SKILLS_DIR, list_skills

    written = []
    for skill in list_skills():
        source = SKILLS_DIR / skill["name"] / "SKILL.md"
        if not source.exists():
            continue
        destination = target / f"plexora-{skill['name']}" / "SKILL.md"
        written.append(str(destination))
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return written


def setup(client, *, scope="project", project_dir=None, dry_run=False,
          install_skills=False, allow_source_writes=False, out=print) -> int:
    command = server_command(allow_source_writes=allow_source_writes)
    project = Path(project_dir or ".").expanduser().resolve()
    home = Path.home()
    verb = "Would write" if dry_run else "Wrote"

    if client == "claude":
        line = "claude mcp add --scope user plexora -- " + " ".join(
            shlex.quote(part) for part in command)
        if scope == "global":
            out("Claude Code keeps user-wide servers in its own config; run:")
            out(f"  {line}")
        else:
            path = project / ".mcp.json"
            text = _merge_json(path, command, dry_run)
            out(f"{verb} {path}:")
            out(text.rstrip())
            out(f"(For every project instead: {line})")
        if install_skills:
            base = home / ".claude" / "skills" if scope == "global" else project / ".claude" / "skills"
            for path in _install_skills(base, dry_run):
                out(f"{verb} {path}")
    elif client == "cursor":
        path = (home / ".cursor" / "mcp.json" if scope == "global"
                else project / ".cursor" / "mcp.json")
        text = _merge_json(path, command, dry_run)
        out(f"{verb} {path}:")
        out(text.rstrip())
    elif client == "codex":
        path = (home / ".codex" / "config.toml" if scope == "global"
                else project / ".codex" / "config.toml")
        text = _merge_codex_file(path, command, dry_run)
        out(f"{verb} {path}:")
        out(codex_block(command).rstrip())
        if scope != "global":
            out("(Codex reads a project's .codex/config.toml only for trusted projects.)")
    else:  # pragma: no cover - argparse restricts this
        raise SystemExit(f"unknown client {client!r}")
    out("Restart the client, then ask it: \"What Plexora projects do I have?\"")
    return 0


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
