"""tools/bench_remote: the data-node vs SFTP benchmark, and the counters it reads.

What is pinned: the SFTP client counts what crosses each conversation
(requests, replies, READs and their bytes, time waited), `sftp.stats()`
reports it by target, and `/volume/<ds>/perf` carries it beside the remote
chunk cache's own figures; the bench harness classifies a data node's own
tile, batch, level and block URLs and says where each response came from;
the campaign orders arms ABBA from a seeded coin flip and refuses the live
data root; waypoint sets are deterministic and land on tissue; and the
process pattern that stops the benchmark's node matches that node only.
"""

import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tests.remote_fixtures import cache_root  # noqa: F401
from tests.sftp_fixtures import TARGET, sftp_host  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "tools" / "bench_remote"


def _load(name):
    sys.path.insert(0, str(ROOT / "tools" / "bench3d"))
    sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(f"bench_remote_{name}", BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a @dataclass under postponed annotations looks
    # its own module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- the SFTP client's counters ---------------------------------------------------


def test_the_sftp_client_counts_what_each_read_carried(sftp_host):
    from plexora.server.utils import sftp

    host = sftp_host()
    payload = bytes(range(256)) * 1200                    # 307,200 bytes
    (host.root / "blob.bin").write_bytes(payload)
    before = dict(host.client.counters)
    data = host.client.read_range("blob.bin", 1000, 201000)
    assert data == payload[1000:201000]
    after = host.client.counters
    reads = after["reads"] - before["reads"]
    assert reads == -(-200000 // host.client.max_read)
    assert after["read_bytes"] - before["read_bytes"] == 200000
    # Every request answered, the open included; the wire carries the data
    # and its framing.
    assert after["requests"] - before["requests"] >= reads + 1
    assert after["replies"] == after["requests"]
    assert after["bytes_in"] - before["bytes_in"] > 200000
    assert after["wait_s"] >= before["wait_s"]

    stats = sftp.stats()
    assert stats["pid"] > 0
    entry = stats["clients"][sftp.canonical_target(TARGET)]
    assert entry["read_bytes"] >= 200000 and entry["alive"] is True
    assert entry["ssh_pid"] == host.process.pid


def test_volume_perf_carries_the_remote_cache_and_sftp_figures(cache_root):  # noqa: F811
    from plexora.server.routes import volume_routes

    assert {"remote", "sftp"} <= set(volume_routes._PERF_PROVIDERS)
    remote = volume_routes._PERF_PROVIDERS["remote"]("any")
    assert isinstance(remote["bytes_fetched"], int)
    assert isinstance(remote["stores"], list)
    assert "used_bytes" in remote["disk"]
    sftp_stats = volume_routes._PERF_PROVIDERS["sftp"]("any")
    assert set(sftp_stats) == {"pid", "clients"}


# -- the browser-side harness -------------------------------------------------------


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_the_harness_counts_a_data_nodes_own_urls_as_tiles_and_says_whence():
    script = """
import { kindOf, originOf } from "./tools/bench3d/drive.mjs";
const urls = [
  "http://127.0.0.1:24721/node/v1/image/res-1/tile/3/7/2_1?t=abc",
  "http://127.0.0.1:24721/node/v1/seg/res-2/tile/4/0_0?t=abc",
  "http://127.0.0.1:24721/node/v1/batch",
  "http://127.0.0.1:24721/node/v1/image/res-1/level?c=0",
  "http://127.0.0.1:24721/node/v1/image/res-1/block",
  "http://127.0.0.1:8621/generated/data/p/c_0/3/0_0?q=hd&view=z96",
];
const out = urls.map((u) => kindOf(u));
out.push(originOf(urls[0], {}), originOf(urls[5], { node: 12.5 }), originOf(urls[5], { read: 3 }));
console.log(JSON.stringify(out));
process.exit(0);
"""
    done = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == [
        "tile", "tile", "batch", "level", "brick", "tile",
        "node-direct", "node-proxy", "local"]


@needs_node
@pytest.mark.parametrize("name", ["tour.mjs", "survey.mjs"])
def test_the_drivers_parse(name):
    done = subprocess.run(["node", "--check", str(BENCH / name)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# -- the campaign ----------------------------------------------------------------------


def test_arms_run_abba_from_a_seeded_coin_flip():
    campaign = _load("campaign")
    first = campaign.first_arm(20261026)
    assert first == campaign.first_arm(20261026)
    other = "S" if first == "N" else "N"
    orders = [campaign.arm_order(b, first) for b in range(8)]
    assert orders == [[first, other], [other, first], [other, first], [first, other]] * 2
    # Over every four blocks each arm leads twice.
    assert sum(o[0] == "N" for o in orders) == 4


def test_the_campaign_refuses_the_live_data_root(tmp_path):
    campaign = _load("campaign")
    cfg = {"results_dir": str(tmp_path / "out"), "primary_port": 1,
           "bench_dir": str(Path.home() / "Library" / "Application Support" / "plexora"),
           "cluster": {"login": "u@h", "job": 1, "python_env": "/e", "node_data_root": "/n",
                       "file": "/f"}}
    with pytest.raises(SystemExit):
        campaign.Campaign(cfg)


def test_the_stop_pattern_matches_the_benchmarks_node_and_nothing_else():
    o2 = _load("o2")
    pattern = re.compile(o2._own_node("bench-o2"))
    assert pattern.search("/env/bin/python /env/bin/plexora node serve --host 127.0.0.1 "
                          "--port 24621 --token t --node-id bench-o2 --dynamic")
    assert pattern.search("plexora node serve --node-id bench-o2")
    # The user's own nodes, another id that starts the same way, and the
    # shell running pgrep with the pattern in its own command line.
    assert not pattern.search("plexora node serve --dynamic --node-id HMS-O2")
    assert not pattern.search("plexora node serve --node-id bench-o2-old")
    assert not pattern.search(f"bash -c pgrep -u $USER -f '{o2._own_node('bench-o2')}'")


# -- waypoints ----------------------------------------------------------------------------


def _survey(width=5600, height=8400, shot=(560, 840)):
    from PIL import Image

    picture = np.zeros((shot[1], shot[0]), np.uint8)
    picture[100:740, 80:480] = 200                         # the tissue
    buffer = io.BytesIO()
    Image.fromarray(picture).save(buffer, format="PNG")
    return {"info": {"topLeft": [0, 0], "bottomRight": [width, height], "size": [width, height]},
            "png": base64.b64encode(buffer.getvalue()).decode()}


def test_waypoint_sets_are_deterministic_and_on_tissue():
    waypoints = _load("waypoints")
    survey = _survey()
    cells = waypoints.tissue_cells(survey)
    sets, _ = waypoints.pick_sets(cells, 4, seed=7)
    again, _ = waypoints.pick_sets(waypoints.tissue_cells(survey), 4, seed=7)
    assert sets == again and len(sets) == 4
    for one in sets:
        points = [one["a"], one["b"], one["c"]]
        assert len({tuple(p) for p in points}) == 3
        for x, y in points:
            # Inside the bright block (survey px x 10 = image px).
            assert 800 <= x <= 4800 and 1000 <= y <= 7400
