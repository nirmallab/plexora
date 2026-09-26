"""`plexora --desktop`: the server the desktop app starts, in isolation.

Everything the shell relies on is on the three standard streams -- one JSON
line on stdout once the socket is bound, logs on stderr, end-of-file on stdin
as "quit" -- so that is what is pinned here, with Waitress and the package
faked out. `tests/test_desktop_process.py` runs the real thing.
"""

import importlib.util
import io
import json
import os
import socket
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "plexora" / "cli.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("plexora_cli_desktop_under_test",
                                                  CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()

READY_KEYS = {"event", "protocol", "url", "origin", "host", "port", "token",
              "pid", "version", "data_root", "settings_path", "log", "shutdown"}


class FakeServer:
    def __init__(self, rig, sockets):
        self.rig = rig
        self.sockets = sockets
        self.closed = False

    def run(self):
        self.rig.events.append("run")
        if self.rig.interrupt:
            raise KeyboardInterrupt

    def close(self):
        self.closed = True
        for sock in self.sockets:
            sock.close()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Fake Waitress, a fake package and captured streams.

    The environment is restored wholesale afterwards: `main()` writes
    PLEXORA_AUTH_TOKEN, PLEXORA_PLUGINS and friends straight into os.environ,
    and monkeypatch only undoes what it set itself -- a leaked token turns
    every later page test into a 403.
    """
    saved_environ = dict(os.environ)
    yield from _rig(monkeypatch, tmp_path)
    os.environ.clear()
    os.environ.update(saved_environ)


def _rig(monkeypatch, tmp_path):
    rig = types.SimpleNamespace(events=[], created=[], watched=[], signals=[],
                                interrupt=True, notices=[], stdout=io.StringIO(),
                                stderr=io.StringIO())

    def create_server(app, sockets=(), **kwargs):
        # The announce must not have happened yet: the shell reads the ready
        # line as "the socket is bound", so a line before this is a lie.
        assert rig.stdout.getvalue() == ""
        rig.events.append("bind")
        server = FakeServer(rig, list(sockets))
        rig.created.append((server, kwargs))
        return server

    lifetime = types.SimpleNamespace(
        ensure_std_streams=lambda: rig.events.append("streams"),
        flush_std=lambda: None,
        install_signal_handlers=lambda: rig.signals.append(True),
        request_shutdown=lambda *a, **k: True,
        watch_stdin=lambda on_eof: rig.watched.append(on_eof),
    )
    app = types.SimpleNamespace(config={})
    fake_paths = types.SimpleNamespace(
        first_run_notice=lambda: "\n".join(rig.notices) or None,
        data_root_notices=lambda: [],
        DataRootError=RuntimeError,
        data_root=lambda: tmp_path / "data",
        settings_path=lambda: tmp_path / "settings.json",
    )
    package = types.SimpleNamespace(app=app, paths=fake_paths)
    models = types.ModuleType("plexora.server.models")
    models.data_model = types.SimpleNamespace(
        prime_hot_code=lambda: rig.events.append("prime"))

    monkeypatch.setitem(sys.modules, "waitress", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "waitress.server",
                        types.SimpleNamespace(create_server=create_server))
    monkeypatch.setitem(sys.modules, "plexora", package)
    monkeypatch.setitem(sys.modules, "plexora._lifetime", lifetime)
    monkeypatch.setitem(sys.modules, "plexora._resources",
                        types.SimpleNamespace(worker_threads=lambda: 8))
    monkeypatch.setitem(sys.modules, "plexora.server", types.ModuleType("plexora.server"))
    monkeypatch.setitem(sys.modules, "plexora.server.models", models)
    for name in ("PLEXORA_DESKTOP", "PLEXORA_AUTH_TOKEN", "PLEXORA_DATA_PATH",
                 "PLEXORA_PLUGINS", "PLEXORA_DESKTOP_LOG", cli.REEXEC_ENV_VAR):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("desktop mode must not detect or open a browser")

    monkeypatch.setattr(cli, "detect_environment", forbidden)
    monkeypatch.setattr(cli, "should_open_browser", forbidden)
    monkeypatch.setattr(cli, "_schedule_browser_open", forbidden)
    rig.app = app
    yield rig


def run(rig, argv):
    """`cli.main(argv)` with stdout and stderr captured into the rig.

    Swapped here rather than in the fixture: pytest rebinds both streams to
    its own capture when the test body starts, which would undo a fixture's
    patch before `main` ever ran.
    """
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = rig.stdout, rig.stderr
    try:
        return cli.main(argv)
    finally:
        sys.stdout, sys.stderr = saved


def _ready(rig):
    lines = rig.stdout.getvalue().splitlines()
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def test_the_ready_line_is_the_only_stdout_and_has_exactly_the_documented_keys(rig):
    assert run(rig, ["--desktop", "--port", "0"]) == 0

    ready = _ready(rig)
    assert set(ready) == READY_KEYS
    assert ready["event"] == "ready"
    assert ready["protocol"] == cli.DESKTOP_PROTOCOL
    assert ready["host"] == "127.0.0.1"
    assert ready["origin"] == f"http://127.0.0.1:{ready['port']}"
    assert ready["url"] == f"{ready['origin']}/?token={ready['token']}"
    assert ready["pid"] == os.getpid()
    assert ready["shutdown"][0] == "stdin"


def test_the_announce_comes_after_the_bind_and_before_serving(rig):
    run(rig, ["--desktop", "--port", "0"])
    assert rig.events.index("prime") < rig.events.index("bind") < rig.events.index("run")


def test_the_token_is_per_launch_and_reaches_the_app(rig):
    run(rig, ["--desktop", "--port", "0"])
    first = _ready(rig)["token"]
    assert rig.app.config["PLEXORA_AUTH_TOKEN"] == first
    assert rig.app.config["PLEXORA_DESKTOP"] is True
    assert os.environ["PLEXORA_DESKTOP"] == "1"

    rig.stdout.seek(0)
    rig.stdout.truncate()
    run(rig, ["--desktop", "--port", "0"])
    assert _ready(rig)["token"] != first


def test_notices_go_to_stderr_never_stdout(rig):
    rig.notices.append("Plexora will keep your projects in: somewhere")
    run(rig, ["--desktop", "--port", "0"])
    assert "will keep your projects" in rig.stderr.getvalue()
    assert "will keep your projects" not in rig.stdout.getvalue()


def test_the_life_is_tied_to_stdin_and_the_polite_signals(rig):
    run(rig, ["--desktop", "--port", "0"])
    assert len(rig.watched) == 1
    assert rig.signals == [True]
    assert "streams" in rig.events


def test_the_server_is_closed_on_the_way_out(rig):
    run(rig, ["--desktop", "--port", "0"])
    server, kwargs = rig.created[0]
    assert server.closed
    assert kwargs["threads"] == 8


@pytest.mark.parametrize("flag", ["--remote", "--ood"])
def test_it_refuses_the_flags_that_mean_somebody_else_is_watching(rig, flag):
    assert run(rig, ["--desktop", flag]) == 2
    assert rig.stdout.getvalue() == ""


def test_the_host_and_browser_flags_are_ignored_out_loud(rig):
    run(rig, ["--desktop", "--port", "0", "--host", "0.0.0.0", "--browser"])
    assert _ready(rig)["host"] == "127.0.0.1"
    assert "ignores --host, --browser" in rig.stderr.getvalue()


def test_an_explicit_port_is_honoured(rig):
    free = cli._probe_bind("127.0.0.1", 0)
    run(rig, ["--desktop", "--port", str(free)])
    assert _ready(rig)["port"] == free


def test_an_explicit_busy_port_fails_before_any_announce(rig):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        assert run(rig, ["--desktop", "--port", str(busy)]) == 2
    assert rig.stdout.getvalue() == ""
    assert f"Port {busy}" in rig.stderr.getvalue()


def test_the_preferred_port_gives_way_when_taken(rig, monkeypatch):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        monkeypatch.setattr(cli, "DESKTOP_PORT", busy)
        assert run(rig, ["--desktop"]) == 0
    port = _ready(rig)["port"]
    assert port not in (busy, 0)


def test_the_data_dir_and_plugins_flags_still_apply(rig, tmp_path):
    # As the child of the one `--plugins` re-exec, so this process is not
    # replaced by a real server waiting on a stdin nobody will close.
    os.environ[cli.REEXEC_ENV_VAR] = "1"
    run(rig, ["--desktop", "--port", "0", "--data-dir", str(tmp_path / "d"),
              "--plugins", "roi"])
    assert os.environ["PLEXORA_DATA_PATH"] == str(tmp_path / "d")
    assert os.environ["PLEXORA_PLUGINS"] == "roi"


def test_a_datasource_opens_directly(rig):
    run(rig, ["--desktop", "--port", "0", "my slide"])
    ready = _ready(rig)
    assert ready["url"] == f"{ready['origin']}/my%20slide?token={ready['token']}"


def test_a_conflicted_data_root_is_exit_two_on_stderr(rig):
    def refuse():
        raise RuntimeError("Two data directories hold Plexora work.")

    sys.modules["plexora"].paths.first_run_notice = refuse
    assert run(rig, ["--desktop", "--port", "0"]) == 2
    assert "Two data directories" in rig.stderr.getvalue()
    assert rig.stdout.getvalue() == ""


def test_desktop_counts_as_an_answer_to_where_am_i():
    assert "--desktop" in cli.DETECTION_OVERRIDES
    assert not cli.should_detect(["--desktop"], env={})


def test_ready_line_is_ascii_sorted_json():
    line = cli.ready_line(host="127.0.0.1", port=8420, token="t", pid=7,
                          version="1.2.3", data_root="C:/Données/plexora")
    assert line.isascii()
    decoded = json.loads(line)
    assert list(decoded) == sorted(decoded)
    assert decoded["data_root"] == "C:/Données/plexora"
    assert "\n" not in line


def test_the_desktop_socket_refuses_a_port_somebody_is_serving_on():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        assert cli._desktop_socket("127.0.0.1", busy) is None
    sock = cli._desktop_socket("127.0.0.1", 0)
    try:
        assert sock is not None and sock.getsockname()[1] > 0
    finally:
        sock.close()
