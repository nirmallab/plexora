"""`plexora ai setup`: merge, never clobber."""

import json
import sys
import tomllib

from plexora.ai import setup


def test_claude_project_config_is_merged(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    lines = []
    setup.setup("claude", project_dir=tmp_path, out=lines.append)
    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["other"] == {"command": "x"}
    entry = config["mcpServers"]["plexora"]
    # The project file is shared: no path from this machine goes into it.
    assert entry == {"command": "plexora", "args": ["mcp", "serve"], "env": {}}
    assert sys.executable not in (tmp_path / ".mcp.json").read_text()
    # This machine's interpreter is offered at local scope, outside the tree.
    assert any("claude mcp add --scope local" in line and sys.executable in line
               for line in lines)


def test_a_global_config_pins_this_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Path, "home", classmethod(lambda cls: tmp_path))
    setup.setup("cursor", scope="global", project_dir=tmp_path / "p", out=lambda *_: None)
    entry = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]["plexora"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "plexora", "mcp", "serve"]


def test_dry_run_writes_nothing(tmp_path):
    setup.setup("cursor", project_dir=tmp_path, dry_run=True, out=lambda *_: None)
    assert not (tmp_path / ".cursor").exists()


def test_codex_table_is_replaced_in_place(tmp_path):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "x"\n\n[mcp_servers.plexora]\ncommand = "old"\n\n'
                    '[mcp_servers.other]\ncommand = "keep"\n')
    setup.setup("codex", project_dir=tmp_path, allow_source_writes=True, out=lambda *_: None)
    config = tomllib.loads(path.read_text())
    assert config["model"] == "x"
    assert config["mcp_servers"]["other"]["command"] == "keep"
    plexora = config["mcp_servers"]["plexora"]
    assert plexora["command"] == "plexora"
    assert plexora["args"][-1] == "--allow-source-writes"
    assert plexora["startup_timeout_sec"] >= 30
    assert path.read_text().count("[mcp_servers.plexora]") == 1


def test_codex_appends_to_an_empty_file(tmp_path):
    merged = setup.merge_codex("", setup.server_command())
    assert tomllib.loads(merged)["mcp_servers"]["plexora"]["args"][:2] == ["-m", "plexora"]


URL = "http://127.0.0.1:8321/mcp"


def test_an_http_server_is_registered_with_the_token_by_reference(tmp_path):
    lines = []
    setup.setup("claude", project_dir=tmp_path, http_url=URL, out=lines.append)
    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["plexora"]
    assert entry == {"type": "http", "url": URL,
                     "headers": {"Authorization": "Bearer ${PLEXORA_MCP_TOKEN}"}}
    assert any("export PLEXORA_MCP_TOKEN=" in line for line in lines)
    assert any("--transport http" in line for line in lines)

    setup.setup("cursor", project_dir=tmp_path, http_url=URL, out=lambda *_: None)
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert cursor["plexora"] == {"url": URL, "headers": {
        "Authorization": "Bearer ${env:PLEXORA_MCP_TOKEN}"}}

    setup.setup("codex", project_dir=tmp_path, http_url=URL, out=lambda *_: None)
    codex = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["plexora"]["url"] == URL
    assert codex["mcp_servers"]["plexora"]["bearer_token_env_var"] == "PLEXORA_MCP_TOKEN"
    assert "command" not in codex["mcp_servers"]["plexora"]


def test_the_cli_parses_http_serve_and_tokens():
    from plexora import cli

    serve = cli.build_parser("mcp").parse_args(
        ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "9000",
         "--no-auth", "--allowed-host", "a.example", "--allowed-host", "b.example"])
    assert (serve.transport, serve.host, serve.port, serve.no_auth, serve.allowed_host) == \
        ("http", "0.0.0.0", 9000, True, ["a.example", "b.example"])
    token = cli.build_parser("ai").parse_args(["token", "create", "--scope", "write",
                                               "--label", "x"])
    assert (token.token_command, token.scope, token.label) == ("create", "write", "x")
    http = cli.build_parser("ai").parse_args(["setup", "codex", "--http", URL])
    assert http.http == URL
