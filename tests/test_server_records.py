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
