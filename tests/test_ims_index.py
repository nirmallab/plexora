"""A large local Imaris file's chunk index, walked once and kept.

server/volume/sources/ims_index.py saves where every chunk of a file is; the
reader (ims.py) then reads chunks by offset, outside its lock, with no HDF5
call to find them. Pinned: every read with an index is exactly what h5py
returns, for each filter pipeline and for chunks never written; an index whose
file has changed, or that is damaged, is not used; one that is wrong in any
way costs speed, never the picture; and the builder starts once, in the
background, only for a large file on a disk with no index, none on its way
and no failed attempt.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from plexora import paths
from plexora.server.volume import chunk_cache
from plexora.server.volume.sources import ims as ims_source
from plexora.server.volume.sources import ims_index
from tests import volume_fixtures as vf

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def fresh_cache(monkeypatch):
    cache = chunk_cache.ChunkCache(64 * 2**20)
    monkeypatch.setattr(chunk_cache, "CACHE", cache)
    return cache


@pytest.fixture()
def no_builder(monkeypatch):
    """No background builder from `open_ims_volume`: each test builds itself."""
    started = []
    monkeypatch.setattr(ims_index, "ensure", lambda path: started.append(path) or False)
    return started


def _lz4():
    import hdf5plugin

    return hdf5plugin.LZ4()


def _stored(path, level, channel, z0, z1, rows, cols):
    import h5py

    with h5py.File(path, "r") as handle:
        data = handle[f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel {channel}/Data"]
        return np.asarray(data[z0:z1 + 1, rows, cols])


def _counting_reads(source):
    reads = []
    real = source._raw.read
    source._raw.read = lambda offset, size: reads.append(offset) or real(offset, size)
    return reads


@pytest.mark.parametrize("compression,shuffle", [(None, False), ("gzip", False), ("gzip", True),
                                                 ("lz4", True)])
def test_with_an_index_every_read_is_what_h5py_returns_and_by_offset(
        tmp_path, fresh_cache, no_builder, compression, shuffle):
    if compression == "lz4":
        compression = _lz4()
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=2, depth=9, height=70, width=53,
                           compression=compression, shuffle=shuffle, chunks=(4, 32, 16))
    ims_index.build(path, workers=1)
    source = ims_source.open_ims_volume(path)
    reads = _counting_reads(source)
    for level in (0, 1):
        info = source._levels[level]
        for z0, z1, rows, cols in ((0, 8, slice(0, info["height"]), slice(0, info["width"])),
                                   (3, 5, slice(7, 41), slice(15, 33))):
            z1 = min(z1, info["depth"] - 1)
            z0 = min(z0, z1)
            rows_in = slice(rows.start, min(rows.stop, info["height"]))
            cols_in = slice(cols.start, min(cols.stop, info["width"]))
            got = source.read_block(level, 0, 1, z0, z1, rows, cols)
            assert np.array_equal(got, _stored(path, level, 1, z0, z1, rows_in, cols_in)), \
                (level, z0, z1, rows, cols)
    assert source._chunk_index is not None
    assert reads, "with an index, chunks are read by offset"
    assert source._direct == {(0, 0, 1): True, (1, 0, 1): True}
    source.close()


def test_the_index_is_walked_by_worker_processes_the_same(tmp_path):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=3, depth=8, height=64, width=64,
                           compression="gzip", chunks=(4, 16, 16))
    ims_index.build(path, workers=1)
    one = ims_index.load(path)
    single = {key: [a.copy() for a in one.grid(*key)] for key in ((0, 0, 0), (1, 0, 2))}
    one.close()
    ims_index.index_path(ims_index.identity(path)).unlink()
    ims_index.build(path, workers=2)
    two = ims_index.load(path)
    for key, arrays in single.items():
        assert all(np.array_equal(a, b) for a, b in zip(arrays, two.grid(*key)))
    two.close()


def test_a_chunk_never_written_reads_as_the_fill_value(tmp_path, fresh_cache, no_builder):
    import h5py

    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           levels=1, chunks=(4, 16, 16))
    with h5py.File(path, "r+") as handle:
        group = handle["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"]
        del group["Data"]
        data = group.create_dataset("Data", shape=(8, 72, 72), chunks=(4, 16, 16),
                                    dtype="uint16", fillvalue=7, compression="gzip")
        data[0:4, 0:16, 0:16] = 300        # one chunk written, the rest never
    ims_index.build(path, workers=1)
    offsets, _, _ = ims_index.load(path).grid(0, 0, 0)
    assert offsets[0, 0, 0] >= 0 and (offsets < 0).sum() == offsets.size - 1
    source = ims_source.open_ims_volume(path)
    got = source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 7, slice(0, 64), slice(0, 64)))
    assert got[0, 0, 0] == 300 and got[5, 40, 40] == 7
    source.close()


def test_an_index_whose_file_changed_is_not_used(tmp_path, fresh_cache, no_builder):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, compression="gzip")
    ims_index.build(path, workers=1)
    assert ims_index.load(path) is not None
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert ims_index.load(path) is None
    source = ims_source.open_ims_volume(path)
    got = source.read_block(0, 0, 0, 0, 5, slice(0, 96), slice(0, 80))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 5, slice(0, 96), slice(0, 80)))
    assert source._chunk_index is None
    source.close()


def test_a_damaged_index_is_not_used(tmp_path, fresh_cache, no_builder):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, compression="gzip")
    where = ims_index.build(path, workers=1)
    where.write_bytes(b"not an index")
    assert ims_index.load(path) is None
    source = ims_source.open_ims_volume(path)
    got = source.read_block(0, 0, 0, 0, 5, slice(0, 96), slice(0, 80))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 5, slice(0, 96), slice(0, 80)))
    source.close()


def test_a_wrong_index_costs_speed_never_the_picture(tmp_path, fresh_cache, no_builder):
    """The first chunk read by offset is compared with h5py's own read of it:
    offsets that point elsewhere send the dataset back to h5py's way."""
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, compression="gzip", chunks=(4, 16, 16))
    ims_index.build(path, workers=1)
    source = ims_source.open_ims_volume(path)
    index = source._chunk_index = ims_index.load(path)
    offsets, sizes, masks = index.grid(0, 0, 0)
    index._grids["L0_T0_C0"] = (offsets + 8, sizes, masks)
    got = source.read_block(0, 0, 0, 0, 5, slice(0, 96), slice(0, 80))
    assert np.array_equal(got, _stored(path, 0, 0, 0, 5, slice(0, 96), slice(0, 80)))
    assert source._direct == {(0, 0, 0): False}
    source.close()


def test_without_an_index_a_reader_looks_again_now_and_then(tmp_path, fresh_cache, no_builder,
                                                            monkeypatch):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, compression="gzip", chunks=(4, 16, 16))
    source = ims_source.open_ims_volume(path)
    looks = []
    real = ims_index.load
    monkeypatch.setattr(ims_index, "load", lambda p: looks.append(p) or real(p))
    source.read_block(0, 0, 0, 0, 3, slice(0, 16), slice(0, 16))
    source.read_block(0, 0, 0, 0, 3, slice(16, 32), slice(0, 16))
    assert len(looks) == 1, "looked for again within POLL_S"
    ims_index.build(path, workers=1)
    source._index_looked -= ims_index.POLL_S
    source.read_block(0, 0, 0, 0, 3, slice(32, 48), slice(0, 16))
    assert len(looks) == 2 and source._chunk_index is not None
    source.close()


# -- starting the builder -------------------------------------------------------


@pytest.fixture()
def spawned(monkeypatch):
    """Popen calls `ensure` makes, recorded rather than run."""
    calls = []
    monkeypatch.setattr(ims_index.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(ims_index, "_started", set())
    monkeypatch.delenv(ims_index.ENV_SWITCH, raising=False)
    return calls


def test_a_small_file_is_not_indexed(tmp_path, spawned):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1)
    assert ims_index.ensure(path) is False and spawned == []


def test_a_large_file_starts_one_builder_once(tmp_path, spawned, monkeypatch):
    monkeypatch.setattr(ims_index, "MIN_BYTES", 0)
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1)
    assert ims_index.ensure(path) is True
    assert ims_index.ensure(path) is False
    assert len(spawned) == 1 and spawned[0][1:] == ["-m", "plexora.server.volume.sources.ims_index",
                                                     os.path.abspath(path)]


@pytest.mark.parametrize("why", ["switched off", "built", "failed", "being built"])
def test_no_builder_when(tmp_path, spawned, monkeypatch, why):
    monkeypatch.setattr(ims_index, "MIN_BYTES", 0)
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1)
    ident = ims_index.identity(path)
    if why == "switched off":
        monkeypatch.setenv(ims_index.ENV_SWITCH, "0")
    elif why == "built":
        ims_index.build(path, workers=1)
    elif why == "failed":
        ims_index._failed_path(ident).parent.mkdir(parents=True, exist_ok=True)
        ims_index._failed_path(ident).write_text("RuntimeError: no\n")
    else:
        assert ims_index._lock(ident)
    assert ims_index.ensure(path) is False and spawned == []


def test_a_dead_builders_lock_is_taken_over(tmp_path):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1)
    ident = ims_index.identity(path)
    assert ims_index._lock(ident)
    assert not ims_index._lock(ident), "a live builder's lock is respected"
    lock = ims_index._lock_path(ident)
    held = json.loads(lock.read_text())
    lock.write_text(json.dumps({**held, "pid": 2**22 + 12345}))     # no such process
    assert ims_index._lock(ident)
    lock.write_text(json.dumps({**held, "host": "elsewhere", "started": time.time() - 2 * 3600}))
    assert ims_index._lock(ident), "a lock older than STALE_LOCK_S is taken over"


def test_the_builder_runs_as_its_own_process(tmp_path):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=2, compression="gzip")
    env = {**os.environ, "PLEXORA_DATA_PATH": str(paths.data_root())}
    run = [sys.executable, "-m", "plexora.server.volume.sources.ims_index", str(path)]
    first = subprocess.run(run, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    assert "indexed in" in first.stdout
    where = ims_index.index_path(ims_index.identity(path))
    assert where.exists() and not ims_index._lock_path(ims_index.identity(path)).exists()
    built = where.stat().st_mtime_ns
    again = subprocess.run(run, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert again.returncode == 0 and where.stat().st_mtime_ns == built, "built once"


def test_a_build_that_fails_is_noted_and_not_tried_again(tmp_path, spawned, monkeypatch):
    monkeypatch.setattr(ims_index, "MIN_BYTES", 0)
    path = tmp_path / "not.ims"
    path.write_bytes(b"not an HDF5 file")
    assert ims_index.main([str(path)]) == 1
    assert ims_index._failed_path(ims_index.identity(path)).exists()
    assert ims_index.ensure(path) is False and spawned == []
