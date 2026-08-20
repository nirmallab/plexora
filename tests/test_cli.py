import os
import sys
import types
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "plexora" / "cli.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("plexora_cli_under_test", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


def test_pyproject_exposes_friendly_console_entry_point():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
        import tomli as tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["project"]["scripts"]["plexora"] == "plexora.cli:main"
    assert config["project"]["scripts"]["plexora-server"] == "plexora.server_cli:main"


def test_browser_url_targets_localhost_when_binding_all_interfaces():
    assert cli.browser_url("0.0.0.0", "8000") == "http://127.0.0.1:8000/"


def test_browser_url_can_open_a_datasource_under_a_base_url():
    assert (
        cli.browser_url("127.0.0.1", 9000, "/proxy/9000/", "my dataset")
        == "http://127.0.0.1:9000/proxy/9000/my%20dataset"
    )


@pytest.mark.parametrize(
    ("env", "system", "expected"),
    [
        ({}, "Linux", False),
        ({"DISPLAY": ":0"}, "Linux", True),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "Linux", True),
        ({"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 55555", "DISPLAY": ":0"}, "Linux", False),
        ({"SLURM_JOB_ID": "123"}, "Linux", False),
        ({}, "Windows", True),
        ({}, "Darwin", True),
    ],
)
def test_should_open_browser_auto_detects_interactive_desktop(env, system, expected):
    assert cli.should_open_browser(env=env, system=system) is expected


def test_browser_flags_override_environment_detection():
    assert cli.should_open_browser(env={"SLURM_JOB_ID": "123"}, system="Linux", preference="yes")
    assert not cli.should_open_browser(env={"DISPLAY": ":0"}, system="Linux", preference="no")


def test_open_browser_waits_on_health_url_but_opens_requested_url():
    waited = []
    opened = []

    cli._open_browser_when_ready(
        "http://127.0.0.1:8000/demo",
        "http://127.0.0.1:8000/",
        wait_fn=lambda url: waited.append(url) or True,
        open_fn=opened.append,
    )

    assert waited == ["http://127.0.0.1:8000/"]
    assert opened == ["http://127.0.0.1:8000/demo"]


def test_main_sets_default_user_data_dir_and_serves(monkeypatch):
    served = {}

    fake_waitress = types.SimpleNamespace(
        serve=lambda app, **kwargs: served.update({"app": app, **kwargs})
    )
    fake_plexora = types.SimpleNamespace(
        app=object(),
        _clean_base_url=lambda base_url: "" if not base_url else "/" + str(base_url).strip("/"),
    )
    monkeypatch.setitem(sys.modules, "waitress", fake_waitress)
    monkeypatch.setitem(sys.modules, "plexora", fake_plexora)
    monkeypatch.setattr(cli, "user_data_dir", lambda name: os.path.join("tmp", name))
    monkeypatch.setattr(cli, "should_open_browser", lambda **kwargs: False)
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)

    cli.main(["--no-browser", "--port", "8765"])

    assert os.environ["PLEXORA_DATA_PATH"] == os.path.join("tmp", "plexora")
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8765


def test_jupyter_start_server_passes_plugins():
    source = (ROOT / "plexora" / "jupyter.py").read_text(encoding="utf-8")

    assert 'cmd.extend(["--plugins", plugins])' in source
    assert 'command.extend(["--plugins", plugins])' not in source
