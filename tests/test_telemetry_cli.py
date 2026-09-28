"""`plexora telemetry ...` and the one-time terminal notice."""

import ast
import json
from pathlib import Path

from plexora import cli
from plexora.telemetry import config
from plexora.telemetry import cli as telemetry_cli


def run(argv):
    lines = []
    code = telemetry_cli.run(argv, log=lines.append)
    return code, "\n".join(lines)


def test_status_says_off_and_why():
    code, out = run(["status"])
    assert code == 0
    assert out.startswith("Telemetry: off")


def test_modes_are_saved():
    for action, mode in (("diagnostics", "diagnostics"), ("on", "anonymous"), ("off", "off")):
        code, _out = run([action])
        assert code == 0
        assert config.read_prefs()["mode"] == mode


def test_off_explains_an_override():
    code, out = run(["diagnostics"])
    assert "overrides it" in out  # the suite pins PLEXORA_TELEMETRY=off


def test_preview_prints_json_after_a_note():
    code, out = run(["preview"])
    assert code == 0
    note, _, body = out.partition("\n")
    assert note.startswith("# Nothing would be sent")
    assert json.loads(body)["schema"] == 1


def test_sample_is_the_fixture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = run(["sample", "--fixture"])
    assert code == 0
    written = tmp_path / "backend" / "test" / "fixtures" / "sample-batch.json"
    assert json.loads(written.read_text())["events"]


def test_send_without_an_endpoint(telemetry_enabled, monkeypatch):
    monkeypatch.delenv("PLEXORA_TELEMETRY_ENDPOINT")
    code, out = run(["send"])
    assert out == "Upload: no_endpoint" and code == 1


def test_reached_through_the_main_cli(capsys):
    assert cli.main(["telemetry", "status"]) == 0
    assert "Telemetry: off" in capsys.readouterr().out


def test_cli_imports_nothing_from_plexora_at_module_level():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("plexora"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("plexora") for a in node.names)


def test_terminal_notice_once(telemetry_enabled, monkeypatch):
    monkeypatch.delenv("PLEXORA_TELEMETRY")
    lines = []
    cli.telemetry_notice(log=lines.append)
    assert len(lines) == 2 and "plexora telemetry off" in lines[1]
    cli.telemetry_notice(log=lines.append)
    assert len(lines) == 2
    assert config.read_prefs()["notice_shown"] is True


def test_no_notice_when_the_environment_decided(telemetry_enabled):
    lines = []
    cli.telemetry_notice(log=lines.append)  # PLEXORA_TELEMETRY=diagnostics
    assert lines == []
