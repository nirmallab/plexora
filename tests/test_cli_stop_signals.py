"""A terminal `plexora` and a `plexora-server` sidecar stop on SIGTERM / SIGHUP
the way they stop on Ctrl+C.

Python's default for SIGTERM and SIGHUP is to die on the spot, without running
`atexit` -- and `atexit` is where the ssh sessions and cluster jobs a viewer
started (detached, in their own session, so they never see the terminal's
hangup) are stopped, where `nodes.json` and `servers.json` are tidied, and
`serve()` returning is what runs `--also-serve`'s teardown. A closed terminal
is a SIGHUP; a notebook kernel ending its sidecar is `process.terminate()`, a
SIGTERM. `_lifetime.install_signal_handlers` turns both into the
KeyboardInterrupt waitress already stops cleanly on; these tests check that it
is in place while each launcher serves.

Nothing here binds a port: waitress is a stand-in that records the signal
dispositions at the moment `serve()` would block.
"""

import atexit
import signal
import sys
import types

import pytest

from plexora import _lifetime, _resources, cli, server_cli  # noqa: F401
from plexora.server.models import data_model, server_records


def _stop_signals():
    names = ["SIGTERM"]
    for name in ("SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            names.append(name)
    return [(name, getattr(signal, name)) for name in names]


@pytest.fixture
def default_stop_signals():
    """Every stop signal at Python's own default, put back afterwards."""
    saved = {signum: signal.getsignal(signum) for _, signum in _stop_signals()}
    for signum in saved:
        signal.signal(signum, signal.SIG_DFL)
    _lifetime._reset_for_tests()
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
        _lifetime.cancel_backstop()
        _lifetime._reset_for_tests()


def _recording_waitress(seen):
    def serve(app, **kwargs):
        for name, signum in _stop_signals():
            seen[name] = signal.getsignal(signum)

    return types.SimpleNamespace(serve=serve)


def _fake_plexora(fake_app):
    return types.SimpleNamespace(
        app=fake_app, _lifetime=_lifetime, cli=cli,
        paths=types.SimpleNamespace(first_run_notice=lambda: None,
                                    data_root_notices=lambda: [],
                                    DataRootError=RuntimeError),
        _clean_base_url=lambda base_url: "",
        _logging=types.SimpleNamespace(configure=lambda level=None, **kwargs: None))


def _assert_stops_like_ctrl_c(seen):
    assert set(seen) == {name for name, _ in _stop_signals()}
    for name, handler in seen.items():
        assert callable(handler), (
            f"{name} is {handler!r} while serving: the process dies without atexit, "
            f"leaving detached ssh sessions, cluster jobs and nodes.json entries behind")
        with pytest.raises(KeyboardInterrupt):
            handler(getattr(signal, name), None)


def test_terminal_plexora_stops_on_sigterm_and_sighup_like_ctrl_c(
        monkeypatch, default_stop_signals):
    seen = {}
    fake_app = types.SimpleNamespace(config={})
    monkeypatch.setattr(cli, "should_open_browser", lambda **kwargs: False)
    monkeypatch.setattr(cli, "should_detect", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_announce_server", lambda *a, **k: None)
    monkeypatch.setattr(data_model, "prime_hot_code", lambda *a, **k: None)
    with monkeypatch.context() as modules:
        modules.setitem(sys.modules, "waitress", _recording_waitress(seen))
        modules.setitem(sys.modules, "plexora", _fake_plexora(fake_app))
        assert cli.main(["--port", "0", "--no-browser"]) is None

    _assert_stops_like_ctrl_c(seen)


def test_sidecar_server_stops_on_sigterm_like_ctrl_c(monkeypatch, default_stop_signals):
    """jupyter's `_cleanup_servers` ends a sidecar with `process.terminate()`."""
    seen = {}
    fake_app = types.SimpleNamespace(config={})
    monkeypatch.setattr(data_model, "prime_hot_code", lambda *a, **k: None)
    monkeypatch.setattr(server_records, "announce", lambda *a, **k: None)
    monkeypatch.setattr(cli, "start_telemetry", lambda *a, **k: None)
    monkeypatch.delenv(cli.RESTART_ENV_VAR, raising=False)
    with monkeypatch.context() as modules:
        modules.setattr(atexit, "register", lambda *a, **k: None)
        modules.setitem(sys.modules, "waitress", _recording_waitress(seen))
        modules.setitem(sys.modules, "plexora", _fake_plexora(fake_app))
        assert server_cli.main(["--port", "0"]) is None

    _assert_stops_like_ctrl_c(seen)
