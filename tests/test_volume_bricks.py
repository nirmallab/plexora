"""A stack's z-chunked copy (plexora/server/volume/bricks.py).

Reads across planes -- projections, sections, the 3-D view's level, the
brightness scan -- come from bricks 32 planes deep where the copy holds them,
and from the stack itself everywhere else. The copy is the stack's own levels,
never resampled: a block read either way is the same block. These pin that,
and the bookkeeping that keeps it true: a shard is read only once its write
finished, a changed source throws the copy away, and the cache of finer levels
keeps to its budget.
"""

import time

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model, layer_jobs
from plexora.server.volume import bricks, model, plane_view
from plexora.server.volume.sources import open_volume
from plexora.server.volume.viewspec import ViewSpec
from tests import volume_fixtures as vf


def _stack(tmp_path, depth=40, height=300, width=260, channels=2):
    path, data = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=channels,
                                     depth=depth, height=height, width=width, levels=3)
    source = open_volume(path, z_semantics="depth")
    return path, data, source


def _store(tmp_path, path, source):
    return bricks.Store(tmp_path / "volume_v1", source.volume, bricks.source_key(path))


@pytest.fixture
def small_levels(monkeypatch):
    # Level 1 (150 x 130) of a 40-plane 16-bit stack fits; level 0 does not.
    monkeypatch.setattr(bricks, "LEVEL_BUDGET", 40 * 150 * 130 * 2)


def test_the_coarse_copy_reads_back_exactly(tmp_path, small_levels):
    path, data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        assert bricks.coarse_levels(source.volume) == [1, 2]
        bricks.build_coarse(store, source)
        assert store.coarse_complete()
        bricked = bricks.adopt(source, store)
        # Blocks where the copy holds the level; plane by plane where it does
        # not -- a stack stored plane by plane read in thin bands decodes each
        # tile strip over and over (a 15 s brightness scan took 213 s).
        assert model.reads_blocks(bricked, 1) and model.block_depth(bricked, 1) == 32
        assert not model.reads_blocks(bricked, 0)
        rows, cols = slice(10, 120), slice(5, 100)
        assert np.array_equal(bricked.read_block(1, 0, 1, 3, 37, rows, cols),
                              source.read_block(1, 0, 1, 3, 37, rows, cols))
        # Plane reads are the stack's own; level 0 is not in the coarse copy.
        assert not store.holds(0, 0, 1, 0, 39, slice(0, 10), slice(0, 10))
        assert np.array_equal(bricked.read_block(0, 0, 1, 0, 39, slice(0, 10), slice(0, 10)),
                              data[0, 1, :, :10, :10])
        # A projection through the copy is the projection of the stack.
        slab = plane_view.plane_pyramid(bricked, ViewSpec.parse("z0-39max", bricked.volume))
        assert np.array_equal(slab["1"][1], data[0, 1, :, ::2, ::2].max(0))
    finally:
        source.close()


def test_a_copy_interrupted_resumes_where_it_stopped(tmp_path, small_levels):
    path, _data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        reads = []
        original = source.read_block

        def counted(*args):
            reads.append(args)
            return original(*args)

        source.read_block = counted

        class StopAfter:
            def __init__(self, n):
                self.n = n

            def is_set(self):
                return len(reads) >= self.n

        bricks.build_coarse(store, source, cancel=StopAfter(3))
        assert not store.coarse_complete()
        first = len(reads)
        bricks.build_coarse(store, source)
        assert store.coarse_complete()
        # 2 levels x 2 channels x 2 blocks of 32 planes = 8 units, each read once.
        assert len(reads) == 8 and first == 3
    finally:
        source.close()


def test_a_changed_source_throws_the_copy_away(tmp_path, small_levels):
    path, _data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        bricks.build_coarse(store, source)
        assert store.coarse_complete()
        moved = bricks.Store(tmp_path / "volume_v1", source.volume, "another:version")
        moved.reset_if_stale()
        assert not moved.coarse_complete()
        assert not (tmp_path / "volume_v1" / "1").exists()
    finally:
        source.close()


def test_an_area_is_prepared_at_a_finer_level_and_evicted_past_the_budget(
        tmp_path, small_levels, monkeypatch):
    path, data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        bricked = bricks.adopt(source, store)
        done = bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[1])
        assert done["shards"] == 2          # planes 0-31 and 32-39 of the one shard there
        rows, cols = slice(40, 140), slice(25, 190)
        assert store.holds(0, 0, 1, 0, 39, rows, cols)
        assert np.array_equal(bricked.read_block(0, 0, 1, 0, 39, rows, cols),
                              data[0, 1, :, 40:140, 25:190])
        assert not store.holds(0, 0, 0, 0, 39, rows, cols)        # channel 0 not prepared
        # Preparing channel 0 too, with room for only one channel's shards,
        # gives back the least recently used.
        held = store.manifest()["bytes"]
        monkeypatch.setattr(bricks, "CACHE_BUDGET", held + 1)
        bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[0])
        assert store.manifest()["bytes"] <= held + 1
        assert store.holds(0, 0, 0, 0, 39, rows, cols)
        assert not store.holds(0, 0, 1, 0, 39, rows, cols)
        # ... and the evicted area reads from the stack again, correctly.
        assert np.array_equal(bricked.read_block(0, 0, 1, 0, 39, rows, cols),
                              data[0, 1, :, 40:140, 25:190])
    finally:
        source.close()


def test_a_rectangle_partly_prepared_reads_its_pieces_from_both(tmp_path, small_levels,
                                                                 monkeypatch):
    monkeypatch.setattr(bricks, "SHARD_XY", 128)
    path, data, source = _stack(tmp_path, height=300, width=400)
    try:
        store = _store(tmp_path, path, source)
        bricked = bricks.adopt(source, store)
        bricks.prepare(store, source, 0, (130, 0, 300, 120), channels=[0])
        reads = []
        original = source.read_block

        def counted(level, t, channel, z0, z1, rows, cols):
            reads.append((rows.start, rows.stop, cols.start, cols.stop))
            return original(level, t, channel, z0, z1, rows, cols)

        source.read_block = counted
        rows, cols = slice(10, 120), slice(0, 400)       # a section-tile-wide strip
        block = bricked.read_block(0, 0, 0, 0, 39, rows, cols)
        assert np.array_equal(block, data[0, 0, :, 10:120, :])
        # Shard columns 0 and 3 from the stack; 1 and 2 (128-384) from the bricks.
        assert sorted(read[2] for read in reads) == [0, 384]
    finally:
        source.close()


def test_a_small_or_z_chunked_stack_is_never_copied(tmp_path):
    path, _data, source = _stack(tmp_path)
    try:
        assert not bricks.worth_it(source)          # far below MIN_VOXELS
    finally:
        source.close()
    ims_path, _ = vf.write_ims(tmp_path / "v.ims", depth=16)
    ims = open_volume(ims_path)
    try:
        assert bricks.z_decode(ims) >= 4
    finally:
        ims.close()


def test_a_loaded_large_stack_is_copied_in_the_background(tmp_path, small_levels,
                                                          monkeypatch):
    monkeypatch.setattr(bricks, "MIN_VOXELS", 1)
    path, data = vf.write_ome_volume(tmp_path / "big.ome.tif", axes="CZYX", channels=2,
                                     depth=40, height=300, width=260, levels=3)
    register_image_datasource("big", path)
    data_model.load_datasource("big")
    try:
        assert isinstance(data_model.volume_source, bricks.BrickedSource)
        client = plexora.app.test_client()
        for _ in range(200):
            state = client.get("/volume/big/info").get_json()["bricks"]
            if state.get("state") == "ready":
                break
            time.sleep(0.05)
        assert state["state"] == "ready" and state["coarse_ready"] == [1, 2]
        prepared = client.post("/volume/big/prepare",
                               json={"box": {"x": 0, "y": 0, "width": 100, "height": 80}})
        assert prepared.status_code == 202 and prepared.get_json()["level"] == 0
        for _ in range(200):
            state = client.get("/volume/big/prepare").get_json()
            if (state.get("prepare") or {}).get("status") not in (None, "pending"):
                break
            time.sleep(0.05)
        assert state["prepare"]["status"] == "ready" and state["cached_levels"] == [0]
        assert client.post("/volume/big/prepare", json={"box": {}}).status_code == 400
    finally:
        layer_jobs.forget("big")


def test_eviction_passes_over_a_shard_a_reader_is_using(tmp_path, small_levels, monkeypatch):
    """Reads no longer hold the store's lock (they pin their shards): a shard
    pinned by a read in progress is not deleted under it."""
    path, data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[1])
        rows, cols = slice(40, 140), slice(25, 190)
        names = [f"0/{shard}" for shard in store.shards_for(0, 0, 1, 0, 39, rows, cols)]
        with store.lock:
            store.pin(names)
        monkeypatch.setattr(bricks, "CACHE_BUDGET", 1)
        bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[0])
        assert store.holds(0, 0, 1, 0, 39, rows, cols), "a pinned shard was evicted"
        store.unpin(names)
        with store.lock:
            bricks._evict(store)
        assert not store.holds(0, 0, 1, 0, 39, rows, cols), "unpinned, it goes as before"
    finally:
        source.close()


def test_a_read_does_not_hold_the_store_lock_while_it_decodes(tmp_path, small_levels):
    path, data, source = _stack(tmp_path)
    try:
        store = _store(tmp_path, path, source)
        bricks.build_coarse(store, source)
        bricked = bricks.adopt(source, store)
        level = bricks.coarse_levels(source.volume)[0]
        seen = []
        original = store.array

        def array(lvl, create=False):
            seen.append(store.lock.locked())
            return original(lvl, create)

        store.array = array
        height, width = source.level_shape(level)
        block = bricked.read_block(level, 0, 0, 0, 39, slice(0, height), slice(0, width))
        assert seen and not any(seen), "the brick read ran under the store's lock"
        assert np.array_equal(block, np.asarray(source.read_block(level, 0, 0, 0, 39,
                                                                  slice(0, height), slice(0, width))))
    finally:
        source.close()
