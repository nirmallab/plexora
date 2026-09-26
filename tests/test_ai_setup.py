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
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "plexora", "mcp", "serve"]
    assert any("claude mcp add" in line for line in lines)


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
    assert plexora["command"] == sys.executable
    assert plexora["args"][-1] == "--allow-source-writes"
    assert plexora["startup_timeout_sec"] >= 30
    assert path.read_text().count("[mcp_servers.plexora]") == 1


def test_codex_appends_to_an_empty_file(tmp_path):
    merged = setup.merge_codex("", setup.server_command())
    assert tomllib.loads(merged)["mcp_servers"]["plexora"]["args"][:2] == ["-m", "plexora"]
