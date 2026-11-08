"""Remote servers on the website: what the website's Python asks the page to run,
and how it reads the page's connections (server/models/web_remote.py,
docs/internal/WEB_REMOTE_RELAY.md).

The page's half (services/webRemote.js) is stood in for by a fake of its API,
so these run under CPython; the start script itself is run in a real shell,
against a stand-in for `plexora node serve`.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plexora.server.models import remote_sessions, web_remote
from plexora.server.models import remotes as remote_store
from plexora.server.models.remotes import Remote
from tests.remote_fixtures import cache_root  # noqa: F401
from tests.sftp_fixtures import sftp_host  # noqa: F401

O2 = dict(target="ajn16@o2.hms.harvard.edu", remote_command="/home/ajn16/miniconda3/envs/plexora",
          srun="-p interactive -t 2:00:00 -c 16 --mem 128G")


def _remote(name="HMS-O2", **fields):
    return Remote(name=name, **{**O2, **fields})


# -- what to run ------------------------------------------------------------------


def test_the_start_script_runs_the_profiles_node_detached_in_its_job():
    plan = web_remote.start_script(_remote(), port=41234)
    assert plan["user"] == "ajn16" and plan["host"] == "o2.hms.harvard.edu" and plan["port"] == 22
    assert plan["queued"] is True and plan["install"] is False
    assert plan["command"].startswith("sh -c ")
    script = shlex.split(plan["command"])[2]
    # The profile's own srun line and environment, as the desktop runs them.
    assert "srun -p interactive -t 2:00:00 -c 16 --mem 128G" in script
    assert "/home/ajn16/miniconda3/envs/plexora/bin/plexora node serve --port 41234 --host 127.0.0.1" in script
    assert "--dynamic" in script and '--relay-file "$d/$r.json"' in script
    # Detached, its log tailed; the room filled in by the page.
    assert "nohup $s " in script and 'exec tail -n +1 -F "$d/$r.log"' in script
    assert f"r={web_remote.ROOM_PLACEHOLDER}" in script
    # The relay file arrives on stdin, never on a command line.
    assert "IFS= read -r line" in script and "umask 077" in script
    # A queued job waits as long as the desktop's would.
    from plexora import connect

    assert plan["timeout"] == connect.DEFAULT_SRUN_TIMEOUT


def test_the_start_script_installs_first_when_the_profile_asks():
    plan = web_remote.start_script(_remote(install=True, srun=None), port=40000)
    script = shlex.split(plan["command"])[2]
    from plexora import connect

    assert plan["install"] is True and plan["queued"] is False
    assert connect.install_command_line(O2["remote_command"]) in script
    assert script.index(connect.INSTALL_DONE_MARK) < script.index("nohup")
    assert plan["timeout"] == connect.NODE_START_TIMEOUT + connect.INSTALL_TIMEOUT


def test_a_port_in_the_address_is_the_ssh_port():
    plan = web_remote.start_script(_remote(target="me@lab.example.org:2222", srun=None), port=40000)
    assert (plan["host"], plan["port"]) == ("lab.example.org", 2222)


@pytest.mark.parametrize("fields, words", [
    ({"target": "o2.hms.harvard.edu"}, "user name"),
    ({"jump": "bastion.example.org"}, "jump host"),
    ({"extra": {"workstation": {"os": "windows"}}}, "Windows"),
])
def test_what_the_website_cannot_connect_says_why(fields, words):
    problem = web_remote.refusal(_remote(**fields))
    assert problem and words in problem


def test_a_cluster_profile_is_connectable():
    assert web_remote.refusal(_remote()) is None


@pytest.mark.skipif(sys.platform == "win32", reason="the start script is POSIX sh")
def test_the_start_script_runs_and_its_node_outlives_it(tmp_path):
    """The script, in a real shell: the relay file lands mode 0600 where the
    node is told to read it, the node starts detached with it, and its log is
    shown -- and killing the script (SSH closing) leaves the node running."""
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "node-ran"
    fake = tmp_path / "env" / "bin" / "plexora"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n"
                    f'echo "$@" > {shlex.quote(str(marker))}\n'
                    'echo "[plexora-node] host=127.0.0.1 port=4 node_id=n token=t"\n'
                    'echo "[plexora-relay] joined room"\n'
                    "sleep 30\n")
    fake.chmod(0o755)
    plan = web_remote.start_script(_remote(remote_command=str(tmp_path / "env"), srun=None), port=40000)
    command = plan["command"].replace(web_remote.ROOM_PLACEHOLDER, "RoomRoomRoomRoomRoom1")
    relay_file = json.dumps({"v": 1, "relay": "wss://relay.example", "room": "RoomRoomRoomRoomRoom1",
                             "auth": "a", "key": "k", "idle_exit": 1800})
    proc = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env={**os.environ, "HOME": str(home)},
                            start_new_session=True)
    proc.stdin.write((relay_file + "\n").encode())
    proc.stdin.flush()
    seen = []
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            line = proc.stdout.readline().decode().rstrip("\n")
            seen.append(line)
            if line.startswith(web_remote.JOINED_MARK):
                break
        assert any(line.startswith("[plexora-start] pid=") for line in seen), seen
        assert any(line.startswith(web_remote.JOINED_MARK) for line in seen), seen
        written = home / ".plexora" / "relay" / "RoomRoomRoomRoomRoom1.json"
        assert json.loads(written.read_text())["room"] == "RoomRoomRoomRoomRoom1"
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        args = marker.read_text()
        assert "node serve --port 40000 --host 127.0.0.1" in args
        assert f"--relay-file {written}" in args
    finally:
        # SSH closing, harder than sshd's hangup: the script's whole process
        # group is terminated. The node has a group of its own.
        os.killpg(proc.pid, 15)
        proc.wait(timeout=10)
    node_pid = int(next(line for line in seen if line.startswith("[plexora-start]")).split("=")[1])
    try:
        os.kill(node_pid, 0)  # still there
    finally:
        try:
            os.kill(node_pid, 15)
        except ProcessLookupError:
            pass


# -- reading the page's connections ------------------------------------------------


class FakePage:
    """The page's /__plexora/remote/v1 API, as services/webRemote.js answers it."""

    def __init__(self):
        self.sessions = {}
        self.rooms = []
        self.calls = []

    def __call__(self, method, path, body=None, timeout=15.0):
        self.calls.append((method, path, body))
        if path.startswith("/sessions?"):
            return {"sessions": list(self.sessions.values()), "rooms": list(self.rooms)}
        if path == "/sessions" and method == "POST":
            view = {"id": "s1", "name": body["name"], "state": "connecting", "lines": [],
                    "prompt": None, "error": None, "room": "R" * 22, "node": None,
                    "job_started_at": None, "plan": body["plan"]}
            self.sessions["s1"] = view
            self.rooms.append(view["room"])
            return view
        if path.endswith("/answer"):
            self.sessions["s1"]["answered"] = body
            return {"ok": True}
        if method == "DELETE":
            self.sessions["s1"]["state"] = "exited"
            return self.sessions["s1"]
        raise AssertionError(path)


@pytest.fixture()
def page(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    monkeypatch.setenv("PLEXORA_SITE_ORIGIN", "https://viewer.example")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")
    fake = FakePage()
    monkeypatch.setattr(web_remote, "_call", fake)
    monkeypatch.setattr(web_remote, "CACHE_SECONDS", 0.0)
    registered, forgotten = [], []
    from plexora import nodes as node_api

    monkeypatch.setattr(node_api, "register_node",
                        lambda name, endpoint, **kw: registered.append((name, endpoint, kw)))
    monkeypatch.setattr(node_api, "forget_node", lambda name: forgotten.append(name))
    fake.registered, fake.forgotten = registered, forgotten
    remote_store.save(_remote())
    return fake


def test_remote_sessions_on_the_website_are_the_pages(page):
    session = remote_sessions.start(_remote(), kind=remote_sessions.KIND_NODE)
    assert page.calls[0][:2] == ("POST", "/sessions")
    assert page.calls[0][2]["plan"]["host"] == "o2.hms.harvard.edu"
    found = remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE)
    assert found is not None and found.state == "connecting"
    # The website has no viewer connections.
    assert remote_sessions.get("HMS-O2", remote_sessions.KIND_VIEWER) is None
    assert remote_sessions.find_by_nonce("anything") is None
    assert session.id == "s1"


def test_a_question_is_the_dialogs_prompt_and_its_answer_goes_to_the_page(page):
    remote_sessions.start(_remote(), kind=remote_sessions.KIND_NODE)
    page.sessions["s1"].update(state="authenticating",
                               prompt={"id": "p1", "text": "Passcode or option (1-1):"})
    status = remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE).status()
    assert status["state"] == "authenticating"
    assert status["prompt"] == {"id": "p1", "text": "Passcode or option (1-1):"}
    assert remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE).answer("1", "p1")
    assert page.sessions["s1"]["answered"] == {"id": "p1", "answer": "1"}


def test_a_connected_node_goes_on_the_map_at_its_rooms_address(page):
    remote_sessions.start(_remote(), kind=remote_sessions.KIND_NODE)
    page.sessions["s1"].update(state="connected", job_started_at=time.time() * 1000,
                               node={"node_id": "n1", "api_version": 1, "token": "tok",
                                     "hostname": "compute-b-16-186", "platform": "linux"})
    status = remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE).status()
    assert status["node"] == "HMS-O2" and status["state"] == "connected"
    # The profile asked for two hours; the clock started when the job did.
    assert status["time_limit"] == 7200 and 7190 <= status["time_left"] <= 7200
    name, endpoint, extra = page.registered[-1]
    assert name == "HMS-O2"
    assert endpoint == f"https://viewer.example{web_remote.RELAY_PREFIX}/{'R' * 22}"
    assert extra["token"] == "tok" and extra["browser_endpoint"] == endpoint
    assert extra["managed_by"] == "connect:HMS-O2"


def test_a_start_that_ended_says_what_the_desktop_would(page):
    remote_sessions.start(_remote(), kind=remote_sessions.KIND_NODE)
    page.sessions["s1"].update(state="failed", ended_by_script=True,
                               error="The start command on the server ended before the data node started.",
                               lines=["bash: /home/ajn16/miniconda3/envs/plexora/bin/plexora: "
                                      "No such file or directory"])
    status = remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE).status()
    assert status["state"] == "failed"
    assert "could not run" in status["error"]


def test_an_ended_connection_comes_off_the_map(page, monkeypatch):
    from plexora.server.models import nodes as node_registry

    remote_sessions.start(_remote(), kind=remote_sessions.KIND_NODE)
    endpoint = web_remote.relay_endpoint("R" * 22)
    node_registry.save(node_registry.Node(name="HMS-O2", endpoint=endpoint, token="tok",
                                          extra={"managed_by": "connect:HMS-O2"}))
    page.sessions["s1"].update(state="exited")
    remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE)
    assert page.forgotten == ["HMS-O2"]


def test_a_node_at_a_room_the_page_forgot_comes_off_the_map(page):
    from plexora.server.models import nodes as node_registry

    node_registry.save(node_registry.Node(name="old", endpoint=web_remote.relay_endpoint("Z" * 22),
                                          token="tok", extra={"managed_by": "connect:old"}))
    remote_sessions.get("HMS-O2", remote_sessions.KIND_NODE)
    assert page.forgotten == ["old"]


def test_the_website_connects_through_the_routes(page):
    import plexora

    client = plexora.app.test_client()
    answer = client.post("/settings/remotes/HMS-O2/connect")
    assert answer.status_code == 202, answer.get_json()
    assert answer.get_json()["state"] == "connecting"
    places = client.get("/data_places").get_json()["places"]
    mine = next(place for place in places if place["id"] == "HMS-O2")
    assert mine["state"] == "connecting"
    assert client.post("/settings/remotes/HMS-O2/disconnect").status_code == 200
    assert page.calls[-1][0] in ("DELETE", "GET")


def test_the_website_refuses_what_it_cannot_connect(page):
    import plexora

    remote_store.save(_remote(name="hop", jump="bastion.example.org"))
    answer = plexora.app.test_client().post("/settings/remotes/hop/connect")
    assert answer.status_code == 409
    assert "jump host" in answer.get_json()["error"]


def test_the_page_answers_both_prefixes():
    """services/webRemote.js and the service worker agree on the two prefixes
    this module calls and registers nodes at."""
    root = Path(__file__).resolve().parents[1] / "plexora" / "client" / "src" / "js"
    page_js = (root / "services" / "webRemote.js").read_text(encoding="utf-8")
    worker = (root / "workers" / "backendRelay.js").read_text(encoding="utf-8")
    assert f'const API = "{web_remote.API_PREFIX}"' in page_js
    assert f'const RELAY = "{web_remote.RELAY_PREFIX}/"' in page_js
    assert f'"{web_remote.API_PREFIX.rsplit("/", 1)[0]}/"' in worker
    assert f'"{web_remote.RELAY_PREFIX}/"' in worker
    assert f'const ROOM_PLACEHOLDER = "{web_remote.ROOM_PLACEHOLDER}"' in page_js


# -- files-only servers -----------------------------------------------------------
#
# Nothing runs on one (HMS O2's transfer node), so the page's SSH connection is
# the connection: the page opens its SFTP side and Python reads through the
# page (`PageFiles`), as the desktop reads through its system ssh.

TRANSFER = dict(target="ajn16@transfer.rc.hms.harvard.edu", srun=None, remote_command=None,
                extra={"files": {"via": "sftp"}})


def test_a_files_only_server_is_signed_in_to_by_the_page():
    remote = _remote(name="O2-transfer", **TRANSFER)
    assert web_remote.refusal(remote) is None
    plan = web_remote.files_plan(remote)
    assert plan["kind"] == "files" and "command" not in plan
    assert (plan["user"], plan["host"], plan["port"]) == ("ajn16", "transfer.rc.hms.harvard.edu", 22)
    assert plan["target"] == "ajn16@transfer.rc.hms.harvard.edu"
    # The port the desktop's ssh is given among the profile's options.
    other = web_remote.files_plan(_remote(name="lab", target="me@Files.Example.org", srun=None,
                                          ssh_opts=("Port=2222",), extra={"files": {"via": "sftp"}}))
    assert (other["host"], other["port"], other["target"]) == ("Files.Example.org", 2222,
                                                               "me@files.example.org")


def _attrs(attrs):
    import dataclasses

    return dataclasses.asdict(attrs)


def _serve_with(client):
    """The page's files route (services/webRemote.js `files`), over a real
    SFTP client: what the page's own SFTP conversation would answer."""
    def call(name, body, timeout):
        op, path = body["op"], body.get("path", "")
        try:
            if op in ("stat", "lstat"):
                return 200, json.dumps({"attrs": _attrs(getattr(client, op)(path))}).encode()
            if op == "realpath":
                return 200, json.dumps({"path": client.realpath(path or ".")}).encode()
            if op == "listdir":
                return 200, json.dumps({"entries": [[n, _attrs(a)] for n, a in client.listdir(path)]}).encode()
            if op == "read":
                end = body.get("end")
                if end is None:
                    end = client.stat(path).size
                return 200, client.read_range(path, body.get("start", 0), end)
        except FileNotFoundError as exc:
            return 404, json.dumps({"error": exc.strerror, "fx": 2}).encode()
        except PermissionError as exc:
            return 403, json.dumps({"error": exc.strerror, "fx": 3}).encode()
        raise AssertionError(op)
    return call


@pytest.fixture()
def files_page(page, monkeypatch):
    from plexora.server.utils import sftp

    monkeypatch.setattr(sftp, "_resolver", web_remote.files_client)
    remote_store.save(_remote(name="O2-transfer", **TRANSFER))
    yield page
    for key in list(sftp._clients):
        if isinstance(sftp._clients[key], web_remote.PageFiles):
            sftp._clients.pop(key, None)


def test_a_files_connection_is_a_place_once_the_page_has_signed_in(files_page, monkeypatch):
    from plexora.server.utils import sftp

    page = files_page
    remote_sessions.start(remote_store.find("O2-transfer"), kind=remote_sessions.KIND_NODE)
    assert page.calls[0][2]["plan"]["kind"] == "files"
    target = "ajn16@transfer.rc.hms.harvard.edu"
    status = remote_sessions.get("O2-transfer", remote_sessions.KIND_NODE).status()
    assert status["files"] is None and status["node"] is None
    assert "SFTP" in status["phase"]
    assert sftp.client_for(target) is None

    page.sessions["s1"].update(state="connected", kind="files", room=None,
                               files={"home": "/home/ajn16"})
    status = remote_sessions.get("O2-transfer", remote_sessions.KIND_NODE).status()
    assert status["files"] == {"base": f"sftp://{target}", "home": "/home/ajn16"}
    assert status["node"] is None
    client = sftp.client_for(target)
    assert isinstance(client, web_remote.PageFiles) and client.home == "/home/ajn16"

    # The page signing in again after a drop: reads are held, not refused.
    page.sessions["s1"].update(state="connecting", detail="The connection closed; signing in again…")
    status = remote_sessions.get("O2-transfer", remote_sessions.KIND_NODE).status()
    assert status["files"] is None and "signing in again" in status["phase"]
    assert sftp.client_for(target) is client

    page.sessions["s1"].update(state="exited")
    remote_sessions.get("O2-transfer", remote_sessions.KIND_NODE)
    assert not client.alive and sftp.client_for(target) is None


def test_reads_go_to_the_page_and_its_failures_are_the_desktops(files_page, monkeypatch):
    from plexora.server.utils import sftp

    asked = []

    def page_files(name, body, timeout):
        asked.append((name, body))
        if body["path"] == "/missing":
            return 404, json.dumps({"error": "No such file", "fx": 2}).encode()
        if body["path"] == "/gone":
            return 503, json.dumps({"error": "Not connected", "not_connected": True}).encode()
        if body["op"] == "stat":
            return 200, json.dumps({"attrs": {"size": 5, "mode": 0o100644, "mtime": 1}}).encode()
        if body["op"] == "listdir":
            return 200, json.dumps({"entries": [["a.tif", {"size": 5, "mode": 0o100644, "mtime": 1}],
                                                ["sub", {"size": 0, "mode": 0o040755, "mtime": 1}]]}).encode()
        return 200, b"hello"[body.get("start", 0):body.get("end", 5)]

    monkeypatch.setattr(web_remote, "_files_call", page_files)
    client = web_remote.PageFiles("O2-transfer", "ajn16@transfer.rc.hms.harvard.edu", "/home/ajn16")
    assert client.stat("/x/a.tif").size == 5
    assert [(name, attrs.is_dir) for name, attrs in client.listdir("/x")] == [("a.tif", False), ("sub", True)]
    assert client.read_range("/x/a.tif", 1, 4) == b"ell"
    assert client.read_all("/x/a.tif") == b"hello"
    assert asked[0][0] == "O2-transfer"
    with pytest.raises(FileNotFoundError):
        client.stat("/missing")
    with pytest.raises(sftp.SFTPNotConnected):
        client.read_range("/gone", 0, 10)
    # No threads on the website: the async filesystem's call is done in place.
    assert client.run(client.read_range, "/x/a.tif", 0, 2).result() == b"he"
    assert client.counters["reads"] == 3 and client.counters["read_bytes"] == 3 + 5 + 2


@pytest.mark.skipif(sys.platform == "win32", reason="OpenSSH's sftp-server")
def test_an_image_on_a_files_server_draws_through_the_page(sftp_host, monkeypatch):  # noqa: F811
    """The chunk cache over `PageFiles` over the page, the page standing on a
    real SFTP server: the pixels of an OME-TIFF read as on the desktop."""
    import numpy as np
    import tifffile as tf

    from plexora import datasource
    from plexora.server.models import data_model
    from plexora.server.utils import sftp
    from tests.brightfield_fixtures import write_planar_fluorescence

    host = sftp_host()
    local = host.root / "panel.ome.tif"
    write_planar_fluorescence(local)
    sftp.unregister(host.target, host.client)
    monkeypatch.setattr(web_remote, "_files_call", _serve_with(host.client))
    page_client = web_remote.PageFiles("lab", host.target, host.client.home)
    sftp.register(host.target, page_client)
    try:
        entry = datasource.register_image_datasource(name="pagetiff", image=host.url("panel.ome.tif"))
        assert entry["image_kind"] == "ome_tiff"
        data_model.load_datasource("pagetiff", reload=True)
        with tf.TiffFile(str(local)) as tiff:
            expected = tiff.series[0].asarray()
        assert np.array_equal(np.asarray(data_model.channels[0][1, 0:200, 50:250]),
                              expected[1, 0:200, 50:250])
        assert page_client.counters["reads"] > 0
        listed = sftp.listing(host.base, str(host.root))
        assert any(item.get("name") == "panel.ome.tif" for item in listed.get("entries", []))
    finally:
        sftp.unregister(host.target, page_client)


# -- the admin's switch -----------------------------------------------------------


@pytest.fixture()
def switch(monkeypatch):
    from plexora.server.models import remote_access

    value = {"remote_servers": {"website": "free", "local": "free"}}
    monkeypatch.setattr(remote_access, "_fetch", lambda: value["remote_servers"])
    remote_access.reset_for_tests()
    yield value
    remote_access.reset_for_tests()


def _licensed(monkeypatch, yes):
    from plexora import licensing

    monkeypatch.setattr(licensing, "peek", lambda: type("S", (), {"licensed": yes, "state": "free"})())


def test_connecting_is_free_by_default(switch, monkeypatch):
    from plexora.server.models import remote_access

    _licensed(monkeypatch, False)
    assert remote_access.refusal() is None


def test_a_licensed_switch_refuses_an_unlicensed_install_where_it_applies(switch, monkeypatch):
    import plexora
    from plexora.server.models import remote_access

    _licensed(monkeypatch, False)
    switch["remote_servers"] = {"website": "free", "local": "licensed"}
    remote_store.save(_remote(name="lab", srun=None))
    with plexora.app.test_request_context():
        response, code = remote_access.refusal()
    assert code == 403
    assert response.get_json()["license"]["code"] == "license_required"
    answer = plexora.app.test_client().post("/settings/remotes/lab/connect")
    assert answer.status_code == 403
    assert answer.get_json()["error"] == "Connecting to servers needs a Plexora licence."
    # The website's switch is the other one.
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    remote_access.reset_for_tests()
    assert remote_access.where() == "website" and remote_access.refusal() is None


def test_a_licence_passes_a_licensed_switch(switch, monkeypatch):
    from plexora.server.models import remote_access

    _licensed(monkeypatch, True)
    switch["remote_servers"] = {"website": "licensed", "local": "licensed"}
    assert remote_access.refusal() is None


def test_the_last_answer_stands_when_the_platform_cannot_be_reached(switch, monkeypatch):
    from plexora.server.models import remote_access

    switch["remote_servers"] = {"website": "free", "local": "licensed"}
    assert remote_access.switches()["local"] == "licensed"
    remote_access.reset_for_tests()
    monkeypatch.setattr(remote_access, "_fetch", lambda: (_ for _ in ()).throw(OSError("offline")))
    # Kept in the data root: still "licensed", not silently free.
    assert remote_access.switches()["local"] == "licensed"


def test_anything_but_licensed_reads_as_free(switch):
    from plexora.server.models import remote_access

    switch["remote_servers"] = {"website": "yes please", "local": None}
    assert remote_access.switches() == {"website": "free", "local": "free"}
