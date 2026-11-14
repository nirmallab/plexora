"""A second load of a stack works on the same 3-D copy as the first
(volume/bricks.py `store_for`, called by `data_model._with_bricks`).

Every load of a large stack builds its store with `store_for`. The first open
starts the background copy, which runs for minutes on a light-sheet-sized
stack and is not stopped when another project opens. Reopening the stack
before that copy finishes (A, then B, then A again) loads it a second time:
the task already running is left alone (layer_jobs.start_task) and keeps
writing through the first load's store. The second load must still see the
copy finish -- its sections and 3-D view read from the copy once it is
there -- and nothing it later writes (a prepared area) may put back the
manifest as it was when it loaded, which loses the copy's progress so the
next open copies the whole stack again.
"""

import pytest

from plexora.server.volume import bricks
from plexora.server.volume.sources import open_volume
from tests import volume_fixtures as vf


@pytest.fixture
def small_levels(monkeypatch):
    # Level 1 (150 x 130) of a 40-plane 16-bit stack fits; level 0 does not.
    monkeypatch.setattr(bricks, "LEVEL_BUDGET", 40 * 150 * 130 * 2)


class _StopAfter:
    """A cancel flag set once the source has been read `n` times."""

    def __init__(self, reads, n):
        self.reads, self.n = reads, n

    def is_set(self):
        return len(self.reads) >= self.n


def _first_load_copies_part_way(tmp_path, monkeypatch, name):
    path, _data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                      depth=40, height=300, width=260, levels=3)
    monkeypatch.setattr("plexora.paths.derived_root", lambda _name, *a, **k: tmp_path / name)
    source = open_volume(path, z_semantics="depth")
    reads = []
    original = source.read_block

    def counted(*args):
        reads.append(args)
        return original(*args)

    source.read_block = counted
    first = bricks.store_for(name, source)
    first.reset_if_stale()
    assert bricks.coarse_levels(source.volume) == [1, 2]
    # The first load's background copy has written some of its blocks...
    bricks.build_coarse(first, source, cancel=_StopAfter(reads, 3))
    assert not first.coarse_complete()
    return source, first


def _second_load(name, source):
    # ...when the stack is opened again: what `_with_bricks` does on that load.
    second = bricks.store_for(name, source)
    second.reset_if_stale()
    assert not second.coarse_complete()
    return second


def test_a_second_load_sees_the_copy_the_first_load_started_finish(
        tmp_path, small_levels, monkeypatch):
    source, first = _first_load_copies_part_way(tmp_path, monkeypatch, "reopened")
    try:
        second = _second_load("reopened", source)
        # The first load's copy carries on and finishes.
        bricks.build_coarse(first, source)
        assert first.coarse_complete()
        assert second.coarse_complete(), (
            "the copy finished, yet the store of the load on screen still says it "
            "holds none of it: every section and 3-D read goes to the stack itself")
        assert second.holds(1, 0, 1, 0, 39, slice(0, 150), slice(0, 130))
    finally:
        source.close()


def test_an_area_prepared_after_a_second_load_keeps_the_finished_copy_on_disk(
        tmp_path, small_levels, monkeypatch):
    source, first = _first_load_copies_part_way(tmp_path, monkeypatch, "prepared")
    try:
        second = _second_load("prepared", source)
        bricks.build_coarse(first, source)
        assert bricks.store_for("prepared", source).coarse_complete()
        # The section panes prepare an area through the load on screen.
        done = bricks.prepare(second, source, 0, (20, 30, 200, 150), channels=[1])
        assert done["shards"] == 2
        # The next open of the stack still finds the whole copy.
        later = bricks.store_for("prepared", source)
        later.reset_if_stale()
        assert later.coarse_complete(), (
            f"after a prepare through the second load, build.json says coarse="
            f"{later.manifest()['coarse']} with {len(later.manifest().get('units', []))} "
            f"units: the next open copies the stack again")
        assert later.holds(0, 0, 1, 0, 39, slice(40, 140), slice(25, 190))
    finally:
        source.close()
