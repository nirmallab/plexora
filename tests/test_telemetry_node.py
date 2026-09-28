"""Data nodes count in memory and never upload; the primary folds their counts."""

import json
import threading

import numpy as np
import tifffile

from plexora.server.providers.http import TOKEN_HEADER
from plexora.telemetry import node_hooks


def _image(path):
    rng = np.random.default_rng(3)
    data = rng.poisson(50, (2, 256, 256)).astype(np.uint16)
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def test_in_process_node_counts_and_never_uploads(telemetry_enabled, tmp_path):
    from plexora.server.node.app import create_node_app

    _t, fake = telemetry_enabled
    path = _image(tmp_path / "slide.ome.tif")
    app = create_node_app([f"image:img={path}"], token="secret-token",
                          allow_origins=["http://allowed.example"])
    client = app.test_client()
    headers = {TOKEN_HEADER: "secret-token"}
    for i in range(50):
        response = client.get(f"/node/v1/image/img/tile/channel_0/0/0_0.png?i={i % 3}",
                              headers=headers)
        assert response.status_code == 200, response.data
        assert "total;dur=" in response.headers["Server-Timing"]
    allowed = client.get("/node/v1/image/img/tile/channel_0/0/0_0.png",
                         headers={**headers, "Origin": "http://allowed.example"})
    assert allowed.headers.get("Timing-Allow-Origin") == "http://allowed.example"
    other = client.get("/node/v1/image/img/tile/channel_0/0/0_0.png",
                       headers={**headers, "Origin": "http://evil.example"})
    assert "Timing-Allow-Origin" not in other.headers
    hello = client.get("/node/v1/hello", headers=headers).get_json()
    block = hello["telemetry"]
    assert block["tiles"]["count"] == 52
    assert block["tiles"]["cache_miss"] >= 1 and block["tiles"]["cache_hit"] >= 49
    assert sum(block["tiles"]["total_ms"]) == 52
    assert fake.requests == []
    assert not [t for t in threading.enumerate() if t.name.startswith("plexora-telemetry")]
    assert not (tmp_path / ".telemetry").exists()
    node_hooks._reset_for_tests()


def test_node_with_telemetry_off_has_no_block(tmp_path):
    from plexora.server.node.app import create_node_app

    path = _image(tmp_path / "slide.ome.tif")
    app = create_node_app([f"image:img={path}"], token="t")
    client = app.test_client()
    response = client.get("/node/v1/image/img/tile/channel_0/0/0_0.png", headers={TOKEN_HEADER: "t"})
    assert "Server-Timing" in response.headers  # always
    assert "telemetry" not in client.get("/node/v1/hello", headers={TOKEN_HEADER: "t"}).get_json()


def block(since, tiles_hit, tiles_miss, bins, requests=0, errors=0):
    return {"since": since, "uptime_s": 1,
            "tiles": {"count": tiles_hit + tiles_miss, "cache_hit": tiles_hit,
                      "cache_miss": tiles_miss, "total_ms": bins, "read_ms": bins,
                      "encode_ms": bins},
            "requests": {"count": requests, "errors": errors, "by_family": {},
                         "ms": [0] * 9}}


def test_primary_folds_deltas_once_and_resets_on_restart(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    node_hooks._reset_for_tests()
    first = block("aaaa", 5, 1, [6, 0, 0, 0, 0, 0, 0, 0, 0], requests=6)
    second = block("aaaa", 9, 1, [10, 0, 0, 0, 0, 0, 0, 0, 0], requests=10, errors=1)
    node_hooks.fold("hpc-node", {"telemetry": first}, 12.0)
    node_hooks.fold("hpc-node", {"telemetry": second}, 12.0)
    node_hooks.fold("hpc-node", {"telemetry": second}, 12.0)  # a repeat adds nothing
    restarted = block("bbbb", 2, 0, [2, 0, 0, 0, 0, 0, 0, 0, 0])
    node_hooks.fold("hpc-node", {"telemetry": restarted}, 12.0)
    t.sync()
    rows = [(r[2], json.loads(r[3]), r[5], list(r[6:15])) for r in t.queue.snapshot()[0]
            if r[1] == "node.summary"]
    hits = sum(n for key, dims, n, _ in rows if key == "tiles" and dims == {"cache": "hit"})
    assert hits == 9 + 2
    total = next(bins for key, _d, _n, bins in rows if key == "tile_total_ms")
    assert total[0] == 12
    assert next(n for key, _d, n, _ in rows if key == "nodes") == 1
    assert next(n for key, _d, n, _ in rows if key == "request_errors") == 1
    assert "hpc-node" not in json.dumps(rows)


def test_hostile_blocks_are_ignored(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    node_hooks._reset_for_tests()
    for hostile in ({"since": "x", "tiles": "lots"}, {"tiles": {}}, "nope",
                    block("/Users/alice", 1, 0, [1] * 8)):
        node_hooks.fold("n", {"telemetry": hostile}, 5.0)
    t.sync()
    rows = [r for r in t.queue.snapshot()[0] if r[1] == "node.summary"]
    assert {r[2] for r in rows} <= {"hello_rtt_ms", "nodes"}
    assert "alice" not in json.dumps(rows)


def test_subprocess_node_never_uploads(tmp_path, telemetry_enabled):
    from tests.node_harness import start_node

    _t, fake = telemetry_enabled
    path = _image(tmp_path / "slide.ome.tif")
    node = start_node(f"image:img={path}",
                      env={"PLEXORA_TELEMETRY": "diagnostics", "PLEXORA_TESTING": "0",
                           "PLEXORA_TELEMETRY_ENDPOINT": fake.url})
    try:
        import urllib3

        pool = urllib3.PoolManager()
        base = f"http://127.0.0.1:{node.port}"
        for _ in range(5):
            response = pool.request("GET", f"{base}/node/v1/image/img/tile/channel_0/0/0_0.png",
                                    headers={TOKEN_HEADER: node.token})
            assert response.status == 200
        hello = json.loads(pool.request("GET", f"{base}/node/v1/hello",
                                        headers={TOKEN_HEADER: node.token}).data)
        assert hello["telemetry"]["tiles"]["count"] == 5
    finally:
        node.stop()
    assert fake.requests == []
    from pathlib import Path

    assert not (Path(node.root) / ".telemetry").exists()
