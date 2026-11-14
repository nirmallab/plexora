"""A coarse level of a stack's 3-D copy is never evicted (volume/bricks.py).

The section panes can prepare an area at a level the background copy has not
finished yet. Once the copy finishes that level it holds every shard of it, and
the copy's coarse levels are never evicted: the cache of prepared areas making
room for a finer area must not delete a shard of a finished coarse level, which
would then read as zeros.
"""

import threading

import numpy as np
import pytest

from plexora.server.volume import bricks
from plexora.server.volume.sources import open_volume
from tests import volume_fixtures as vf


@pytest.fixture
def small_levels(monkeypatch):
    # Level 1 (150 x 130) of a 40-plane 16-bit stack fits; level 0 does not.
    monkeypatch.setattr(bricks, "LEVEL_BUDGET", 40 * 150 * 130 * 2)


def test_an_area_prepared_before_the_copy_finished_its_level_is_not_evicted_from_it(
        tmp_path, small_levels, monkeypatch):
    path, data = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=2,
                                     depth=40, height=300, width=260, levels=3)
    source = open_volume(path, z_semantics="depth")
    try:
        assert bricks.coarse_levels(source.volume) == [1, 2]
        store = bricks.Store(tmp_path / "volume_v1", source.volume, bricks.source_key(path))
        # The panes prepare the area on screen at level 1 while the copy has
        # not reached it yet...
        bricks.prepare(store, source, 1, (0, 0, 130, 150))
        # ...then the background copy finishes every coarse level.
        bricks.build_coarse(store, source)
        assert store.coarse_complete()
        # Later areas at level 0 push the prepared-area cache past its budget.
        monkeypatch.setattr(bricks, "CACHE_BUDGET", 1)
        bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[0])

        bricked = bricks.adopt(source, store)
        info = source.volume.levels[1]
        rows, cols = slice(0, info.height), slice(0, info.width)
        for channel in range(source.volume.channels):
            expected = source.read_block(1, 0, channel, 0, 39, rows, cols)
            got = bricked.read_block(1, 0, channel, 0, 39, rows, cols)
            assert np.count_nonzero(expected) > 0
            zeros = int(np.count_nonzero((got == 0) & (expected != 0)))
            assert zeros == 0, (f"channel {channel}: {zeros} of {expected.size} voxels of the "
                                f"finished coarse level 1 read as 0 where the stack has none")
            assert np.array_equal(got, expected)
    finally:
        source.close()


def test_an_area_prepared_at_a_level_still_being_copied_is_not_evicted_from_it(
        tmp_path, small_levels, monkeypatch):
    """The same, with the eviction while the copy of that level is under way:
    a unit the copy has written may share a file with a prepared shard."""
    path, data = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=2,
                                     depth=40, height=300, width=260, levels=3)
    source = open_volume(path, z_semantics="depth")
    try:
        store = bricks.Store(tmp_path / "volume_v1", source.volume, bricks.source_key(path))
        bricks.prepare(store, source, 1, (0, 0, 130, 150), channels=[0])
        # The copy stops after level 1's channel 0 (both blocks of planes)...
        cancel = threading.Event()
        steps = []

        def report(_percent, _message):
            steps.append(_percent)
            if len(steps) == 2:
                cancel.set()

        bricks.build_coarse(store, source, report=report, cancel=cancel)
        assert not store.has_coarse(1)
        # ...a finer area pushes the cache past its budget meanwhile...
        monkeypatch.setattr(bricks, "CACHE_BUDGET", 1)
        bricks.prepare(store, source, 0, (20, 30, 200, 150), channels=[0])
        # ...and then the copy finishes.
        bricks.build_coarse(store, source)
        assert store.coarse_complete()

        bricked = bricks.adopt(source, store)
        info = source.volume.levels[1]
        rows, cols = slice(0, info.height), slice(0, info.width)
        expected = source.read_block(1, 0, 0, 0, 39, rows, cols)
        got = bricked.read_block(1, 0, 0, 0, 39, rows, cols)
        zeros = int(np.count_nonzero((got == 0) & (expected != 0)))
        assert zeros == 0, f"{zeros} voxels of level 1 read as 0 where the stack has none"
    finally:
        source.close()


def test_a_build_json_that_lost_shards_of_a_finished_level_copies_it_again(tmp_path):
    """A manifest written before the fix may list prepared shards on a
    finished coarse level, some of them already evicted: that level, and the
    pooled levels built from it, are copied again rather than trusted."""
    raw = {"version": bricks.FORMAT_VERSION, "source_key": "k",
           "coarse": {"1": True, "2": True}, "pooled": {"1p4": True, "2p4": True},
           "shards": {"1": {"0.0.0.0.0": 100}, "0": {"0.0.0.0.0": 50}}, "bytes": 150,
           "units": ["1.0.0.0", "1p4.0.0.0", "2.0.0.0", "2p4.0.0.0", "10.0.0.0"]}
    bricks.Store._repair_coarse(raw)
    assert raw["coarse"] == {"2": True}
    assert raw["pooled"] == {"2p4": True}
    assert raw["shards"] == {"0": {"0.0.0.0.0": 50}}
    assert raw["bytes"] == 50
    assert raw["units"] == ["2.0.0.0", "2p4.0.0.0", "10.0.0.0"]
