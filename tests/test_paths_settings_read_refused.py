"""The settings file survives one refused read.

On Windows a read of settings.json is refused (PermissionError) for the few
milliseconds another process -- the desktop app, a Jupyter sidecar, `plexora
mcp serve` -- spends replacing it with `write_settings`'s os.replace, or while
a scanner holds it. `project.read_config` waits that refusal out for
config.json; `paths.read_settings` must too. A refusal is not "there are no
settings": a read-modify-write must not write back a file holding only its own
key, and a starting process must not settle on the platform default as its
data root when the account has recorded one.
"""

import json
import pathlib

from plexora import paths, updates


def _settings(data):
    path = paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _refuse_once(monkeypatch, target):
    """Path.read_text refuses `target` once, as Windows does mid-replace."""
    original = pathlib.Path.read_text
    target = pathlib.Path(target).resolve()
    refused = []

    def read_text(self, *args, **kwargs):
        if not refused and pathlib.Path(self).resolve() == target:
            refused.append(self)
            raise PermissionError(13, "The process cannot access the file", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    return refused


def test_an_update_check_after_a_refused_read_keeps_every_other_setting(tmp_path, monkeypatch):
    stored = tmp_path / "recorded-data-dir"
    path = _settings({"data_dir": str(stored), "telemetry": {"level": "off"}})
    refused = _refuse_once(monkeypatch, path)

    updates.write_prefs(last_checked="2026-11-13T00:00:00Z")

    assert refused, "the settings file was never read"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written.get("data_dir") == str(stored)
    assert written.get("telemetry") == {"level": "off"}
    assert written.get("updates", {}).get("last_checked") == "2026-11-13T00:00:00Z"


def test_a_starting_process_keeps_the_recorded_data_dir_past_a_refused_read(tmp_path, monkeypatch):
    stored = tmp_path / "recorded-data-dir"
    stored.mkdir()
    platform_default = tmp_path / "platform-default"
    monkeypatch.setattr(paths, "user_data_dir", lambda *a, **k: str(platform_default))
    monkeypatch.delenv(paths.ENV_DATA_PATH, raising=False)
    path = _settings({"data_dir": str(stored)})
    paths.reset()
    refused = _refuse_once(monkeypatch, path)

    resolution = paths.data_root_resolution()

    assert refused, "the settings file was never read"
    assert resolution.path == stored.resolve(), resolution
    # And the answer the process keeps for its lifetime is the same one.
    assert paths.data_root() == stored.resolve()
