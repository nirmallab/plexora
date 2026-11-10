"""The website's browser node reading tiles side by side together (services/
browserNode.js TiffPool `flush`, `readRun`, `indexed`, and `group`), run in
Node with its workers stood in for (tests/js/browser_node_runs_probe.mjs).

On a file share one large read costs about what one tile's read does, and
every level a worker parsed for itself was a chain of small reads. A row's
tiles sit back to back in a pyramid's file, so the viewer's scheduler hands
the page a tile's row neighbours with it (views/tileFetch.js `localGroup`)
and the pool reads them in one go (node/tiffReader.js `tiles`); a level is
parsed by one worker, whose reads the others are handed (`index`, `seed`).
A 4-channel view of an ORION slide on the lab's share: 1.5x faster."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "js" / "browser_node_runs_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_rows_tiles_asked_together_are_one_read(probe):
    row = probe["rowOfThree"]
    assert row["ops"] == [{"channel": 0, "level": 0, "tx0": 0, "ty": 2, "count": 3}]
    assert row["single"] == 0
    # Each caller gets its own tile, whatever order they asked in.
    assert row["answers"] == [[0, 0, 1, 2], [0, 0, 0, 2], [0, 0, 2, 2]]


def test_a_read_is_four_tiles_at_most(probe):
    assert probe["rowOfFive"] == [[0, 4], [4, 1]]


def test_tiles_not_side_by_side_are_read_apart(probe):
    apart = probe["apart"]
    assert sorted(apart["ops"]) == [[1, 0, 0, 1], [1, 0, 1, 1], [1, 2, 0, 1], [2, 1, 0, 1]]
    assert apart["answers"] == [[1, 0, 0, 0], [2, 0, 1, 0], [1, 0, 0, 1], [1, 0, 2, 0]]


def test_a_level_is_parsed_once_a_pool(probe):
    index = probe["index"]
    assert index["ops"] == 1
    # Every other worker is handed its reads before it reads that level.
    assert index["seeded"] == index["workers"] - 1
    assert index["seededFirst"] is True


def test_a_level_whose_index_fails_is_still_read(probe):
    assert probe["indexFailed"] == {"answered": True, "seeds": 0}


def test_a_file_in_strips_is_read_a_tile_at_a_time(probe):
    assert probe["strips"] == {"single": 3, "runs": 0, "answered": True}


def test_group_names_the_tiles_beside_one_in_its_row(probe):
    group = probe["group"]
    # Out from the tile a side at a time (right, left, right), RUN_MAX in all;
    # never another channel, level, row or quality, a volume's view, or an
    # address the page never reads.
    assert group["picked"] == [0, 1, 2]
    assert group["alone"] == []
    assert group["notATile"] == []
