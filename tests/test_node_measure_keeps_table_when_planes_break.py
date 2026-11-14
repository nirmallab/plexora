"""A finished node measurement survives a broken plane-index download.

The per-plane cell index is optional: a node from before it has none to give,
and the primary then removes the folder and keeps the table
(providers/node.measure_objects_on_node). The same has to hold when the index
download starts and then breaks partway -- a tunnel blip, a node restart, a
read gap past the timeout. The table was already downloaded whole; losing the
index must not throw away hours of measuring.
"""

from __future__ import annotations

import io

import polars as pl
import urllib3

from plexora.server.providers import http
from plexora.server.providers import node as node_provider


class _Node:
    name = "o2"
    endpoint = "http://127.0.0.1:1"


class _Binding:
    node = "o2"

    def __init__(self, resource_id):
        self.resource_id = resource_id


class _BrokenStream:
    """A planes answer whose body breaks after its first chunk, the way
    urllib3 reports a connection dropped mid-body."""

    def __init__(self):
        self.released = False

    def stream(self, chunk, decode_content=True):
        yield b"x" * 512
        raise urllib3.exceptions.ProtocolError(
            "Connection broken: IncompleteRead(512 bytes read, 4096 more expected)")

    def release_conn(self):
        self.released = True


def test_a_planes_download_that_breaks_midway_keeps_the_measured_table(tmp_path, monkeypatch):
    table = pl.DataFrame({"label": [1, 2, 3], "area": [10.0, 20.0, 30.0]})
    buffer = io.BytesIO()
    table.write_parquet(buffer)
    parquet = buffer.getvalue()

    def json_request(node, method, path, **kwargs):
        if path == "/node/v1/volume/measure":
            return {"job": "j1"}
        if path == "/node/v1/volume/measure/j1":
            return {"state": "done", "done": 3, "total": 3, "manifest": {"units": "voxel"}}
        raise AssertionError(f"unexpected {method} {path}")

    def bytes_request(node, method, path, **kwargs):
        assert path == "/node/v1/volume/measure/j1/result"
        return parquet, None

    broken = _BrokenStream()

    def request(node, method, path, **kwargs):
        assert path == "/node/v1/volume/jobs/j1/planes" and kwargs.get("stream")
        return broken

    monkeypatch.setattr(node_provider, "node_for", lambda binding: _Node())
    monkeypatch.setattr(http, "json_request", json_request)
    monkeypatch.setattr(http, "bytes_request", bytes_request)
    monkeypatch.setattr(http, "request", request)

    # An index from an earlier measurement, which must not be left describing
    # this one.
    folder = tmp_path / "objects" / "planes"
    folder.mkdir(parents=True)
    (folder / "stale.bin").write_bytes(b"old")

    frame, manifest = node_provider.measure_objects_on_node(
        _Binding("img"), _Binding("cells"), channel_names=["DNA"], indices=[0],
        units="voxel", planes_folder=folder)

    assert frame.equals(table)
    assert manifest == {"units": "voxel"}
    assert not folder.exists()
    assert broken.released
    assert not list(folder.parent.glob("planes-*.tar"))
