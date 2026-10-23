"""Decoded chunks, kept once and decompressed outside the reader's lock.

server/volume/chunk_cache.py holds decoded chunks for the whole process, one
load per chunk however many threads want it; an Imaris file's deflated or
LZ4 chunks are read raw under its lock and decompressed outside it
(server/volume/sources/ims.py). Pinned: every read is exactly what h5py
returns, for each filter pipeline and at the padded edges; a plane next to one
already read decompresses nothing; nothing is decompressed with the lock held;
and a file on disk never looks a chunk up by its coordinates.
"""

import threading

import numpy as np
import pytest

from plexora.server.volume import chunk_cache
from plexora.server.volume.sources import ims as ims_source
from tests import volume_fixtures as vf


@pytest.fixture()
def fresh_cache(monkeypatch):
    cache = chunk_cache.ChunkCache(64 * 2**20)
    monkeypatch.setattr(chunk_cache, "CACHE", cache)
    return cache


def _open(path):
    return ims_source.open_ims_volume(path)


class _OwnedLock:
    """The reader's lock, able to say whether THIS thread holds it: chunks are
    read under it on one thread while another decodes, so "is it held" is not
    the question."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def acquire(self, *args, **kwargs):
        got = self._lock.acquire(*args, **kwargs)
        if got:
            self._owner = threading.get_ident()
        return got

    def release(self):
        self._owner = None
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()

    def locked(self):
        return self._lock.locked()

    def held_here(self):
        return self._owner == threading.get_ident()


def _lz4():
    """hdf5plugin's LZ4 filter, as Imaris 10 can write it."""
    import hdf5plugin

    return hdf5plugin.LZ4()


@pytest.mark.parametrize("compression,shuffle", [(None, False), ("gzip", False), ("gzip", True),
                                                 ("lz4", False), ("lz4", True)])
def test_every_read_is_what_h5py_returns(tmp_path, fresh_cache, compression, shuffle):
    import h5py

    if compression == "lz4":
        compression = _lz4()

    path, _data = vf.write_ims(tmp_path / "s.ims", channels=2, depth=9, height=70, width=53,
                               compression=compression, shuffle=shuffle, chunks=(4, 32, 16))
    source = _open(path)
    with h5py.File(path, "r") as handle:
        for level in (0, 1):
            info = source._levels[level]
            stored = handle[f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel 1/Data"]
            for z0, z1, rows, cols in ((0, 8, slice(0, info["height"]), slice(0, info["width"])),
                                       (3, 5, slice(7, 41), slice(15, 33)),
                                       (8, 8, slice(info["height"] - 3, info["height"]), slice(0, 5))):
                z1 = min(z1, info["depth"] - 1)
                z0 = min(z0, z1)
                got = source.read_block(level, 0, 1, z0, z1, rows, cols)
                # Imaris pads a dataset to its chunks; reads stop at the real extent.
                rows_in = slice(rows.start, min(rows.stop, info["height"]))
                cols_in = slice(cols.start, min(cols.stop, info["width"]))
                assert np.array_equal(got, stored[z0:z1 + 1, rows_in, cols_in]), \
                    (level, z0, z1, rows, cols)
                plane = source.plane_pyramid(0, z0)[level][1, rows_in, cols_in]
                assert np.array_equal(np.asarray(plane), stored[z0, rows_in, cols_in])
    source.close()


def test_a_plane_beside_one_read_decompresses_nothing(tmp_path, fresh_cache):
    path, data = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                              compression="gzip", chunks=(8, 32, 32))
    source = _open(path)
    first = source.read_block(0, 0, 0, 2, 2, slice(0, 64), slice(0, 64))
    loads = fresh_cache.loads
    misses = fresh_cache.misses
    second = source.read_block(0, 0, 0, 3, 3, slice(0, 64), slice(0, 64))
    assert np.array_equal(first[0], data[0, 0, 2]) and np.array_equal(second[0], data[0, 0, 3])
    assert fresh_cache.loads == loads and fresh_cache.misses == misses, \
        "the next plane is in the chunks the first read decoded"
    source.close()


def test_nothing_is_inflated_with_the_lock_held(tmp_path, fresh_cache, monkeypatch):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           compression="gzip", chunks=(4, 16, 16))
    source = _open(path)
    source._io = _OwnedLock()
    held = []
    real = ims_source._inflate

    def inflate(raw):
        held.append(source._io.held_here())
        return real(raw)

    monkeypatch.setattr(ims_source, "_inflate", inflate)
    source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert held and not any(held), "inflate ran under the file's lock"
    source.close()


def test_lz4_chunks_are_decoded_here_outside_the_lock(tmp_path, fresh_cache, monkeypatch):
    path, data = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                              compression=_lz4(), shuffle=True, chunks=(4, 16, 16))
    source = _open(path)
    source._io = _OwnedLock()
    held = []
    real = ims_source._unlz4

    def unlz4(raw):
        held.append(source._io.held_here())
        return real(raw)

    monkeypatch.setattr(ims_source, "_unlz4", unlz4)
    got = source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert np.array_equal(got, data[0, 0])
    assert held and not any(held), "LZ4 read through h5py, or decoded under the file's lock"
    source.close()


def test_a_file_on_disk_never_looks_a_chunk_up_by_its_coordinates(tmp_path, fresh_cache):
    """`get_chunk_info_by_coord` is linear in a dataset's chunks: ~10 ms a call
    on a real Imaris channel, longer than reading the chunk. A local file reads
    each chunk with `read_direct_chunk` instead, never by offset."""
    path, data = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                              compression="gzip", chunks=(4, 16, 16))
    source = _open(path)
    reads = []
    real = source._raw.read
    source._raw.read = lambda offset, size: reads.append(offset) or real(offset, size)
    got = source.read_block(0, 0, 0, 0, 7, slice(0, 64), slice(0, 64))
    assert np.array_equal(got, data[0, 0])
    assert reads == [], "a local chunk was located and read by offset"
    source.close()


def test_one_load_per_chunk_however_many_threads_ask():
    cache = chunk_cache.ChunkCache(2**20)
    started = threading.Event()
    release = threading.Event()
    loads = []

    def load_many(keys):
        loads.append(list(keys))
        started.set()
        release.wait(5)
        return {key: np.zeros(4, np.uint8) for key in keys}

    results = []
    first = threading.Thread(target=lambda: results.append(cache.fetch(["a", "b"], load_many)))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.append(cache.fetch(["b"], load_many)))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert loads == [["a", "b"]] and len(results) == 2 and cache.waits == 1


def test_the_budget_holds_and_a_scan_keeps_nothing():
    cache = chunk_cache.ChunkCache(1000)
    for i in range(5):
        cache.put(i, np.zeros(300, np.uint8))
    assert cache.stats()["bytes"] <= 1000 and cache.get(0) is None and cache.get(4) is not None
    cache.fetch(["scan"], lambda keys: {k: np.zeros(10, np.uint8) for k in keys}, keep=False)
    assert cache.get("scan") is None
    with pytest.raises(ValueError):
        cache.get(4)[0] = 1
