"""The website's chunk cache, kept in the site's own storage.

plexora/browser/cachefiles.py packs the values the chunk cache keeps into a
fixed set of files the page opened (workers/cacheFiles.js), and keeps the
SQLite index in memory with a snapshot in two of them. A cache under /data
lived in the worker's memory (256 MB at most) and was copied into the user's
projects folder besides.

`Files` stands in for the page's files: the same five calls, over bytearrays.
"""

from __future__ import annotations

import errno
import os

import numpy as np
import pytest

from plexora.browser import buckets, cachefiles
from plexora.server.utils import remote_store
from tests.remote_fixtures import cache_root  # noqa: F401
from tests.test_browser_buckets import Cloud

MB = 1024 ** 2


class Files:
    """The page's files: `count` of them, each a bytearray."""

    def __init__(self, count=2 + 8, limit=None):
        self.count = count
        self.data = [bytearray() for _ in range(count)]
        self.limit = limit
        self.writes = 0

    def size(self, i):
        return len(self.data[i])

    def read(self, i, at, length):
        return bytes(self.data[i][at:at + length])

    def write(self, i, at, data):
        if self.limit is not None and sum(map(len, self.data)) + len(data) > self.limit:
            raise OSError(errno.ENOSPC, "QuotaExceededError")
        buffer = self.data[i]
        if len(buffer) < at:
            buffer.extend(b"\0" * (at - len(buffer)))
        buffer[at:at + len(data)] = data
        self.writes += 1

    def truncate(self, i, length):
        del self.data[i][length:]

    def flush(self, i):
        pass


def _index(tmp_path, files, budget=8 * MB, quota=None):
    return cachefiles.PackedCacheIndex(tmp_path / "cache", files, quota=quota, budget=budget)


def _put(index, store, key, data, rng=remote_store._FULL):
    assert index.accommodate(len(data))
    index.write_value(store, key, rng, data)
    index.record(store, key, rng, len(data))


@pytest.fixture
def files():
    return Files()


def test_values_are_read_back_from_the_page_files(tmp_path, files):
    index = _index(tmp_path, files)
    index.register_store("s1", "s3://lab/a.zarr")
    _put(index, "s1", "0/c/0/0", b"chunk-one")
    _put(index, "s1", "slide.tif", b"x" * 1000, (0, 1000))
    assert index.read_value("s1", "0/c/0/0") == b"chunk-one"
    assert index.read_value("s1", "slide.tif", (0, 1000)) == b"x" * 1000
    with pytest.raises(FileNotFoundError):
        index.read_value("s1", "0/c/0/1")
    # Nothing under the in-memory root but the index itself.
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == ["index.sqlite"]
    assert sum(map(len, files.data[2:])) > 1000


def test_a_reload_finds_what_was_cached(tmp_path, files):
    index = _index(tmp_path, files)
    index.register_store("s1", "s3://lab/a.zarr")
    _put(index, "s1", "0/c/0/0", b"kept across visits")
    index.pin("s1")
    assert index.keep(force=True)
    index.close()

    again = _index(tmp_path / "next-visit", files)
    assert again.read_value("s1", "0/c/0/0") == b"kept across visits"
    assert again.lookup("s1", "0/c/0/0").size == len(b"kept across visits")
    assert again.is_pinned("s1")
    assert again.usage()["used_bytes"] == len(b"kept across visits")


def test_a_snapshot_that_does_not_read_whole_falls_back_to_the_other(tmp_path, files):
    index = _index(tmp_path, files)
    index.register_store("s1", "s3://lab/a.zarr")
    _put(index, "s1", "a", b"first")
    index.keep(force=True)
    _put(index, "s1", "b", b"second")
    index.keep(force=True)
    index.close()
    newest = max((0, 1), key=lambda slot: cachefiles.SNAPSHOT.unpack_from(bytes(files.data[slot]))[2])
    files.data[newest][-10:] = b"\xff" * 10  # a write cut short

    again = _index(tmp_path / "next-visit", files)
    assert again.read_value("s1", "a") == b"first"
    assert again.lookup("s1", "b") is None


def test_a_row_for_bytes_since_overwritten_reads_as_a_miss(tmp_path, files):
    """A snapshot older than the segments: its rows may point at bytes a later
    write put there. The header says so; the reader never gets them."""
    index = _index(tmp_path, files)
    index.register_store("s1", "s3://lab/a.zarr")
    _put(index, "s1", "a", b"A" * 100)
    index.keep(force=True)
    stale = bytes(files.data[0]), bytes(files.data[1])
    index.clear()
    _put(index, "s1", "b", b"B" * 100)  # the same place in the same segment
    index.keep(force=True)
    index.close()
    files.data[0][:], files.data[1][:] = stale  # back to the old snapshot

    again = _index(tmp_path / "next-visit", files)
    assert again.lookup("s1", "a") is not None
    with pytest.raises(FileNotFoundError):
        again.read_value("s1", "a")


def test_the_disk_never_holds_much_more_than_the_budget(tmp_path, files):
    budget = 8 * MB  # eight segments of 1 MB
    index = _index(tmp_path, files, budget=budget)
    index.register_store("s1", "s3://lab/a.zarr")
    block = bytes(range(256)) * 1024  # 256 KB
    for n in range(200):
        _put(index, "s1", f"block/{n}", block)
        assert index.disk_bytes() <= budget + budget // 8
    assert index.usage()["used_bytes"] <= budget
    # The newest are there; the oldest have gone.
    assert index.read_value("s1", "block/199") == block
    assert index.lookup("s1", "block/0") is None
    with pytest.raises(FileNotFoundError):
        index.read_value("s1", "block/0")


def test_what_is_read_again_outlives_what_is_not(tmp_path, files):
    index = _index(tmp_path, files, budget=8 * MB)
    index.register_store("s1", "s3://lab/a.zarr")
    block = b"\x01" * (256 * 1024)
    _put(index, "s1", "popular", block)
    for n in range(120):
        _put(index, "s1", f"once/{n}", block)
        if n % 8 == 0:
            index.read_value("s1", "popular")
            index.touch("s1", "popular")
    assert index.read_value("s1", "popular") == block
    assert index.lookup("s1", "once/0") is None


def test_a_store_kept_offline_is_never_evicted(tmp_path, files):
    index = _index(tmp_path, files, budget=8 * MB)
    index.register_store("kept", "s3://lab/kept.zarr")
    index.register_store("seen", "s3://lab/seen.zarr")
    block = b"\x02" * (256 * 1024)
    for n in range(4):
        _put(index, "kept", f"k/{n}", block)
    index.pin("kept")
    for n in range(150):
        _put(index, "seen", f"s/{n}", block)
    for n in range(4):
        assert index.read_value("kept", f"k/{n}") == block
    assert index.pinned_bytes() == 4 * len(block)


def test_a_value_larger_than_a_segment_is_served_without_being_kept(tmp_path, files):
    index = _index(tmp_path, files, budget=8 * MB)
    assert not index.accommodate(2 * MB)
    assert index.accommodate(512 * 1024)


def test_the_budget_is_the_setting_capped_by_what_the_browser_grants(tmp_path, files, monkeypatch):
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", str(20 * 1024 ** 3))
    index = cachefiles.PackedCacheIndex(tmp_path / "cache", files, quota=10 * 1024 ** 3)
    assert index.budget == int(10 * 1024 ** 3 * cachefiles.QUOTA_SHARE)
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", str(2 * 1024 ** 3))
    assert index.budget == 2 * 1024 ** 3
    # The desktop's default, where the browser grants plenty: several GB.
    monkeypatch.delenv("PLEXORA_REMOTE_CACHE_BYTES")
    roomy = cachefiles.PackedCacheIndex(tmp_path / "roomy", Files(), quota=500 * 1024 ** 3)
    assert roomy.budget == 10 * 1024 ** 3


def test_a_smaller_budget_is_applied_at_once(tmp_path, files):
    index = _index(tmp_path, files, budget=8 * MB)
    index.register_store("s1", "s3://lab/a.zarr")
    block = b"\x03" * (256 * 1024)
    for n in range(40):
        _put(index, "s1", f"b/{n}", block)
    index.set_budget(2 * MB)
    assert index.disk_bytes() <= 2 * MB + index.segment_bytes() * 4
    assert index.read_value("s1", "b/39") == block


def test_a_browser_that_keeps_less_than_it_said_is_lived_with(tmp_path):
    files = Files(limit=3 * MB)
    index = _index(tmp_path, files, budget=8 * MB)
    index.register_store("s1", "s3://lab/a.zarr")
    block = b"\x04" * (256 * 1024)
    kept = 0
    for n in range(40):
        try:
            _put(index, "s1", f"b/{n}", block)
            kept += 1
        except OSError:
            pass
    assert kept >= 30
    assert index.budget < 8 * MB
    assert index.read_value("s1", f"b/{39}") == block or index.lookup("s1", "b/39") is None


def test_clearing_empties_the_files(tmp_path, files):
    index = _index(tmp_path, files)
    index.register_store("s1", "s3://lab/a.zarr")
    index.register_store("s2", "s3://lab/b.zarr")
    _put(index, "s1", "a", b"1" * 100)
    _put(index, "s2", "b", b"2" * 100)
    index.clear("s1")
    with pytest.raises(FileNotFoundError):
        index.read_value("s1", "a")
    assert index.read_value("s2", "b") == b"2" * 100
    index.clear()
    assert index.disk_bytes() == 0
    assert index.usage()["used_bytes"] == 0


# -- through remote_store, as the website reads a bucket ----------------------


@pytest.fixture
def page(cache_root, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cachefiles, "ROOT", str(tmp_path / "cache"))
    stand_in = Cloud()
    buckets.install(stand_in)
    files = Files(count=2 + 8)
    cachefiles.install(files, quota=None)
    yield stand_in, files
    cachefiles.uninstall()
    buckets.uninstall()
    remote_store._reset_for_tests()


def test_a_bucket_read_on_the_website_is_cached_in_the_site_storage(page, monkeypatch):
    import zarr

    cloud, files = page
    monkeypatch.setattr(cachefiles, "ROOT", cachefiles.ROOT)
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", str(64 * MB))
    data = np.arange(64 * 80, dtype=np.uint16).reshape(64, 80)
    memory = zarr.storage.MemoryStore()
    array = zarr.create_array(memory, name="0", shape=data.shape, chunks=(32, 32), dtype=data.dtype)
    array[:] = data
    cloud.s3["lab"] = {f"img.zarr/{key}": value.to_bytes() for key, value in memory._store_dict.items()}

    store, subpath = remote_store.open_store("s3://lab/img.zarr/0")
    assert np.array_equal(zarr.open_array(store=store, path=subpath, mode="r")[:], data)
    index = remote_store.cache_index()
    assert isinstance(index, cachefiles.PackedCacheIndex)
    assert str(index.root) == cachefiles.ROOT
    assert sum(map(len, files.data[2:])) > 0
    assert cachefiles.keep(force=True)

    # A new visit: a new index over the same files, and no request at all.
    remote_store._reset_for_tests()
    cachefiles.install(files, quota=None)
    fetched = len(cloud.requests)
    store, subpath = remote_store.open_store("s3://lab/img.zarr/0")
    assert np.array_equal(zarr.open_array(store=store, path=subpath, mode="r")[:], data)
    data_requests = [r for r in cloud.requests[fetched:] if "/c/" in str(r)]
    assert data_requests == []


def test_without_the_files_the_cache_is_kept_in_memory_outside_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cachefiles, "ROOT", str(tmp_path / "cache"))
    try:
        assert cachefiles.install(None) == "memory"
        index = remote_store.cache_index()
        assert type(index) is remote_store.CacheIndex
        assert index.budget == cachefiles.MEMORY_BUDGET
        assert str(index.root) == cachefiles.ROOT
    finally:
        cachefiles.uninstall()
        remote_store._reset_for_tests()


def test_a_page_without_its_files_starts_with_the_cache_in_memory(tmp_path, monkeypatch):
    # The worker sets self.plexoraCacheFiles = null when it has no files (the
    # self-check's scratch boot, a second tab, no sync access handles); in
    # Pyodide that null is pyodide.ffi.jsnull, not None (0.0.38 failed to boot).
    import sys
    import types

    jsnull = type("JsNull", (), {"__bool__": lambda self: False})()
    ffi = types.ModuleType("pyodide.ffi")
    ffi.jsnull = jsnull
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi
    js = types.ModuleType("js")
    js.plexoraCacheFiles = jsnull
    js.plexoraCacheQuota = 0
    js.plexoraCacheWhy = "another tab holds the files"
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    monkeypatch.setitem(sys.modules, "js", js)
    monkeypatch.setattr(cachefiles, "ROOT", str(tmp_path / "cache"))
    try:
        assert cachefiles.install() == "memory (another tab holds the files)"
        assert type(remote_store.cache_index()) is remote_store.CacheIndex
    finally:
        cachefiles.uninstall()
        remote_store._reset_for_tests()
