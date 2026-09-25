"""The on-disk chunk cache under a remote zarr store.

Measured against IDR, zarr alone refetches a whole chunk for every tile -- 123 s
for forty tiles, against 0.12 s cached -- so this cache is what makes a remote
image usable. Every assertion here counts requests at a real HTTP server
(tests/remote_fixtures.py), because "it returned the right bytes" is true of
the uncached path too.
"""

import sqlite3
import threading

import numpy as np
import pytest
import zarr

from plexora.server.providers.base import RemoteUnreachable
from plexora.server.utils import remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _served_image(tmp_path, version="0.5", **kwargs):
    return write_ngff(tmp_path / "served" / "img.zarr", version=version, **kwargs)


def _open(url):
    store, sub = remote_store.open_store(url)
    return store, zarr.open_group(store, path=sub, mode="r")


def _store(url):
    """The cached store alone, for a 'store' that is only a folder of blobs."""
    return remote_store.open_store(url)[0]


def test_a_chunk_is_fetched_once(tmp_path, http_store):
    _served_image(tmp_path)
    server = http_store()
    store, group = _open(server.url("img.zarr"))
    first = np.asarray(group["0"][0, :32, :32])
    fetched = server.count(method="GET")
    second = np.asarray(group["0"][0, :32, :32])
    assert np.array_equal(first, second)
    assert server.count(method="GET") == fetched
    assert store.stats["hits"] >= 1


def test_concurrent_readers_share_one_fetch(tmp_path, http_store):
    _served_image(tmp_path)
    server = http_store()
    store, group = _open(server.url("img.zarr"))
    server.clear()
    server.delay = 0.2
    key = "0/c/0/0/0"
    results = []

    def read():
        results.append(store.read(key))

    threads = [threading.Thread(target=read) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({bytes(r) for r in results}) == 1
    assert server.count(suffix=key, method="GET") == 1


def test_the_cache_survives_a_restart(tmp_path, http_store, cache_root):
    _served_image(tmp_path)
    server = http_store()
    url = server.url("img.zarr")
    _, group = _open(url)
    np.asarray(group["0"][:])
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)  # a new process, same disk
    server.clear()
    _, group = _open(url)
    np.asarray(group["0"][:])
    assert server.count(method="GET") == 0


def test_a_missing_key_is_remembered(tmp_path, http_store, monkeypatch):
    _served_image(tmp_path)
    server = http_store()
    store, _ = _open(server.url("img.zarr"))
    assert store.read("nope/zarr.json") is None
    assert store.read("nope/zarr.json") is None
    assert server.count(suffix="nope/zarr.json", method="GET") == 1
    # ...but only for a while: a store being written catches up.
    now = remote_store.time.time()
    monkeypatch.setattr(remote_store.time, "time",
                        lambda: now + remote_store.NEGATIVE_META_TTL_S + 1)
    assert store.read("nope/zarr.json") is None
    assert server.count(suffix="nope/zarr.json", method="GET") == 2


def test_forbidden_reads_as_missing(tmp_path, http_store):
    """A bucket without list permission answers 403 for a key that is not
    there; zarr's own store raises on that, and the open dies on the first
    optional metadata name it probes."""
    _served_image(tmp_path, version="0.4")
    server = http_store("forbidden")
    store, group = _open(server.url("img.zarr"))
    assert group["0"].shape[0] == 3
    assert store.read("absent") is None
    assert store.last_status("absent") == "forbidden"


def test_least_recently_read_is_evicted_first(tmp_path, http_store, monkeypatch):
    served = tmp_path / "served"
    for name in "abc":
        (served / "blobs.zarr").mkdir(parents=True, exist_ok=True)
        (served / "blobs.zarr" / name).write_bytes(bytes(400))
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", "1000")
    server = http_store()
    store = _store(server.url("blobs.zarr"))
    store.read("a")
    store.read("b")
    store.read("a")  # a is now the more recently read
    remote_store.cache_index().flush()
    store.read("c")  # needs room: b goes, not a
    server.clear()
    store.read("a")
    store.read("b")
    assert server.count(suffix="/a") == 0
    assert server.count(suffix="/b") == 1


def test_a_pinned_store_is_never_evicted(tmp_path, http_store, monkeypatch):
    served = tmp_path / "served"
    for store_name in ("kept.zarr", "other.zarr"):
        (served / store_name).mkdir(parents=True)
        for name in "ab":
            (served / store_name / name).write_bytes(bytes(400))
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", "1000")
    server = http_store()
    kept = _store(server.url("kept.zarr"))
    other = _store(server.url("other.zarr"))
    kept.read("a")
    remote_store.cache_index().pin(kept.store_id, True)
    other.read("a")
    other.read("b")  # over budget: only other.zarr's bytes can go
    server.clear()
    kept.read("a")
    assert server.count(suffix="kept.zarr/a") == 0


def test_a_value_bigger_than_the_budget_is_served_not_kept(tmp_path, http_store, monkeypatch):
    (tmp_path / "served" / "big.zarr").mkdir(parents=True)
    (tmp_path / "served" / "big.zarr" / "blob").write_bytes(bytes(5000))
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", "1000")
    server = http_store()
    store = _store(server.url("big.zarr"))
    assert len(store.read("blob")) == 5000
    assert remote_store.cache_index().usage()["used_bytes"] == 0


def test_byte_ranges_of_a_sharded_array_are_cached(tmp_path, http_store, cache_root):
    root = zarr.open_group(str(tmp_path / "served" / "sharded.zarr"), mode="w")
    array = root.create_array("a", shape=(64, 64), chunks=(16, 16), shards=(64, 64),
                              dtype="uint16")
    array[:] = np.arange(64 * 64, dtype="uint16").reshape(64, 64)
    server = http_store()
    url = server.url("sharded.zarr")
    _, group = _open(url)
    assert int(group["a"][20, 20]) == 20 * 64 + 20
    assert server.count(method="GET") > 0
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)
    server.clear()
    _, group = _open(url)
    assert int(group["a"][20, 20]) == 20 * 64 + 20
    assert server.count(method="GET") == 0


def test_clearing_while_open_refetches(tmp_path, http_store):
    _served_image(tmp_path)
    server = http_store()
    store, group = _open(server.url("img.zarr"))
    np.asarray(group["0"][0, :16, :16])
    remote_store.cache_index().clear()
    assert remote_store.cache_index().usage()["used_bytes"] == 0
    server.clear()
    np.asarray(group["0"][0, :16, :16])
    assert server.count(method="GET") >= 1


def test_offline_misses_raise_and_hits_still_serve(tmp_path, http_store):
    _served_image(tmp_path)
    server = http_store()
    store, group = _open(server.url("img.zarr"))
    cached = np.asarray(group["0"][0, :16, :16])
    with server.outage():
        assert np.array_equal(np.asarray(group["0"][0, :16, :16]), cached)
        with pytest.raises(RemoteUnreachable):
            np.asarray(group["0"][2, -16:, -16:])
    store.offline = True
    with pytest.raises(RemoteUnreachable):
        store.read("never/fetched")


def test_reconcile_adopts_orphans_and_drops_ghosts(tmp_path, http_store, cache_root):
    (tmp_path / "served" / "r.zarr").mkdir(parents=True)
    for name in "ab":
        (tmp_path / "served" / "r.zarr" / name).write_bytes(bytes(100))
    server = http_store()
    store = _store(server.url("r.zarr"))
    store.read("a")
    store.read("b")
    index = remote_store.cache_index()
    index.value_path(store.store_id, "a").unlink()           # a ghost row
    orphan = index.value_path(store.store_id, "orphan")
    orphan.write_bytes(bytes(50))                             # an unrecorded file
    remote_store._reset_for_tests(cache_root)
    index = remote_store.cache_index()
    index._reconciled.clear()
    index.reconcile()
    assert index.lookup(store.store_id, "a") is None
    assert index.lookup(store.store_id, "orphan").size == 50
    assert index.usage()["used_bytes"] == 150


def test_a_corrupt_index_is_rebuilt_from_the_files(tmp_path, http_store, cache_root):
    (tmp_path / "served" / "c.zarr").mkdir(parents=True)
    (tmp_path / "served" / "c.zarr" / "a").write_bytes(bytes(100))
    server = http_store()
    url = server.url("c.zarr")
    store = _store(url)
    store.read("a")
    remote_store._reset_for_tests(cache_root)
    db = cache_root / "index.sqlite"
    for suffix in ("-wal", "-shm"):
        (cache_root / f"index.sqlite{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"this is not a database" * 100)
    index = remote_store.cache_index()
    index._reconciled.clear()
    index.reconcile()
    assert list(cache_root.glob("index.sqlite.corrupt-*"))
    # The store's directory said which URL it was, so the row is not orphaned.
    rows = index.usage()["stores"]
    assert rows and rows[0]["url"] == url and rows[0]["bytes"] == 100
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
