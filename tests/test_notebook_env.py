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
import urllib.error

import pytest

from plexora import notebook_env
from plexora.notebook_env import COLAB, PORT_PLACEHOLDER, resolve_display


HUB = {"JUPYTERHUB_SERVICE_PREFIX": "/user/me/"}
SSH = {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}
OOD = {"SLURM_JOB_ID": "4242"}

#: What Open OnDemand's own prefix looks like: the portal proxies the notebook
#: to the compute node it is running on, and names both in the path.
OOD_PREFIX = "/node/compute-a-16/8888/"

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
    resolved = resolve_display("auto", None, 8123, env={})
    assert resolved == ("", "http://127.0.0.1:8123", "127.0.0.1", "direct")


def test_a_local_kernel_is_not_proxied_even_with_a_server_running(monkeypatch):
    """VS Code Remote's case, and plain `jupyter lab` on a laptop. A prefix
    exists but there is no evidence the browser is elsewhere."""
    _with_prefix(monkeypatch, "/")
    resolved = resolve_display("auto", None, 8123, env={})
    assert resolved.display == "http://127.0.0.1:8123"


def test_jupyterhub_is_proxied_under_the_users_prefix(monkeypatch):
    _with_prefix(monkeypatch, "/user/me/")
    resolved = resolve_display("auto", None, 8123, env=HUB)
    assert resolved == ("/user/me/proxy/8123", "/user/me/proxy/8123",
                        "127.0.0.1", "proxy")


def test_open_ondemand_uses_the_portals_other_door(monkeypatch):
    """The fix for the 404 that started all this. OOD's `/node/` door forwards
    the path unstripped, so a root-serving Flask app never matches; `/rnode/`
    strips it. Nothing has to be installed for either -- jupyter-server-proxy
    would have to live in the Jupyter SERVER's environment, which on OOD is an
    admin-controlled module."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    resolved = resolve_display("auto", None, 8123, env=OOD)
    assert resolved == ("/rnode/compute-a-16/8123", "/rnode/compute-a-16/8123",
                        "0.0.0.0", "ood")


def test_the_ood_route_binds_an_address_the_portal_can_reach(monkeypatch):
    """The portal's web host connects to the node over the network, so a
    loopback bind is unreachable however right the URL is."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    assert resolve_display("auto", None, 8123, env=OOD).bind_host == "0.0.0.0"

    _with_prefix(monkeypatch, "/user/me/")
    assert resolve_display("auto", None, 8123, env=HUB).bind_host == "127.0.0.1"


def test_ood_needs_no_scheduler_evidence(monkeypatch):
    """The prefix is served BY the portal, so its shape is better evidence than
    any environment variable -- and OOD sets no variable of its own."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    assert resolve_display("auto", None, 8123, env={}).kind == "ood"


def test_proxy_false_still_wins_on_ood(monkeypatch):
    """The escape hatch has to stay an escape hatch."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    resolved = resolve_display(False, None, 8123, env=OOD)
    assert resolved == ("", "http://127.0.0.1:8123", "127.0.0.1", "direct")


def test_an_explicit_base_url_still_wins_on_ood(monkeypatch):
    """For a site whose portal spells the stripping door differently: naming a
    prefix means "proxy me under it", loopback and all."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    resolved = resolve_display("auto", "/node/compute-a-16/8888/", 8123, env=OOD)
    assert resolved == ("/node/compute-a-16/8888/proxy/8123",
                        "/node/compute-a-16/8888/proxy/8123", "127.0.0.1", "explicit")


@pytest.mark.parametrize("prefix", [
    "/user/me/",
    "/node/host/",          # no port segment
    "/node/host/8888/lab/",  # something after the port
    "/node//8888/",         # no host
    "/rnode/host/8888/",    # not the door Jupyter arrives through
])
def test_prefixes_that_are_not_open_ondemand(prefix, monkeypatch):
    _with_prefix(monkeypatch, prefix)
    assert resolve_display("auto", None, 8123, env=HUB).kind != "ood"


def test_a_kernel_reached_over_ssh_is_proxied(monkeypatch):
    _with_prefix(monkeypatch, "/")
    resolved = resolve_display("auto", None, 8123, env=SSH)
    assert resolved.display == "/proxy/8123"


def test_colab_beats_a_hub_prefix(monkeypatch):
    """Colab sets nothing hub-like, but if a stray variable were inherited the
    origin form is still the only one that works there."""
    _in_colab(monkeypatch)
    _with_prefix(monkeypatch, "/user/me/")
    resolved = resolve_display("auto", None, 8123, env=HUB)
    assert resolved == ("", COLAB, "127.0.0.1", "colab")


def test_colab_beats_an_ood_prefix(monkeypatch):
    _in_colab(monkeypatch)
    _with_prefix(monkeypatch, OOD_PREFIX)
    assert resolve_display("auto", None, 8123, env=OOD).display is COLAB


def test_proxy_true_forces_the_proxied_form_with_no_remote_markers(monkeypatch):
    _with_prefix(monkeypatch, "/user/me/")
    resolved = resolve_display(True, None, 8123, env={})
    assert resolved.display == "/user/me/proxy/8123"


def test_proxy_false_forces_direct_even_on_a_hub(monkeypatch):
    """The pre-existing default, which notebooks still pass explicitly."""
    _with_prefix(monkeypatch, "/user/me/")
    _in_colab(monkeypatch)
    resolved = resolve_display(False, None, 8123, env=HUB)
    assert resolved == ("", "http://127.0.0.1:8123", "127.0.0.1", "direct")


def test_an_explicit_prefix_beats_everything(monkeypatch):
    _in_colab(monkeypatch)
    resolved = resolve_display("auto", "/custom/", 8123, env=HUB)
    assert resolved == ("/custom/proxy/8123", "/custom/proxy/8123",
                        "127.0.0.1", "explicit")


def test_an_explicit_full_origin_leaves_the_server_at_the_root():
    """A bespoke reverse proxy or a tunnel the user set up themselves: the
    display is an origin, but the server still mounts at "/"."""
    resolved = resolve_display("auto", "https://plexora.lab.edu/", 8123, env={})
    assert resolved == ("", "https://plexora.lab.edu", "127.0.0.1", "origin")


def test_the_port_placeholder_survives_resolution(monkeypatch):
    """The caller asks before it has a port, so it can decide whether an
    already-running sidecar will do."""
    _with_prefix(monkeypatch, "/user/me/")
    resolved = resolve_display(True, None, PORT_PLACEHOLDER, env=HUB)
    assert resolved.server_base == resolved.display == "/user/me/proxy/{port}"


def test_the_port_placeholder_survives_the_ood_route_too(monkeypatch):
    """The mount names OUR port, not the notebook's -- and it has to survive
    the placeholder, or the sidecar cache key would change on every call."""
    _with_prefix(monkeypatch, OOD_PREFIX)
    resolved = resolve_display("auto", None, PORT_PLACEHOLDER, env=OOD)
    assert resolved.server_base == "/rnode/compute-a-16/{port}"


def test_the_prefix_is_discovered_once(monkeypatch):
    """Discovery prints when it has to choose between several running servers.
    Asking it twice -- once to test the shape, once to use it -- would print
    that note twice for one view()."""
    calls = []
    monkeypatch.setattr(notebook_env, "discover_jupyter_prefix",
                        lambda env=None, echo=print: calls.append(env) or OOD_PREFIX)
    resolve_display("auto", None, 8123, env=OOD)
    assert len(calls) == 1


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


# -- asking the SERVER whether it can proxy -------------------------------


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _plant_proxy_probe(monkeypatch, outcome):
    """Answer the one HTTP request verify_proxy_route makes."""
    asked = []

    def urlopen(url, timeout=None):
        asked.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(notebook_env.urllib.request, "urlopen", urlopen)
    return asked


ONE_SERVER = [{"base_url": "/user/me/", "url": "http://localhost:8888/user/me/",
               "token": "abc123"}]


def test_a_server_that_proxies_the_port_says_nothing(monkeypatch):
    _plant_running_servers(monkeypatch, ONE_SERVER)
    asked = _plant_proxy_probe(monkeypatch, 200)
    said = []

    notebook_env.verify_proxy_route("/user/me/", 8123, echo=said.append)

    assert said == []
    assert asked == ["http://localhost:8888/user/me/proxy/8123/config?token=abc123"]


def test_a_404_names_the_environment_that_actually_needs_the_extension(monkeypatch):
    """The bug this exists for: the old hint looked at the KERNEL's environment,
    which on a hub is routinely not the server's -- so it stayed silent in
    exactly the case where the URL about to be displayed would 404."""
    _plant_running_servers(monkeypatch, ONE_SERVER)
    _plant_proxy_probe(monkeypatch, urllib.error.HTTPError(
        "http://localhost:8888/user/me/proxy/8123/config", 404, "Not Found", {}, None))
    said = []

    notebook_env.verify_proxy_route("/user/me/", 8123, echo=said.append)

    assert said and "JUPYTER SERVER" in said[0]


def test_a_probe_that_could_not_reach_the_server_says_nothing(monkeypatch):
    """A warning that fires when it does not know would be the old bug with the
    sign flipped."""
    _plant_running_servers(monkeypatch, ONE_SERVER)
    _plant_proxy_probe(monkeypatch, TimeoutError("timed out"))
    said = []

    notebook_env.verify_proxy_route("/user/me/", 8123, echo=said.append)

    assert said == []


def test_no_matching_server_entry_says_nothing(monkeypatch):
    """The hub case: the notebook server belongs to another process, so
    list_running_servers() cannot see it and proves nothing either way."""
    _plant_running_servers(monkeypatch, [])
    _plant_proxy_probe(monkeypatch, 404)
    said = []

    notebook_env.verify_proxy_route("/user/me/", 8123, echo=said.append)

    assert said == []


def test_a_tokenless_server_is_probed_without_one(monkeypatch):
    _plant_running_servers(monkeypatch, [{"base_url": "/user/me/",
                                          "url": "http://localhost:8888/user/me/",
                                          "token": ""}])
    asked = _plant_proxy_probe(monkeypatch, 200)

    notebook_env.verify_proxy_route("/user/me/", 8123, echo=lambda line: None)

    assert asked == ["http://localhost:8888/user/me/proxy/8123/config"]


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
