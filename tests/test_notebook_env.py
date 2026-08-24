"""Which URL a notebook viewer should use, per kind of notebook.

Every branch here corresponds to a place `plexora.view()` used to render a
blank box: an iframe pointing at 127.0.0.1 on a machine that is not the one
with the browser on it. The environments are simulated -- a hub is three
environment variables, Colab is a module in sys.modules -- because the real
ones cannot be reached from a test.

The last case is the one to be careful with: plain local Jupyter and VS Code
Remote must keep the direct localhost URL. VS Code forwards the port itself, so
"helpfully" proxying it would break a setup that works today.
"""

import sys
import types

import pytest

from plexora import notebook_env
from plexora.notebook_env import COLAB, PORT_PLACEHOLDER, resolve_display


HUB = {"JUPYTERHUB_SERVICE_PREFIX": "/user/me/"}
SSH = {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}
OOD = {"SLURM_JOB_ID": "4242"}

#: Captured before the autouse fixture below replaces it, so the tests whose
#: subject IS the detector can still reach the real one.
_REAL_DISCOVER = notebook_env.discover_jupyter_prefix


@pytest.fixture(autouse=True)
def no_real_jupyter(monkeypatch):
    """Nothing discovers a server unless a test says so.

    Without this the answer would depend on whether the developer happens to
    have a jupyter lab running, which is the definition of a flaky test.
    """
    monkeypatch.setattr(notebook_env, "discover_jupyter_prefix",
                        lambda env=None, echo=print: None)
    monkeypatch.setattr(notebook_env, "in_colab", lambda: False)
    monkeypatch.setattr(notebook_env, "proxy_hint_if_missing", lambda echo=print: None)


def _with_prefix(monkeypatch, prefix):
    monkeypatch.setattr(notebook_env, "discover_jupyter_prefix",
                        lambda env=None, echo=print: prefix)


def _in_colab(monkeypatch, origin="https://abc-8123.googleusercontent.com"):
    monkeypatch.setattr(notebook_env, "in_colab", lambda: True)
    monkeypatch.setattr(notebook_env, "colab_origin", lambda port: origin)


# -- the ladder -----------------------------------------------------------


def test_local_jupyter_gets_a_direct_localhost_url():
    server, display = resolve_display("auto", None, 8123, env={})
    assert (server, display) == ("", "http://127.0.0.1:8123")


def test_a_local_kernel_is_not_proxied_even_with_a_server_running(monkeypatch):
    """VS Code Remote's case, and plain `jupyter lab` on a laptop. A prefix
    exists but there is no evidence the browser is elsewhere."""
    _with_prefix(monkeypatch, "/")
    server, display = resolve_display("auto", None, 8123, env={})
    assert display == "http://127.0.0.1:8123"


def test_jupyterhub_is_proxied_under_the_users_prefix(monkeypatch):
    _with_prefix(monkeypatch, "/user/me/")
    server, display = resolve_display("auto", None, 8123, env=HUB)
    assert (server, display) == ("/user/me/proxy/8123", "/user/me/proxy/8123")


def test_open_ondemand_is_recognised_by_its_scheduler_job(monkeypatch):
    """OOD sets no hub variable of its own -- it runs Jupyter inside a batch
    job, and the job variables are the only evidence there is."""
    _with_prefix(monkeypatch, "/node/compute-a-16/8888/")
    server, display = resolve_display("auto", None, 8123, env=OOD)
    assert display == "/node/compute-a-16/8888/proxy/8123"


def test_a_kernel_reached_over_ssh_is_proxied(monkeypatch):
    _with_prefix(monkeypatch, "/")
    _server, display = resolve_display("auto", None, 8123, env=SSH)
    assert display == "/proxy/8123"


def test_colab_beats_a_hub_prefix(monkeypatch):
    """Colab sets nothing hub-like, but if a stray variable were inherited the
    origin form is still the only one that works there."""
    _in_colab(monkeypatch)
    _with_prefix(monkeypatch, "/user/me/")
    server, display = resolve_display("auto", None, 8123, env=HUB)
    assert (server, display) == ("", COLAB)


def test_proxy_true_forces_the_proxied_form_with_no_remote_markers(monkeypatch):
    _with_prefix(monkeypatch, "/user/me/")
    _server, display = resolve_display(True, None, 8123, env={})
    assert display == "/user/me/proxy/8123"


def test_proxy_false_forces_direct_even_on_a_hub(monkeypatch):
    """The pre-existing default, which notebooks still pass explicitly."""
    _with_prefix(monkeypatch, "/user/me/")
    _in_colab(monkeypatch)
    server, display = resolve_display(False, None, 8123, env=HUB)
    assert (server, display) == ("", "http://127.0.0.1:8123")


def test_an_explicit_prefix_beats_everything(monkeypatch):
    _in_colab(monkeypatch)
    server, display = resolve_display("auto", "/custom/", 8123, env=HUB)
    assert (server, display) == ("/custom/proxy/8123", "/custom/proxy/8123")


def test_an_explicit_full_origin_leaves_the_server_at_the_root():
    """A bespoke reverse proxy or a tunnel the user set up themselves: the
    display is an origin, but the server still mounts at "/"."""
    server, display = resolve_display("auto", "https://plexora.lab.edu/", 8123, env={})
    assert (server, display) == ("", "https://plexora.lab.edu")


def test_the_port_placeholder_survives_resolution(monkeypatch):
    """The caller asks before it has a port, so it can decide whether an
    already-running sidecar will do."""
    _with_prefix(monkeypatch, "/user/me/")
    server, display = resolve_display(True, None, PORT_PLACEHOLDER, env=HUB)
    assert server == display == "/user/me/proxy/{port}"


# -- the individual detectors --------------------------------------------


@pytest.mark.parametrize("env", [SSH, OOD, {"SSH_TTY": "/dev/pts/1"},
                                 {"PBS_JOBID": "1"}, {"LSB_JOBID": "1"}])
def test_looks_remote(env):
    assert notebook_env.looks_remote(env)


@pytest.mark.parametrize("env", [{}, {"HOME": "/home/me"}])
def test_does_not_look_remote(env):
    assert not notebook_env.looks_remote(env)


def _plant_running_servers(monkeypatch, servers):
    fake = types.ModuleType("jupyter_server.serverapp")
    fake.list_running_servers = lambda: servers
    monkeypatch.setitem(sys.modules, "jupyter_server", types.ModuleType("jupyter_server"))
    monkeypatch.setitem(sys.modules, "jupyter_server.serverapp", fake)


def test_the_hub_prefix_variable_is_used_directly():
    assert _REAL_DISCOVER(HUB) == "/user/me/"


def test_a_running_server_is_asked_when_there_is_no_hub_variable(monkeypatch):
    """How Open OnDemand is found: it invents a prefix per job and advertises
    it nowhere the kernel's environment can see."""
    _plant_running_servers(monkeypatch, [{"base_url": "/node/x/8888/"}])

    assert _REAL_DISCOVER({}) == "/node/x/8888/"


def test_several_running_servers_pick_the_first_and_say_so(monkeypatch):
    _plant_running_servers(monkeypatch, [{"base_url": "/a/"}, {"base_url": "/b/"}])
    said = []

    assert _REAL_DISCOVER({}, echo=said.append) == "/a/"
    assert said and "/a/" in said[0]


def test_no_jupyter_at_all_is_not_an_error(monkeypatch):
    """`import plexora` must work on a machine with no Jupyter installed, so
    every optional import here is guarded and returns None."""
    monkeypatch.setattr(notebook_env, "_module_available", lambda name: False)
    assert _REAL_DISCOVER({}) is None


def test_checking_for_a_module_never_raises(monkeypatch):
    """importlib.util.find_spec does not return None for a module that is in
    sys.modules with a `__spec__` of None -- it raises ValueError. Hubs inject
    modules like that, and so does this file's own simulation, and nothing on
    a notebook's display path may raise."""
    planted = types.ModuleType("plexora_fake_module_for_test")
    assert planted.__spec__ is None
    monkeypatch.setitem(sys.modules, "plexora_fake_module_for_test", planted)

    assert notebook_env._module_available("plexora_fake_module_for_test")
    assert not notebook_env._module_available("plexora_definitely_not_installed")
    # The ModuleNotFoundError case: a missing parent package.
    assert not notebook_env._module_available("plexora_no_such_parent.child")


def test_colab_origin_is_none_without_a_frontend(monkeypatch):
    """eval_js runs Javascript in the notebook FRONTEND and waits for a reply,
    so under "Run all" or a reconnect there is nobody to answer. The viewer's
    iframe fallback depends on this being None rather than an exception."""
    output = types.ModuleType("google.colab.output")

    def boom(_script):
        raise RuntimeError("no frontend")

    output.eval_js = boom
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.colab", types.ModuleType("google.colab"))
    monkeypatch.setitem(sys.modules, "google.colab.output", output)

    assert notebook_env.colab_origin(8123) is None


def test_colab_origin_returns_the_proxy_origin(monkeypatch):
    output = types.ModuleType("google.colab.output")
    asked = []
    output.eval_js = lambda script: (
        asked.append(script) or "https://abc-8123.googleusercontent.com/"
    )
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.colab", types.ModuleType("google.colab"))
    monkeypatch.setitem(sys.modules, "google.colab.output", output)

    assert notebook_env.colab_origin(8123) == "https://abc-8123.googleusercontent.com"
    assert "proxyPort(8123)" in asked[0]


def test_a_non_origin_answer_from_colab_is_refused(monkeypatch):
    """Whatever came back, prefixing it with "/" would produce a path that
    fails far from here."""
    output = types.ModuleType("google.colab.output")
    output.eval_js = lambda script: "/not/an/origin"
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.colab", types.ModuleType("google.colab"))
    monkeypatch.setitem(sys.modules, "google.colab.output", output)

    assert notebook_env.colab_origin(8123) is None
