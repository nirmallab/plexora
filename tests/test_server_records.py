"""servers.json: how an agent's process finds a running Plexora server."""

import json
import os
import stat

from plexora.server.models import server_records


def test_announce_records_and_forget_removes(tmp_path):
    server_records.announce(8123, "tok", mode="terminal", root=tmp_path)
    found = server_records.records(root=tmp_path)
    assert found[0]["url"] == "http://127.0.0.1:8123/" and found[0]["token"] == "tok"
    if os.name != "nt":
        mode = stat.S_IMODE((tmp_path / "servers.json").stat().st_mode)
        assert mode & 0o077 == 0
    server_records.forget(root=tmp_path)
    assert server_records.records(root=tmp_path) == []


def test_dead_processes_are_ignored(tmp_path):
    (tmp_path / "servers.json").write_text(json.dumps({
        "999999": {"pid": 999999, "port": 1, "token": None, "started": 1}}))
    assert server_records.records(root=tmp_path) == []


def test_notebook_sidecars_are_found_too(tmp_path):
    (tmp_path / "sidecars.json").write_text(json.dumps({
        "k": {"pid": os.getpid(), "port": 9001, "token": "t", "started": 2}}))
    found = server_records.records(root=tmp_path)
    assert found[0]["url"] == "http://127.0.0.1:9001/" and found[0]["mode"] == "notebook"


def test_a_wildcard_host_is_reached_on_loopback(tmp_path):
    server_records.announce(8124, None, host="0.0.0.0", root=tmp_path)
    assert server_records.records(root=tmp_path)[0]["url"] == "http://127.0.0.1:8124/"


# -- the bridge ---------------------------------------------------------------


def test_a_record_says_it_speaks_the_bridge(tmp_path):
    server_records.announce(8125, "tok", root=tmp_path)
    raw = json.loads((tmp_path / "servers.json").read_text())[str(os.getpid())]
    assert raw["bridge"]["protocol"] == "1.0" and raw["bridge"]["provider"] == "plexora"
    assert raw["bridge"]["capabilities_url"] == "agent/v1/capabilities"
    assert "token" not in raw["bridge"]
    assert server_records.records(root=tmp_path)[0]["bridge"] == raw["bridge"]


def test_an_explicit_bridge_block_is_kept(tmp_path):
    server_records.announce(8126, None, root=tmp_path, bridge={"protocol": "1.1",
                                                               "provider": "plexora"})
    assert server_records.records(root=tmp_path)[0]["bridge"]["protocol"] == "1.1"


def test_the_protocol_library_reads_the_record(tmp_path):
    from spatialbridge import peers

    server_records.announce(8127, "tok", root=tmp_path)
    found = peers.plexora_records(tmp_path)
    assert found[0].url == "http://127.0.0.1:8127/" and found[0].protocol == "1.0"
    assert found[0].public()["token"] == "***"


def test_the_notebook_registry_is_owner_readable_only(tmp_path):
    """Every sidecar record carries its server's token: world-readable, any
    account on a shared machine could drive the viewer."""
    from plexora import jupyter

    jupyter._write_registry({"k": {"pid": os.getpid(), "port": 9002, "token": "secret"}})
    path = jupyter._registry_path()
    assert json.loads(path.read_text())["k"]["token"] == "secret"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
