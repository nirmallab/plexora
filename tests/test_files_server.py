"""A files-only saved server: the presets, the connection and its routes.

A transfer node (HMS O2's is the case that asked for this) is a machine where
nothing may run but whose files are exactly the images somebody wants to see.
Connecting one opens SFTP through the system ssh and starts nothing over
there; its folders are browsed and its images streamed as `sftp://`
addresses.

No real ssh runs. Where a far side is needed it is OpenSSH's own
`sftp-server`, spawned in place of ssh through connect.py's `_popen` seam --
the same protocol a real `ssh -s host sftp` carries, with no login in front.
"""

import subprocess
import sys
import time

import pytest

import plexora
from plexora import connect
from plexora.server.models import recipes, remote_sessions
from plexora.server.models import remotes as remote_store
from plexora.server.utils import sftp
from tests.remote_fixtures import cache_root  # noqa: F401
from tests.sftp_fixtures import (TARGET, needs_sftp_server, sftp_host,  # noqa: F401
                                 sftp_server_binary)

O2_TRANSFER = "transfer.rc.hms.harvard.edu"


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _no_sessions_left_running():
    yield
    for key in list(remote_sessions.all_sessions()):
        name = key.split(":", 1)[-1]
        remote_sessions.forget(name)
        remote_sessions.forget(name, remote_sessions.KIND_NODE)


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- the presets ---------------------------------------------------------------


def test_the_o2_transfer_preset_asks_for_a_username_and_runs_nothing():
    recipe = recipes.find("hms-o2-transfer")
    assert recipe.files and recipe.institution and recipe.site
    assert recipe.ask == (recipes.ASK_USER,)
    assert recipe.srun is None and not recipe.install
    # Not seen working against the real node yet, and the card says so.
    assert recipe.unverified
    body = recipes.compose("hms-o2-transfer", {"user": "ajn16", "name": "o2-files"})
    assert body["target"] == f"ajn16@{O2_TRANSFER}"
    assert body["files"] == {"via": "sftp"}
    assert body["use_srun"] is False and body["install"] is False
    assert body["recipe"] == "hms-o2-transfer"
    with pytest.raises(ValueError):
        recipes.compose("hms-o2-transfer", {"user": ""})


def test_the_generic_files_preset_asks_for_the_address_too():
    recipe = recipes.find("files")
    assert recipe.files and not recipe.site and not recipe.institution
    body = recipes.compose("files", {"user": "me", "host": "data.lab.edu"})
    assert body["target"] == "me@data.lab.edu"
    with pytest.raises(ValueError, match="address"):
        recipes.compose("files", {"user": "me", "host": ""})


def test_each_profile_on_the_transfer_host_reopens_its_own_form():
    files = remote_store.Remote(name="a", target=f"me@{O2_TRANSFER}",
                                extra={"files": {"via": "sftp"}})
    plain = remote_store.Remote(name="b", target=f"me@{O2_TRANSFER}")
    elsewhere = remote_store.Remote(name="c", target="me@data.lab.edu",
                                    extra={"files": {"via": "sftp"}})
    assert recipes.for_remote(files) == "hms-o2-transfer"
    assert recipes.for_remote(plain) == "ssh"
    assert recipes.for_remote(elsewhere) == "files"


def test_a_profile_records_it_is_files_only_and_how_it_is_reached():
    remote = remote_store.Remote(name="o2", target=f"me@{O2_TRANSFER}",
                                 jump="bastion", ssh_opts=("Port=2222",),
                                 extra={"files": {"via": "sftp"}})
    assert remote.files == {"via": "sftp"}
    assert remote.as_files_kwargs() == {"jump": "bastion", "ssh_opts": ("Port=2222",)}
    back = remote_store.Remote.from_dict("o2", remote.to_dict())
    assert back.files == {"via": "sftp"}
    assert remote_store.Remote(name="x", target="h",
                               extra={"files": {"via": "nfs"}}).files is None


def test_saving_the_preset_through_the_route_keeps_the_record(client, plexora_data_root):
    reply = client.post("/settings/recipes/hms-o2-transfer",
                        json={"user": "ajn16", "name": "o2-files"})
    assert reply.status_code in (200, 201), reply.get_json()
    saved = remote_store.get("o2-files")
    assert saved.files == {"via": "sftp"} and saved.srun is None
    view = [r for r in client.get("/settings/remotes").get_json()["remotes"]
            if r["name"] == "o2-files"][0]
    assert view["files_only"] is True and view["recipe"] == "hms-o2-transfer"
    place = [p for p in client.get("/data_places").get_json()["places"]
             if p["id"] == "o2-files"][0]
    assert place["files_only"] is True and place["files"] is None
    assert place["queued"] is False and place["node"] is None

    # Re-saved through a preset that runs Plexora, it stops being files-only:
    # Connect must not go on opening SFTP for a server that now runs a node.
    client.post("/settings/recipes/ssh",
                json={"user": "ajn16", "host": O2_TRANSFER, "name": "o2-files"})
    assert remote_store.get("o2-files").files is None


# -- the connection --------------------------------------------------------------


def test_the_ssh_line_asks_for_the_sftp_subsystem_and_nothing_else():
    argv = connect.sftp_ssh_argv(f"me@{O2_TRANSFER}", jump="bastion",
                                 ssh_opts=("Port=2222",))
    assert argv[0] == "ssh" and argv[-3:] == ["-s", f"me@{O2_TRANSFER}", "sftp"]
    assert "-T" in argv and "-t" not in argv
    assert "ClearAllForwardings=yes" in argv
    assert argv[argv.index("-J") + 1] == "bastion"
    assert "Port=2222" in argv and "ServerAliveInterval=30" in argv


@pytest.fixture
def far_side(monkeypatch, tmp_path):
    """ssh replaced by a real sftp-server over `tmp_path / "home"`."""
    if sftp_server_binary() is None:
        pytest.skip("OpenSSH sftp-server is not installed")
    home = (tmp_path / "home")
    home.mkdir()
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv)
        kwargs.pop("cwd", None)
        return subprocess.Popen([sftp_server_binary()], cwd=str(home), **kwargs)

    monkeypatch.setattr(connect, "_popen", popen)
    monkeypatch.setattr(connect, "_which", lambda name: "/usr/bin/ssh")
    return home.resolve(), spawned


def test_a_files_session_registers_its_client_and_drops_it_on_stop(far_side):
    home, spawned = far_side
    session = connect.FilesSession(TARGET, echo=lambda line: None)
    session.establish()
    try:
        assert spawned[0][-3:] == ["-s", TARGET, "sftp"]
        assert session.home == str(home)
        assert sftp.client_for(TARGET) is session.client
    finally:
        session.stop()
    assert sftp.client_for(TARGET) is None


def test_no_ssh_is_said_plainly(monkeypatch):
    monkeypatch.setattr(connect, "_which", lambda name: None)
    with pytest.raises(connect.ConnectError, match="No `ssh` command found"):
        connect.FilesSession(TARGET, echo=lambda line: None).establish()


def _fake_ssh(monkeypatch, code):
    def popen(argv, **kwargs):
        return subprocess.Popen([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(connect, "_popen", popen)
    monkeypatch.setattr(connect, "_which", lambda name: "/usr/bin/ssh")


def test_a_server_without_sftp_is_named_as_such(monkeypatch):
    _fake_ssh(monkeypatch, "import sys; sys.stderr.write("
                           "'subsystem request failed on channel 0\\n'); sys.exit(1)")
    with pytest.raises(connect.ConnectError) as caught:
        connect.FilesSession(TARGET, echo=lambda line: None).establish()
    assert caught.value.diagnosed and "does not offer SFTP" in str(caught.value)


def test_a_chatty_shell_startup_file_is_named_as_the_cause(monkeypatch):
    _fake_ssh(monkeypatch, "import sys; sys.stdout.write('Hello from bashrc\\n');"
                           "sys.stdout.flush(); sys.stdin.read()")
    session = connect.FilesSession(TARGET, echo=lambda line: None)
    try:
        with pytest.raises(connect.ConnectError) as caught:
            session.establish()
    finally:
        session.stop()
    assert caught.value.diagnosed and "bashrc" in str(caught.value)


def test_connecting_a_saved_files_server_ends_connected_with_a_place(far_side,
                                                                    plexora_data_root):
    home, _ = far_side
    remote = remote_store.save(remote_store.Remote(
        name="o2-files", target=TARGET, extra={"files": {"via": "sftp"},
                                               "recipe": "files"}))
    session = remote_sessions.start(remote, askpass_url=None,
                                    kind=remote_sessions.KIND_NODE)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED), \
        session.status()
    status = session.status()
    assert status["node"] is None
    assert status["files"] == {"base": f"sftp://{TARGET}", "home": str(home)}
    remote_sessions.stop("o2-files", remote_sessions.KIND_NODE)
    assert wait_for(lambda: sftp.client_for(TARGET) is None)


def test_a_refused_login_reads_as_one(monkeypatch, plexora_data_root):
    _fake_ssh(monkeypatch, "import sys; sys.stderr.write("
                           "'me@host: Permission denied (publickey).\\n'); sys.exit(255)")
    remote = remote_store.Remote(name="f", target=TARGET,
                                 extra={"files": {"via": "sftp"}})
    session = remote_sessions.start(remote, askpass_url=None,
                                    kind=remote_sessions.KIND_NODE)
    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert "rejected the login" in session.error
    # Nothing about a remote `plexora`: nothing was ever run there.
    assert "plexora" not in session.error.lower()


def test_the_routes_keep_one_connection_per_files_profile(client, monkeypatch,
                                                          plexora_data_root):
    started = []
    monkeypatch.setattr(remote_sessions, "start",
                        lambda remote, **kw: started.append(kw["kind"]) or None)
    from plexora.server.routes import settings_routes

    monkeypatch.setattr(settings_routes, "_remote_view",
                        lambda remote, session=None, log_lines=25: {"name": remote.name})
    remote_store.save(remote_store.Remote(name="f", target=TARGET,
                                          extra={"files": {"via": "sftp"}}))
    client.post("/settings/remotes/f/connect")
    client.post("/settings/remotes/f/connect?kind=viewer")
    assert started == [remote_sessions.KIND_NODE, remote_sessions.KIND_NODE]


# -- browsing and reading through the routes ---------------------------------------


def test_list_dir_lists_a_files_server_by_its_address(client, sftp_host):
    host = sftp_host()
    (host.root / "slides").mkdir()
    (host.root / "slides" / "a.ome.tif").write_bytes(b"II*\x00")

    home = client.post("/list_dir", json={"node": host.base}).get_json()
    assert home["path"] == host.url()
    entry = home["entries"][0]
    assert entry["name"] == "slides" and entry["path"] == host.url("slides")
    inside = client.post("/list_dir", json={"node": host.base,
                                            "path": entry["path"]}).get_json()
    assert inside["entries"][0]["path"] == host.url("slides/a.ome.tif")


def test_a_files_server_is_browsed_by_listing_only(client):
    base = "sftp://nobody@nowhere.example"
    reply = client.post("/browse_path", json={"node": base, "mode": "any"})
    assert reply.status_code == 400 and reply.get_json()["fallback"] == "list"
    capability = client.post("/browse_capability", json={"node": base}).get_json()
    assert capability["dialogs"] == "none"
    missing = client.post("/list_dir", json={"node": base})
    assert missing.status_code == 503 and "Not connected" in missing.get_json()["error"]


def test_fetch_file_reads_a_small_file_off_a_files_server(client, sftp_host):
    host = sftp_host()
    (host.root / "channels.csv").write_text("marker\nDNA\nCD3\n")
    reply = client.post("/fetch_file", json={"node": host.base,
                                             "path": "channels.csv"})
    assert reply.status_code == 200
    assert reply.data == b"marker\nDNA\nCD3\n"
    assert reply.headers["X-Plexora-File-Name"] == "channels.csv"
    whole = client.post("/fetch_file", json={"node": host.base,
                                             "path": host.url("channels.csv")})
    assert whole.data == b"marker\nDNA\nCD3\n"
    folder = client.post("/fetch_file", json={"node": host.base, "path": str(host.root)})
    assert folder.status_code == 400


# -- the browser's half of an address --------------------------------------------


def test_the_browser_builds_the_address_the_server_would():
    """`PlexoraLocators.sftpUrl` turns a path typed for a files-only server
    into the address `sftp.url_for` hands out for the same file, and `urlName`
    names it the way the file is called -- spaces and all."""
    import json
    import shutil
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (Path(__file__).resolve().parents[1] / "plexora" / "client" / "src"
              / "js" / "views" / "locators.js")
    code = (
        "global.window = {};"
        f"eval(require('fs').readFileSync({json.dumps(str(script))}, 'utf8'));"
        "const L = window.PlexoraLocators;"
        "console.log(JSON.stringify(["
        "  L.isRemoteLocator('sftp://me@host/x.tif'),"
        "  L.sftpUrl('sftp://me@host', '/n/data1/run 3/50% a?#.tif', '/home/me'),"
        "  L.sftpUrl('sftp://me@host/', 'images/a.tif', '/home/me'),"
        "  L.sftpUrl('sftp://me@host', '~/a.tif', '/home/me'),"
        "  L.urlName('sftp://me@host/n/data1/my slide.ome.tif'),"
        "  L.remoteScheme('SFTP://me@host/x')]));")
    out = subprocess.run([node, "-e", code], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    remote, typed, relative, tilde, name, scheme = json.loads(out.stdout)
    assert remote is True and scheme == "sftp"
    assert typed == sftp.url_for("me@host", "/n/data1/run 3/50% a?#.tif")
    assert relative == "sftp://me@host/home/me/images/a.tif"
    assert tilde == "sftp://me@host/home/me/a.tif"
    assert name == "my slide.ome.tif"


# -- a project whose server is not connected --------------------------------------


def test_an_unconnected_project_offers_the_saved_server_that_reconnects_it(
        client, sftp_host, monkeypatch):
    """What the viewer's "X is not connected" dialog is drawn from: `profiles`
    carries the saved files-only server so the dialog can offer Connect --
    marked `files` so it says SFTP rather than promising a data node."""
    from plexora import datasource
    from plexora.server.models import data_model
    from tests.ngff_fixtures import write_ngff

    host = sftp_host()
    write_ngff(host.root / "slide.ome.zarr", shape=(1, 64, 64), levels=1, version="0.4")
    remote_store.save(remote_store.Remote(name="o2-files", target=TARGET,
                                          extra={"files": {"via": "sftp"}}))
    datasource.register_image_datasource(name="streamed",
                                         image=host.url("slide.ome.zarr"))
    host.disconnect()
    # Whatever the load managed from the cache, the image is reported missing:
    # what is under test is who is offered to bring it back.
    real = data_model.resource_unavailable
    monkeypatch.setattr(data_model, "resource_unavailable",
                        lambda name, kind: (str(sftp.SFTPNotConnected(TARGET))
                                            if kind == "image" else real(name, kind)))
    reply = client.get("/resource_status?datasource=streamed").get_json()
    assert "o2-files" in reply["unavailable"]["image"]
    assert reply["profiles"] == [{"node": None, "profile": "o2-files", "files": True}]
