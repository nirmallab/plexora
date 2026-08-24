"""The notebook sidecar: what it launches, what it reuses, what it gives up on.

No real server is started. What matters here is the plumbing around it -- the
environment the child is handed (which is the only channel that reaches its
import-time plugin registration), the cache that decides whether to start one
at all, and the failure path, which used to be a 30-second wait ending in the
one explanation that was not true.
"""

import types

import pytest

from plexora import jupyter
from plexora.notebook_env import COLAB


class FakeProcess:
    def __init__(self, exit_code=None):
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def clean_registry():
    """Never let a fake process reach the real atexit teardown."""
    jupyter._SERVERS.clear()
    yield
    jupyter._SERVERS.clear()


@pytest.fixture
def recorder(monkeypatch):
    """Records what would have been spawned; nothing actually is."""
    calls = types.SimpleNamespace(commands=[], envs=[])

    def popen(cmd, env=None, **kwargs):
        calls.commands.append(cmd)
        calls.envs.append(env or {})
        return FakeProcess()

    monkeypatch.setattr(jupyter, "subprocess", types.SimpleNamespace(Popen=popen))
    monkeypatch.setattr(jupyter, "_wait_until_ready",
                        lambda port, timeout=30, process=None: None)
    return calls


def _direct(monkeypatch):
    monkeypatch.setattr(jupyter, "resolve_display",
                        lambda proxy, base_url: ("", "http://127.0.0.1:{port}"))


def _proxied(monkeypatch, prefix="/user/me/"):
    mount = f"{prefix}proxy/{{port}}"
    monkeypatch.setattr(jupyter, "resolve_display",
                        lambda proxy, base_url: (mount, mount))


# -- what the child is told ----------------------------------------------


def test_the_child_gets_the_settings_that_only_reach_it_as_environment(recorder,
                                                                      monkeypatch,
                                                                      tmp_path):
    _direct(monkeypatch)
    jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    env = recorder.envs[0]
    assert env["PLEXORA_DATA_PATH"] == str(tmp_path.resolve())
    assert env["PLEXORA_NOTEBOOK_MODE"] == "1"


def test_plugins_reach_the_child_as_both_a_flag_and_a_variable(recorder,
                                                              monkeypatch, tmp_path):
    """The variable is the one that counts -- Blueprint registration happens
    during the child's first `import plexora`, before argv is read."""
    _direct(monkeypatch)
    jupyter.PlexoraViewer("tonsil", data_dir=tmp_path, plugins="gating")

    assert recorder.envs[0]["PLEXORA_PLUGINS"] == "gating"
    assert "--plugins" in recorder.commands[0]


def test_unset_plugins_are_not_sent_at_all(recorder, monkeypatch, tmp_path):
    """Unset means "everything installed"; "" means a core-only build. Sending
    "" for the former would silently disable every tool."""
    _direct(monkeypatch)
    jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert "--plugins" not in recorder.commands[0]
    assert "PLEXORA_PLUGINS" not in recorder.envs[0]


# -- the sidecar cache ---------------------------------------------------


def test_a_second_viewer_reuses_the_first_sidecar(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    first = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)
    second = jupyter.PlexoraViewer("spleen", data_dir=tmp_path)

    assert len(recorder.commands) == 1
    assert first._port == second._port


def test_reuse_works_in_proxy_mode_too(recorder, monkeypatch, tmp_path):
    """The bug this fixes: the cache key embedded the base URL, which in proxy
    mode contains the freshly chosen port -- so the lookup could never hit and
    every view() in a hosted notebook spawned another server. Harmless while
    proxying was opt-in; not harmless now that it is reached by default."""
    _proxied(monkeypatch)
    first = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)
    second = jupyter.PlexoraViewer("spleen", data_dir=tmp_path)

    assert len(recorder.commands) == 1
    assert first._port == second._port


def test_a_different_data_directory_gets_its_own_sidecar(recorder, monkeypatch,
                                                        tmp_path):
    _direct(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)
    jupyter.PlexoraViewer("tonsil", data_dir=other)

    assert len(recorder.commands) == 2


def test_a_dead_sidecar_is_replaced(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)
    for process in jupyter._SERVERS.values():
        process.returncode = 1

    jupyter.PlexoraViewer("spleen", data_dir=tmp_path)
    assert len(recorder.commands) == 2


# -- failing fast --------------------------------------------------------


def test_a_child_that_exits_is_reported_immediately_with_the_likely_cause():
    """Previously indistinguishable from a slow start, so the cell sat for 30
    seconds and then blamed a timeout."""
    dead = FakeProcess(exit_code=1)

    with pytest.raises(jupyter.ServerStartError) as excinfo:
        jupyter._wait_until_ready(9, timeout=30, process=dead)

    message = str(excinfo.value)
    assert "exited with code 1" in message
    assert "pip install -e ." in message


def test_without_a_process_to_watch_it_still_times_out(monkeypatch):
    monkeypatch.setattr(jupyter.time, "sleep", lambda seconds: None)
    with pytest.raises(jupyter.ServerStartError) as excinfo:
        jupyter._wait_until_ready(9, timeout=0.05)
    assert "did not become ready" in str(excinfo.value)


def test_a_lost_port_race_is_retried_once(monkeypatch, tmp_path):
    """The gap between _free_port() letting a port go and the child binding it
    is real on a busy machine, and losing it is entirely recoverable."""
    attempts = []

    def spawn(data_dir, base_url, port, plugins):
        attempts.append(port)
        if len(attempts) == 1:
            raise jupyter.ServerStartError("address in use")
        return FakeProcess()

    monkeypatch.setattr(jupyter, "_spawn_server", spawn)
    port, _base = jupyter._start_server(tmp_path, "")

    assert len(attempts) == 2
    assert port == attempts[1]


def test_a_pinned_port_is_not_retried(monkeypatch, tmp_path):
    attempts = []

    def spawn(data_dir, base_url, port, plugins):
        attempts.append(port)
        raise jupyter.ServerStartError("address in use")

    monkeypatch.setattr(jupyter, "_spawn_server", spawn)
    with pytest.raises(jupyter.ServerStartError):
        jupyter._start_server(tmp_path, "", port=8123)

    assert attempts == [8123]


# -- the URL the viewer shows --------------------------------------------


def test_a_direct_viewer_builds_a_localhost_url(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert viewer.url == f"http://127.0.0.1:{viewer._port}/tonsil"


def test_a_proxied_viewer_builds_a_path_only_url(recorder, monkeypatch, tmp_path):
    """Path-only on purpose: an iframe resolves it against the notebook page's
    own origin, which is the origin holding the user's auth cookie."""
    _proxied(monkeypatch)
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert viewer.url == f"/user/me/proxy/{viewer._port}/tonsil"


def test_a_project_name_with_spaces_is_quoted(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    viewer = jupyter.PlexoraViewer("Tonsil 2", data_dir=tmp_path)

    assert viewer.url.endswith("/Tonsil%202")


def test_the_iframe_points_at_the_same_url(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert f'src="{viewer.url}"' in viewer._repr_html_()


def test_open_prints_a_path_only_url_instead_of_launching_a_browser(
    recorder, monkeypatch, tmp_path, capsys
):
    """There is nothing webbrowser could do with "/user/me/proxy/8123/tonsil",
    and the browser on this machine is the wrong one anyway."""
    _proxied(monkeypatch)
    monkeypatch.setattr(jupyter.webbrowser, "open",
                        lambda url: pytest.fail("should not have opened a browser"))
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert viewer.open() == viewer.url
    assert viewer.url in capsys.readouterr().out


def test_open_launches_a_browser_for_a_real_origin(recorder, monkeypatch, tmp_path):
    _direct(monkeypatch)
    opened = []
    monkeypatch.setattr(jupyter.webbrowser, "open", opened.append)
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    viewer.open()
    assert opened == [viewer.url]


# -- Colab ---------------------------------------------------------------


def test_a_colab_viewer_uses_the_origin_the_frontend_reported(recorder, monkeypatch,
                                                              tmp_path):
    monkeypatch.setattr(jupyter, "resolve_display", lambda proxy, base_url: ("", COLAB))
    monkeypatch.setattr(jupyter, "colab_origin",
                        lambda port: "https://abc-8123.googleusercontent.com")
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert viewer.url == "https://abc-8123.googleusercontent.com/tonsil"


def test_colab_without_a_frontend_falls_back_to_its_own_iframe_helper(
    recorder, monkeypatch, tmp_path
):
    """colab_origin needs a live frontend to answer. The helper does not -- it
    emits Javascript that resolves the port in the browser instead."""
    monkeypatch.setattr(jupyter, "resolve_display", lambda proxy, base_url: ("", COLAB))
    monkeypatch.setattr(jupyter, "colab_origin", lambda port: None)
    served = []
    monkeypatch.setattr(
        jupyter.PlexoraViewer, "_colab_iframe",
        lambda self: served.append(self._port) or "iframe",
    )
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    assert viewer.iframe() == "iframe"
    assert served == [viewer._port]


def test_url_says_why_it_cannot_answer_rather_than_returning_a_broken_one(
    recorder, monkeypatch, tmp_path
):
    monkeypatch.setattr(jupyter, "resolve_display", lambda proxy, base_url: ("", COLAB))
    monkeypatch.setattr(jupyter, "colab_origin", lambda port: None)
    viewer = jupyter.PlexoraViewer("tonsil", data_dir=tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        viewer.url
    assert "iframe()" in str(excinfo.value)


# -- the jupyter-server-proxy launcher tile ------------------------------


def test_the_launcher_tile_does_not_disable_every_plugin(monkeypatch):
    """The bug: `--plugins ""` was passed unconditionally, and server_cli.py
    writes the flag straight into PLEXORA_PLUGINS, where "" is not "unset" --
    it is the deliberate core-only build. Every launch from the JupyterHub
    tile came up with no tools at all."""
    from plexora import proxy

    monkeypatch.delenv("PLEXORA_PLUGINS", raising=False)
    spec = proxy.setup_plexora()

    assert "--plugins" not in spec["command"]
    assert "PLEXORA_PLUGINS" not in spec["environment"]


def test_the_launcher_tile_passes_plugins_when_they_are_set(monkeypatch):
    from plexora import proxy

    monkeypatch.setenv("PLEXORA_PLUGINS", "gating")
    spec = proxy.setup_plexora()

    command = spec["command"]
    assert command[command.index("--plugins") + 1] == "gating"
    assert spec["environment"]["PLEXORA_PLUGINS"] == "gating"


def test_an_explicit_core_only_build_still_reaches_the_child(monkeypatch):
    from plexora import proxy

    monkeypatch.setenv("PLEXORA_PLUGINS", "")
    spec = proxy.setup_plexora()

    command = spec["command"]
    assert command[command.index("--plugins") + 1] == ""
    assert spec["environment"]["PLEXORA_PLUGINS"] == ""
