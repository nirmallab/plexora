"""Moving through a volume's planes without waiting on the reader.

Measured on a 70-channel, 194-plane Imaris stack served from an HMS O2 data
node: a node with 16 cores answered about 21 cold tiles a second however many
were asked for, a far jump through the planes took seconds, and switching a
channel on took ~10 s the first time. Pinned here, as the work done rather
than as wall-clock time:

- An Imaris file at a web address has its chunks read BY OFFSET, outside the
  file's lock and in parallel (server/volume/sources/ims.py), and one whose
  offsets do not read back as h5py's own bytes reads the old way. A file on a
  disk does not: finding a chunk's offset costs more there than reading it
  (tests/test_volume_chunk_cache.py).
- Background work -- a window scan, a read ahead -- reads in the chunk cache's
  background lane, never in front of a tile.
- The block of planes ahead of a plane tile is read in the background
  (server/volume/prefetch.py), in the direction of travel.
- A node's 16-bit tile of a view waits on no window; its tile cache is
  budgeted in bytes; a view's overview, statistics and mixture fit survive the
  job that computed them; and its banked volume windows are seeded at warm-up.
- The browser side runs as a probe (tests/js/coverage_ready_probe.mjs).
"""

from __future__ import annotations

import shutil
import subprocess
import time
import types
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest

from plexora.server.volume import chunk_cache
from plexora.server.volume import prefetch as prefetch_module
from plexora.server.volume.sources import ims as ims_source
from tests import volume_fixtures as vf

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = {"X-Plexora-Node-Token": "x"}


def _quiet(*_args, **_kwargs):
    pass


@pytest.fixture()
def fresh_cache(monkeypatch):
    cache = chunk_cache.ChunkCache(64 * 2**20)
    monkeypatch.setattr(chunk_cache, "CACHE", cache)
    return cache


def _stored(path, level, channel, z0, z1, rows, cols):
    import h5py

    with h5py.File(path, "r") as handle:
        data = handle[f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel {channel}/Data"]
        return np.asarray(data[z0:z1 + 1, rows, cols])


# -- chunks by offset ------------------------------------------------------------


def _as_if_remote(source):
    """Read `source` the way a file at a web address is read -- by offset --
    while its bytes still come from the local file through `_RawFile`."""
    source._stream = types.SimpleNamespace(close=lambda: None)
    return source


@pytest.mark.parametrize("compression,shuffle", [(None, False), ("gzip", False), ("gzip", True)])
def test_chunks_are_read_by_offset_outside_the_lock(tmp_path, fresh_cache, monkeypatch,
                                                    compression, shuffle):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           compression=compression, shuffle=shuffle, chunks=(4, 16, 16))
    source = _as_if_remote(ims_source.open_ims_volume(path))
    held = []
    real = ims_source._RawFile.read

    def read(self, offset, size):
        held.append(source._io.locked())
        return real(self, offset, size)

    monkeypatch.setattr(ims_source._RawFile, "read", read)
    got = source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 7, slice(0, 64), slice(0, 64)))
    assert source._direct == {(0, 0, 0): True}
    assert held and not any(held), "a chunk's bytes were read with the file's lock held"
    source.close()


def test_a_file_whose_offsets_do_not_read_back_reads_the_old_way(tmp_path, fresh_cache,
                                                                 monkeypatch):
    """The first chunk read by offset is compared with h5py's own read of it.
    Bytes that differ (a driver that is not a plain file) mean this dataset is
    read through h5py, and the picture is still exactly the file's."""
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           compression="gzip", chunks=(4, 16, 16))
    source = _as_if_remote(ims_source.open_ims_volume(path))
    monkeypatch.setattr(ims_source._RawFile, "read", lambda self, offset, size: b"\0" * size)
    got = source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 7, slice(0, 64), slice(0, 64)))
    assert source._direct == {(0, 0, 0): False}
    source.close()


def test_many_threads_read_the_same_file_at_once_and_agree_with_h5py(tmp_path, fresh_cache):
    import threading

    path, _ = vf.write_ims(tmp_path / "s.ims", channels=3, depth=17, height=150, width=133,
                           compression="gzip", shuffle=True, chunks=(8, 32, 32))
    source = ims_source.open_ims_volume(path)
    wrong = []

    def plane(channel, z):
        got = np.asarray(source.plane_pyramid(0, z)[0][channel, 0:150, 0:133])
        if not np.array_equal(got, _stored(path, 0, channel, z, z, slice(0, 150),
                                           slice(0, 133))[0]):
            wrong.append((channel, z))

    threads = [threading.Thread(target=plane, args=(c, z)) for c in range(3) for z in range(17)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not wrong
    source.close()


def test_a_closed_source_lets_go_of_its_file(tmp_path, fresh_cache):
    """Windows will not delete a file anybody holds open, and the reader by
    offset holds a handle of its own beside h5py's."""
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           compression="gzip", chunks=(4, 16, 16))
    source = ims_source.open_ims_volume(path)
    source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    source.close()
    path.unlink()
    assert not path.exists()


# -- the background lane ---------------------------------------------------------


class _Pool:
    """An executor that runs at once and remembers that it was asked."""

    def __init__(self, name, used):
        self.name, self.used = name, used

    def submit(self, fn, *args):
        self.used.append(self.name)
        future = Future()
        future.set_result(fn(*args))
        return future


def test_background_reads_go_to_the_background_pool(tmp_path, fresh_cache, monkeypatch):
    used = []
    monkeypatch.setattr(chunk_cache, "DECODE_POOL", _Pool("tile", used))
    monkeypatch.setattr(chunk_cache, "BACKGROUND_POOL", _Pool("background", used))
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           compression="gzip", chunks=(4, 16, 16))
    source = ims_source.open_ims_volume(path)
    source.read_block(0, 0, 0, 0, 3, slice(0, 64), slice(0, 64))
    assert used and set(used) == {"tile"}
    used.clear()
    with chunk_cache.background():
        assert chunk_cache.in_background()
        source.read_block(0, 0, 0, 4, 7, slice(0, 64), slice(0, 64))
    assert not chunk_cache.in_background()
    assert used and set(used) == {"background"}
    source.close()


# -- reading ahead -----------------------------------------------------------------


def _wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_block_ahead_is_read_in_the_direction_of_travel():
    ahead = prefetch_module.PlanePrefetcher(min_budget=0)
    reads, lanes = [], []

    def read(z):
        reads.append(z)
        lanes.append(chunk_cache.in_background())

    # No direction yet: the block after this one and the one before.
    assert sorted(ahead.after_tile("tile", 20, 40, 8, read)) == [15, 24]
    # Moving deeper, inside the same block: what is ahead is already queued.
    assert ahead.after_tile("tile", 21, 40, 8, read) == []
    # Moving back up into the block before: only the block before that.
    assert ahead.after_tile("tile", 12, 40, 8, read) == [7]
    assert _wait_for(lambda: len(reads) == 3)
    assert sorted(reads) == [7, 15, 24]
    assert all(lanes), "a read ahead ran in front of tiles"
    # Another tile is another region.
    assert ahead.after_tile("other", 20, 40, 8, read)


def test_nothing_is_read_ahead_where_it_cannot_help(monkeypatch):
    ahead = prefetch_module.PlanePrefetcher(min_budget=0)
    never = lambda z: pytest.fail("read ahead")  # noqa: E731
    assert ahead.after_tile("planes", 3, 40, 1, never) == [], "one plane per chunk"
    assert ahead.after_tile("thin", 3, 8, 8, never) == [], "one block is the whole stack"
    small = prefetch_module.PlanePrefetcher(min_budget=chunk_cache.CACHE.budget + 1)
    assert small.after_tile("tile", 3, 40, 8, never) == [], "no room beside the plane on screen"


def test_a_read_ahead_that_fails_is_nobodys_error_and_may_be_asked_again():
    ahead = prefetch_module.PlanePrefetcher(min_budget=0)

    def broken(z):
        raise OSError("the filesystem went away")

    assert ahead.after_tile("tile", 3, 40, 8, broken) == [8]
    assert _wait_for(lambda: ahead.snapshot()["failed"] == 1)
    assert ahead.after_tile("tile", 4, 40, 8, broken) == [8]


# -- a volume on a node -------------------------------------------------------------


def _ims_node(tmp_path, monkeypatch, name="v.ims"):
    from plexora.server.node.app import create_node_app

    path, data = vf.write_ims(tmp_path / name, channels=2, depth=12, height=300, width=280,
                              compression="gzip", chunks=(4, 64, 64), levels=2)
    app = create_node_app([f"image:vol={path}"], token="x", log=_quiet)
    return app, app.config["PLEXORA_NODE_RESOURCES"].get("vol"), path


def test_a_nodes_hd_tile_of_a_view_waits_on_no_window(tmp_path, monkeypatch):
    """A 16-bit tile is the voxels themselves. Asking for the channel's window
    first made a cold channel's first HD tile wait on a scan of its stack."""
    from plexora.server.node import api as node_api

    app, _resource, _path = _ims_node(tmp_path, monkeypatch)

    def never(*_a, **_k):
        raise AssertionError("an HD tile asked for the volume window")

    monkeypatch.setattr(node_api, "_volume_window", never)
    answer = app.test_client().get(
        "/node/v1/image/vol/tile/ch_0/0/0_0?view=z5&zs=depth&tw=128&th=128&q=hd",
        headers=TOKEN)
    assert answer.status_code == 200, answer.data[:200]
    assert "max-age" in answer.headers["Cache-Control"]


def test_a_nodes_plane_tile_reads_the_block_ahead(tmp_path, monkeypatch):
    from plexora.server.node import api as node_api

    app, _resource, _path = _ims_node(tmp_path, monkeypatch)
    monkeypatch.setattr(prefetch_module.PREFETCHER, "min_budget", 0)
    prefetch_module.PREFETCHER.forget()
    before = prefetch_module.PREFETCHER.snapshot()["read"]
    node_api._tile_cache.clear()
    answer = app.test_client().get(
        "/node/v1/image/vol/tile/ch_1/0/0_0?view=z1&zs=depth&tw=128&th=128&q=hd",
        headers=TOKEN)
    assert answer.status_code == 200
    assert _wait_for(lambda: prefetch_module.PREFETCHER.snapshot()["read"] > before)
    # The block of planes 4-7 (chunks are 4 deep) is decoded before anybody asked.
    blocks = {key[4] for key in list(chunk_cache.CACHE._entries)
              if isinstance(key, tuple) and len(key) == 7 and key[3] == 1}
    assert 1 in blocks, blocks


def test_the_nodes_tile_cache_is_budgeted_in_bytes(tmp_path, monkeypatch):
    from plexora.server.node import api as node_api

    app, _resource, _path = _ims_node(tmp_path, monkeypatch)
    node_api._tile_cache.clear()
    monkeypatch.setattr(node_api, "_TILE_CACHE_BYTES", 40_000)
    client = app.test_client()
    # Whole tiles only: one cut by the image's edge is smaller, and two of
    # those can fit the budget for the right reason.
    for tile in ("0_0", "1_0", "0_1", "1_1"):
        answer = client.get(f"/node/v1/image/vol/tile/ch_0/0/{tile}?view=z5&zs=depth"
                            "&tw=128&th=128&q=hd", headers=TOKEN)
        assert answer.status_code == 200
    # Each 128 px HD tile is ~33 KB: one fits, a second does not.
    assert len(node_api._tile_cache) == 1
    assert node_api._tile_cache_bytes <= 40_000
    node_api._tile_cache.clear()


def test_a_views_statistics_survive_the_job_that_computed_them(tmp_path, monkeypatch):
    """Every channel's overview at once means opening every channel's dataset
    -- on a cluster filesystem, the twenty-odd seconds before a first channel
    could show. Kept on disk with the file's fingerprint, a restarted node
    answers from there."""
    from plexora.server.node import api as node_api
    from plexora.server.node.app import create_node_app
    from plexora.server.providers import local as local_provider

    app, _resource, path = _ims_node(tmp_path, monkeypatch)
    query = "channel_index=1&view=z5&zs=depth"
    first = app.test_client()
    stats = first.get(f"/node/v1/image/vol/stats?{query}&provisional=1", headers=TOKEN)
    fit = first.get(f"/node/v1/image/vol/gmm?{query}", headers=TOKEN)
    assert stats.status_code == fit.status_code == 200

    def never(*_a, **_k):
        raise AssertionError("the overview was read again")

    monkeypatch.setattr(local_provider, "volume_overview", never)
    monkeypatch.setattr("plexora.server.models.data_model.channel_gmm_of", never)
    second_app = create_node_app([f"image:vol={path}"], token="x", log=_quiet)
    second = second_app.test_client()
    again = second.get(f"/node/v1/image/vol/stats?{query}&provisional=1", headers=TOKEN)
    refit = second.get(f"/node/v1/image/vol/gmm?{query}", headers=TOKEN)
    assert again.status_code == refit.status_code == 200
    assert again.json["vmax_hint"] == pytest.approx(stats.json["vmax_hint"])
    assert refit.json["vmax"] == pytest.approx(fit.json["vmax"])
    assert node_api._derived_dir(second_app.config["PLEXORA_NODE_RESOURCES"].get("vol")).is_dir()


def test_a_rewritten_file_does_not_answer_from_the_old_statistics(tmp_path, monkeypatch):
    import os

    from plexora.server.node.app import create_node_app
    from plexora.server.providers import local as local_provider

    app, _resource, path = _ims_node(tmp_path, monkeypatch)
    query = "channel_index=0&view=z5&zs=depth&provisional=1"
    assert app.test_client().get(f"/node/v1/image/vol/stats?{query}",
                                 headers=TOKEN).status_code == 200
    os.utime(path, ns=(1, 1))
    calls = []
    real = local_provider.volume_overview
    monkeypatch.setattr(local_provider, "volume_overview",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    second_app = create_node_app([f"image:vol={path}"], token="x", log=_quiet)
    assert second_app.test_client().get(f"/node/v1/image/vol/stats?{query}",
                                        headers=TOKEN).status_code == 200
    assert calls, "a changed file answered from the old file's overview"


def test_warming_seeds_a_volumes_banked_windows(tmp_path, monkeypatch):
    from plexora.server.node import api as node_api
    from plexora.server.node.app import warm_resources

    app, resource, _path = _ims_node(tmp_path, monkeypatch)
    key = "1|volume|depth|"
    node_api._remember_window(resource, key, (0.0, 1234.0))
    warm_resources(app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)
    assert resource.derived.get(("qwindow", key)) == (0.0, 1234.0)


# -- sizing from the allocation ------------------------------------------------------


def test_memory_is_the_allocations_not_the_machines(monkeypatch):
    from plexora import _resources

    monkeypatch.setattr(_resources, "_cgroup_memory_bytes", lambda: None)
    monkeypatch.setenv("SLURM_MEM_PER_NODE", "1024")
    monkeypatch.delenv("SLURM_MEM_PER_CPU", raising=False)
    assert _resources.allocated_memory_bytes() == 1024 * 2**20
    monkeypatch.delenv("SLURM_MEM_PER_NODE")
    monkeypatch.setattr(_resources, "allocated_cpus", lambda: 4)
    monkeypatch.setenv("SLURM_MEM_PER_CPU", "512")
    assert _resources.allocated_memory_bytes() == 4 * 512 * 2**20


# -- the browser ------------------------------------------------------------------------


def test_planes_and_channels_show_from_coverage_and_the_hovered_plane_is_made_ready():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / "coverage_ready_probe.mjs")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checks passed" in proc.stdout, proc.stdout
    assert proc.stdout.count("ok - ") >= 45, proc.stdout
