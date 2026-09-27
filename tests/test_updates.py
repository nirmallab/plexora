"""Help > Check for Updates: plexora/updates.py and update_routes.py.

Nothing here touches the network or runs pip: the index is a dict handed to
`fetch`, the install job gets a fake Popen, and the restart re-exec is an
injected `relaunch`. Settings land in the per-test settings file conftest
already points `paths.settings_path` at.
"""

import io
import json
import types

import pytest

import plexora
from plexora import _lifetime, cli, updates
from plexora.server.models import layer_jobs
from plexora.server.routes import update_routes


# -- versions -------------------------------------------------------------------


@pytest.mark.parametrize("newer, older", [
    ("0.0.25", "0.0.24"),
    ("0.1.0", "0.0.99"),
    ("1.0.0", "1.0.0rc2"),
    ("1.0.0rc2", "1.0.0rc1"),
    ("1.0.0rc1", "1.0.0b3"),
    ("1.0.0a1", "1.0.0.dev4"),
    ("1.0.0.post1", "1.0.0"),
    ("0.0.10", "0.0.9"),
])
def test_version_order(newer, older):
    assert updates.is_newer(newer, older)
    assert not updates.is_newer(older, newer)


def test_equal_versions_spelled_differently_are_not_newer():
    assert not updates.is_newer("1.0", "1.0.0")
    assert not updates.is_newer("v1.0.0", "1.0.0")


def test_a_checkout_with_no_version_is_never_behind():
    """Offering a wheel over something that cannot be named is how an
    editable install gets replaced by a copy."""
    assert not updates.is_newer("9.9.9", None)
    assert not updates.is_newer("9.9.9", "unknown (source checkout)")


def _release(yanked=False):
    return [{"filename": "plexora.whl", "yanked": yanked}]


def test_latest_skips_yanked_empty_and_prereleases():
    payload = {"info": {"version": "0.0.26"}, "releases": {
        "0.0.24": _release(),
        "0.0.25": _release(),
        "0.0.26": _release(yanked=True),   # yanked: pip will not pick it
        "0.0.27": [],                      # a name with no files
        "0.1.0rc1": _release(),            # pre-release
    }}
    assert updates.latest_from_index(payload) == "0.0.25"
    assert updates.latest_from_index(payload, include_prereleases=True) == "0.1.0rc1"


def test_latest_falls_back_to_info_version_without_releases():
    assert updates.latest_from_index({"info": {"version": "0.0.30"}}) == "0.0.30"
    assert updates.latest_from_index({}) is None


def test_fetch_latest_reads_the_override_and_caches(monkeypatch, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"releases": {"0.0.99": _release()}}), encoding="utf-8")
    monkeypatch.setenv("PLEXORA_UPDATE_INDEX", str(index))
    updates._reset_cache_for_tests()
    calls = []

    def fetch(location, **_):
        calls.append(location)
        if location.startswith("https://api.github.com"):
            return {"body": "Faster tiles.", "html_url": "https://example.test/notes"}
        return updates._read_json(location)

    first = updates.fetch_latest(fetch=fetch)
    assert first == {"latest": "0.0.99", "notes": "Faster tiles.",
                     "notes_url": "https://example.test/notes"}
    assert calls[0] == str(index)
    assert updates.fetch_latest(fetch=fetch) == first
    assert len(calls) == 2          # the index and the notes, once each
    updates.fetch_latest(fetch=fetch, force=True)
    assert len(calls) == 4
    updates._reset_cache_for_tests()


def test_release_notes_are_optional():
    def fetch(location, **_):
        raise OSError("rate limited")

    notes, url = updates.release_notes("1.2.3", fetch=fetch)
    assert notes == ""
    assert url.endswith("/releases/tag/v1.2.3")


def test_current_version_is_captured_once(monkeypatch):
    """After pip runs, the metadata on disk names the new version while the
    running modules are the old ones -- so the running answer is cached."""
    updates.current_version.cache_clear()
    monkeypatch.setattr(updates, "_metadata_version", lambda: "0.0.24")
    assert updates.current_version() == "0.0.24"
    monkeypatch.setattr(updates, "_metadata_version", lambda: "0.0.25")
    assert updates.current_version() == "0.0.24"
    assert updates.installed_on_disk() == "0.0.25"
    updates.current_version.cache_clear()


# -- this install -----------------------------------------------------------------


class _Dist:
    def __init__(self, direct_url=None, site="/nowhere"):
        self._direct = direct_url
        self._site = site

    def read_text(self, name):
        return json.dumps(self._direct) if self._direct is not None else None

    def locate_file(self, _):
        return self._site


def test_install_kind_for_each_case(tmp_path):
    common = dict(container=False, uv=None)
    assert updates.install_kind(desktop=True)["kind"] == "desktop"
    assert updates.install_kind(dist=None, **common)["kind"] == "editable"
    editable = _Dist({"url": "file:///src", "dir_info": {"editable": True}})
    assert updates.install_kind(dist=editable, **common)["kind"] == "editable"
    assert updates.install_kind(dist=_Dist(), container=True, uv=None)["kind"] == "container"
    readonly = updates.install_kind(dist=_Dist(), writable=False, **common)
    assert readonly["kind"] == "readonly" and not readonly["can_install"]
    no_pip = updates.install_kind(dist=_Dist(), writable=True, has_pip=False, **common)
    assert no_pip["kind"] == "no_pip" and not no_pip["can_install"]
    with_uv = updates.install_kind(dist=_Dist(), writable=True, has_pip=False,
                                   container=False, uv="/usr/bin/uv")
    assert with_uv["can_install"] and with_uv["uv"] == "/usr/bin/uv"
    pip = updates.install_kind(dist=_Dist(), writable=True, has_pip=True, **common)
    assert pip == {"kind": "pip", "can_install": True, "uv": None, "reason": ""}


def test_every_refusal_says_why():
    for kwargs in (dict(desktop=True), dict(dist=None),
                   dict(dist=_Dist(), container=True),
                   dict(dist=_Dist(), container=False, writable=False)):
        kind = updates.install_kind(**kwargs)
        assert not kind["can_install"] and kind["reason"]


def test_installed_extras_are_the_complete_ones():
    requires = [
        "numpy>=2",
        'pyarrow>=14; extra == "spatial"',
        'paramiko; extra == "remote"',
        'asyncssh; extra == "remote"',
        'pytest; extra == "dev"',
    ]
    present = {"pyarrow", "paramiko", "pytest"}
    assert updates.installed_extras(requires, installed=present.__contains__) == ["spatial"]
    present.add("asyncssh")
    assert updates.installed_extras(requires, installed=present.__contains__) == ["remote", "spatial"]


def test_pip_command_pins_the_version_and_keeps_extras():
    command = updates.pip_command("0.0.25", extras=["wsi", "spatial"], python="/py")
    assert command == ["/py", "-m", "pip", "install", "--disable-pip-version-check",
                       "--upgrade-strategy", "only-if-needed", "plexora[spatial,wsi]==0.0.25"]
    assert updates.pip_command("0.0.25", extras=[], python="/py", uv="/uv") == [
        "/uv", "pip", "install", "--python", "/py", "plexora==0.0.25"]


def test_manual_command_for_a_checkout_is_git():
    assert "git pull" in updates.manual_command("1.0.0", {"kind": "editable"})


# -- the job ------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, lines, code):
        self.stdout = io.StringIO("".join(f"{line}\n" for line in lines))
        self._code = code

    def wait(self):
        return self._code


def test_install_job_captures_output_and_outcome():
    updates._reset_job_for_tests()
    seen = {}

    def popen(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return _FakeProcess(["Collecting plexora==0.0.25", "Successfully installed"], 0)

    job = updates.start_install("0.0.25", ["pip", "install", "x"], popen=popen)
    job.thread.join(5)
    view = job.snapshot()
    assert view["state"] == "done" and view["returncode"] == 0
    assert view["log_tail"][-1] == "Successfully installed"
    assert seen["env"]["PIP_NO_INPUT"] == "1"

    failing = updates.start_install("0.0.25", ["pip"], popen=lambda *a, **k: _FakeProcess(["boom"], 1))
    failing.thread.join(5)
    assert failing.snapshot()["state"] == "failed"
    updates._reset_job_for_tests()


def test_only_one_install_runs_at_a_time():
    updates._reset_job_for_tests()
    import threading

    gate = threading.Event()

    class Slow:
        stdout = iter(())

        def wait(self):
            gate.wait(5)
            return 0

    first = updates.start_install("1", ["x"], popen=lambda *a, **k: Slow())
    assert updates.start_install("1", ["x"], popen=lambda *a, **k: Slow()) is None
    gate.set()
    first.thread.join(5)
    updates._reset_job_for_tests()


# -- routes -----------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    updates._reset_cache_for_tests()
    updates._reset_job_for_tests()
    _lifetime._reset_for_tests()
    layer_jobs.forget()
    monkeypatch.setattr(update_routes, "RUNNING_VERSION", "0.0.24")
    monkeypatch.setattr(updates, "_read_json", _fake_index({"0.0.25": _release()}))
    monkeypatch.setattr(updates, "install_kind", lambda desktop=False, **_: (
        {"kind": "desktop", "can_install": False, "uv": None, "reason": "desktop"} if desktop
        else {"kind": "pip", "can_install": True, "uv": None, "reason": ""}))
    monkeypatch.setattr(updates, "installed_extras", lambda *a, **k: [])
    # Synchronous, so a restart a test asks for runs while that test's
    # patches are still in place -- a real 0.25 s timer would fire the real
    # request_restart after teardown, into the test runner's main thread.
    monkeypatch.setattr(update_routes, "threading", types.SimpleNamespace(Timer=_NowTimer))
    monkeypatch.setattr(_lifetime, "request_restart", _refuse_real_restart)
    for key in ("PLEXORA_DESKTOP", "PLEXORA_NOTEBOOK_MODE", "PLEXORA_CAN_RESTART"):
        monkeypatch.setitem(plexora.app.config, key, False)
    yield plexora.app.test_client()
    updates._reset_cache_for_tests()
    updates._reset_job_for_tests()
    _lifetime._reset_for_tests()
    layer_jobs.forget()


class _NowTimer:
    def __init__(self, delay, fn, args=()):
        self.fn, self.args, self.daemon = fn, args, True

    def start(self):
        # Looked up on the module at call time, so a test's own patch wins.
        self.fn(*self.args)


def _refuse_real_restart(*args, **kwargs):
    return None


def _fake_index(releases):
    def fetch(location, **_):
        if location.startswith("https://api.github.com"):
            return {"body": "Notes."}
        return {"releases": releases}
    return fetch


def test_check_reports_an_available_update(client):
    body = client.get("/update/check?force=1").get_json()
    assert body["current"] == "0.0.24" and body["latest"] == "0.0.25"
    assert body["available"] and body["can_install"]
    assert body["mode"] == "browser" and body["notes"] == "Notes."
    assert "plexora==0.0.25" in body["command"]
    assert updates.read_prefs()["last_checked"]


def test_check_when_up_to_date(client, monkeypatch):
    monkeypatch.setattr(updates, "_read_json", _fake_index({"0.0.24": _release()}))
    body = client.get("/update/check?force=1").get_json()
    assert not body["available"] and not body["can_install"]


def test_offline_is_an_answer_not_an_error(client, monkeypatch):
    def fetch(location, **_):
        raise OSError("no route to host")

    monkeypatch.setattr(updates, "_read_json", fetch)
    response = client.get("/update/check?force=1")
    assert response.status_code == 200
    assert "Could not reach" in response.get_json()["error"]


def test_auto_check_uses_the_last_answer_within_a_day(client, monkeypatch):
    client.get("/update/check?force=1")

    def fetch(location, **_):
        raise AssertionError("the auto check must not reach the index again today")

    monkeypatch.setattr(updates, "_read_json", fetch)
    updates._reset_cache_for_tests()
    body = client.get("/update/check?auto=1").get_json()
    assert body["available"] and body["latest"] == "0.0.25"


def test_auto_check_off_means_off(client):
    client.post("/update/prefs", json={"auto_check": False})
    body = client.get("/update/check?auto=1").get_json()
    assert body.get("disabled") and not body["available"]


def test_skipping_a_version_marks_it(client):
    client.post("/update/prefs", json={"skipped_version": "0.0.25"})
    assert client.get("/update/check?force=1").get_json()["skipped"]
    client.post("/update/prefs", json={"skipped_version": None})
    assert not client.get("/update/check?force=1").get_json()["skipped"]


def test_install_refuses_anything_but_the_offered_version(client, monkeypatch):
    started = []
    monkeypatch.setattr(updates, "start_install", lambda *a, **k: started.append(a))
    assert client.post("/update/install", json={"version": "9.9.9"}).status_code == 409
    assert client.post("/update/install", json={}).status_code == 409
    assert started == []


def test_install_starts_the_pinned_command(client, monkeypatch):
    started = []

    class Job:
        state = "running"

        def snapshot(self):
            return {"state": "running", "version": "0.0.25", "log_tail": []}

    def start(version, command, **_):
        started.append((version, command))
        updates._job = Job()
        return updates._job

    monkeypatch.setattr(updates, "start_install", start)
    response = client.post("/update/install", json={"version": "0.0.25"})
    assert response.status_code == 202
    assert started[0][0] == "0.0.25"
    assert started[0][1][-1] == "plexora==0.0.25"
    status = client.get("/update/install/status").get_json()
    assert status["state"] == "running"


def test_the_desktop_server_never_installs(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)
    body = client.get("/update/check").get_json()
    assert body["mode"] == "desktop" and body["latest"] is None
    assert client.post("/update/install", json={"version": "0.0.25"}).status_code == 409


def test_restart_needs_a_launcher(client, monkeypatch):
    fired = []
    monkeypatch.setattr(_lifetime, "request_restart", lambda *a, **k: fired.append(a))
    assert client.post("/update/restart").status_code == 409
    monkeypatch.setitem(plexora.app.config, "PLEXORA_CAN_RESTART", True)
    assert client.post("/update/restart").status_code == 202
    assert fired == [("restarting to finish an update",)]


def test_restart_is_allowed_in_notebook_mode(client, monkeypatch):
    """Unlike Quit: exec keeps the pid the kernel's registry holds."""
    monkeypatch.setitem(plexora.app.config, "PLEXORA_NOTEBOOK_MODE", True)
    monkeypatch.setitem(plexora.app.config, "PLEXORA_CAN_RESTART", True)
    monkeypatch.setattr(_lifetime, "request_restart", lambda *a, **k: None)
    assert client.post("/shutdown").status_code == 403
    assert client.post("/update/restart").status_code == 202


def test_restart_asks_before_stopping_work(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_CAN_RESTART", True)
    monkeypatch.setattr(_lifetime, "request_restart", lambda *a, **k: None)
    with layer_jobs._lock:
        layer_jobs._jobs[("sample", "cells")] = layer_jobs._record(
            "pending", stage="building", stage_label="Building the layer")
    refused = client.post("/update/restart")
    assert refused.status_code == 409
    assert refused.get_json()["busy"] == ["Building the layer (sample)"]
    assert client.post("/update/restart", json={"force": True}).status_code == 202


def test_a_layer_nothing_can_build_is_not_work_in_progress(client):
    with layer_jobs._lock:
        layer_jobs._jobs[("sample", "t")] = layer_jobs._record("pending", stage="waiting")
    assert update_routes._busy() == []


def test_health_names_the_running_version(client):
    response = client.get("/health")
    assert response.status_code == 204
    assert response.headers["X-Plexora-Version"] == "0.0.24"


# -- the relaunch -------------------------------------------------------------------------


def test_restart_command_pins_the_port_and_opens_no_browser():
    command = cli.restart_command(["demo", "--port", "8001", "--browser", "--remote"], 8123,
                                  python="/py")
    assert command == ["/py", "-m", "plexora", "demo", "--remote",
                       "--port", "8123", "--no-browser"]
    assert cli.restart_command(["--port=9000"], 9000, python="/py") == [
        "/py", "-m", "plexora", "--port", "9000", "--no-browser"]


def test_restart_after_update_runs_teardown_then_relaunches(monkeypatch):
    order = []
    import atexit

    monkeypatch.setattr(atexit, "_run_exitfuncs", lambda: order.append("atexit"))
    monkeypatch.setattr(_lifetime, "cancel_backstop", lambda: order.append("cancel"))
    environ = {}
    code = cli.restart_after_update(["/py", "-m", "plexora"], environ=environ,
                                    relaunch=lambda command: order.append(command),
                                    log=lambda line: None)
    assert code == 0
    assert order == ["cancel", "atexit", ["/py", "-m", "plexora"]]
    assert environ[cli.RESTART_ENV_VAR] == "1"


def test_main_relaunches_after_serve_when_a_restart_was_asked(monkeypatch):
    served = {}
    relaunched = []
    fake_app = types.SimpleNamespace(config={})
    fake_waitress = types.SimpleNamespace(
        serve=lambda app, **kwargs: (served.update(kwargs), _lifetime.request_restart(
            "test", interrupt=lambda: None, grace=60)))
    fake_plexora = types.SimpleNamespace(
        app=fake_app, _lifetime=_lifetime,
        paths=types.SimpleNamespace(first_run_notice=lambda: None, data_root_notices=lambda: [],
                                    DataRootError=RuntimeError),
        _clean_base_url=lambda base_url: "")
    import sys

    monkeypatch.setitem(sys.modules, "waitress", fake_waitress)
    monkeypatch.setitem(sys.modules, "plexora", fake_plexora)
    monkeypatch.setattr(cli, "should_open_browser", lambda **kwargs: False)
    monkeypatch.setattr(cli, "should_detect", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_announce_server", lambda *a, **k: None)
    monkeypatch.setattr(cli, "restart_after_update", lambda command: relaunched.append(command) or 0)
    from plexora.server.models import data_model

    monkeypatch.setattr(data_model, "prime_hot_code", lambda *a, **k: None)
    _lifetime._reset_for_tests()
    try:
        cli.main(["--port", "0", "--no-browser"])
    finally:
        _lifetime.cancel_backstop()
        _lifetime._reset_for_tests()
    assert fake_app.config["PLEXORA_CAN_RESTART"] is True
    assert relaunched and relaunched[0][-3:] == ["--port", str(served["port"]), "--no-browser"]


def test_wait_for_port_gives_up():
    clock = iter(range(100))
    assert not cli.wait_for_port("127.0.0.1", 1, timeout=3, probe=lambda h, p: None,
                                 sleep=lambda s: None, clock=lambda: next(clock))
    assert cli.wait_for_port("127.0.0.1", 1, probe=lambda h, p: p)


def test_a_main_thread_deaf_to_sigint_is_made_interruptible():
    """`plexora &` from a script inherits SIGINT ignored, and interrupt_main
    then does nothing: Quit and the restart both fell to the hard exit."""
    import signal

    before = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        assert _lifetime.make_interruptible() is True
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert _lifetime.make_interruptible() is False
    finally:
        signal.signal(signal.SIGINT, before)
