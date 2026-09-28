"""Which telemetry mode is in force, and that asking never breaks anything."""

import json
import subprocess
import sys
import time

import pytest

from plexora.telemetry import config, identity


def resolve(env=None, prefs=None, server=None, testing=False):
    return config.resolve(env=env or {}, prefs=prefs or {}, server=server, testing=testing)


def test_default_is_anonymous():
    assert resolve() == ("anonymous", "default", None)


def test_testing_wins_over_everything():
    got = resolve({"PLEXORA_TELEMETRY": "diagnostics"}, {"mode": "diagnostics"}, testing=True)
    assert got.mode == "off" and got.source == "testing"


def test_live_resolution_is_off_under_pytest():
    assert config.under_pytest()
    assert config.resolve().mode == "off"


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_do_not_track_turns_it_off(value):
    got = resolve({"DO_NOT_TRACK": value, "PLEXORA_TELEMETRY": "diagnostics"})
    assert got == ("off", "do_not_track", None)


def test_do_not_track_zero_is_not_a_request():
    assert resolve({"DO_NOT_TRACK": "0"}).mode == "anonymous"


@pytest.mark.parametrize("value,mode", [
    ("off", "off"), ("0", "off"), ("false", "off"), ("anonymous", "anonymous"),
    ("diagnostics", "diagnostics"), ("on", "anonymous"), ("DIAGNOSTICS", "diagnostics"),
])
def test_env_values(value, mode):
    assert resolve({"PLEXORA_TELEMETRY": value}).mode == mode


def test_invalid_env_is_ignored_not_consent():
    got = resolve({"PLEXORA_TELEMETRY": "diagnostic-ish"}, {"mode": "off"})
    assert got == ("off", "settings", None)


def test_env_beats_settings():
    got = resolve({"PLEXORA_TELEMETRY": "anonymous"}, {"mode": "diagnostics"})
    assert got == ("anonymous", "env", None)


def test_settings_used_when_env_silent():
    assert resolve(prefs={"mode": "diagnostics"}) == ("diagnostics", "settings", None)


def test_ceiling_only_lowers():
    lowered = resolve({"PLEXORA_TELEMETRY": "diagnostics"}, server={"level_max": "anonymous"})
    assert lowered == ("anonymous", "ceiling", "anonymous")
    kept = resolve(prefs={"mode": "anonymous"}, server={"level_max": "diagnostics"})
    assert kept.mode == "anonymous" and kept.source == "settings"


def test_server_pause_turns_off_until_it_expires():
    now = time.time()
    paused = resolve(server={"disabled_until": now + 60})
    assert paused.mode == "off" and paused.source == "server"
    assert resolve(server={"disabled_until": now - 60}).mode == "anonymous"


def test_garbage_never_raises():
    assert resolve(prefs={"mode": 12}, server={"level_max": [], "disabled_until": "x"}).mode \
        == "anonymous"
    assert config.resolve(env={}, prefs="nope", server="nope", testing=False).mode in config.MODES


def test_garbage_settings_file_reads_as_empty(tmp_path):
    from plexora import paths

    paths.settings_path().write_text("{not json", encoding="utf-8")
    assert config.read_prefs() == {}
    paths.settings_path().write_text(json.dumps({"telemetry": "string"}), encoding="utf-8")
    assert config.read_prefs() == {}


def test_write_prefs_keeps_other_settings():
    from plexora import paths

    paths.write_settings({"data_dir": "/somewhere", "updates": {"auto_check": False}})
    config.write_prefs(mode="diagnostics", notice_shown=True)
    data = paths.read_settings()
    assert data["data_dir"] == "/somewhere"
    assert data["updates"] == {"auto_check": False}
    assert data["telemetry"] == {"mode": "diagnostics", "notice_shown": True}
    config.write_prefs(notice_shown=None)
    assert paths.read_settings()["telemetry"] == {"mode": "diagnostics"}


def test_install_id_minted_once_and_persisted():
    first = identity.install_id()
    assert len(first) == 32 and int(first, 16) >= 0
    assert identity.install_id() == first
    assert config.read_prefs()["install_id"] == first
    assert identity.reset_install_id() != first


def test_install_id_is_not_minted_when_asked_not_to():
    assert identity.install_id(mint=False) is None
    assert "install_id" not in config.read_prefs()


def test_install_id_ephemeral_when_settings_unwritable(monkeypatch):
    monkeypatch.setattr(config, "write_prefs", lambda **_: None)
    first = identity.install_id()
    assert len(first) == 32
    assert identity.install_id() == first  # stable for the process


def test_owner_label():
    assert identity.owner_label("gating") == "gating"
    label = identity.owner_label("acme_secret_plugin")
    assert label.startswith("ext:") and len(label) == 12
    assert "acme" not in label


def test_endpoint_trailing_slash():
    assert config.endpoint({"PLEXORA_TELEMETRY_ENDPOINT": "https://x.example/"}) \
        == "https://x.example"
    assert config.endpoint({}) == config.DEFAULT_ENDPOINT.rstrip("/")


def test_importing_telemetry_writes_nothing(tmp_path):
    """A fresh interpreter importing the package creates no file anywhere
    under its data root and home, starts no thread."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    code = (
        "import threading, plexora.telemetry as t\n"
        "from plexora.telemetry import schema, config, queue, batch, redact, uploader\n"
        "t.count('tool.summary', 'open', tool='gating')\n"
        "assert not [x for x in threading.enumerate() if x.name.startswith('plexora-telemetry')]\n"
        "print('ok')\n"
    )
    env = {"HOME": str(home), "PLEXORA_DATA_PATH": str(data), "PATH": "/usr/bin:/bin",
           "PLEXORA_TELEMETRY": "diagnostics", "XDG_CONFIG_HOME": str(home / ".config")}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    written = [p for p in list(data.rglob("*")) + list(home.rglob("*")) if p.is_file()]
    assert written == []
