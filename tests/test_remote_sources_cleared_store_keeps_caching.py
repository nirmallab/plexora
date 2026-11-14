"""A web-data store that is cleared, removed or released while it is open.

There is one `ChunkCacheStore` per store root per process, and an open zarr
array keeps holding it, so it outlives Settings > Web data's Clear and Remove
buttons and the eviction that deleting a project does
(`remote_sources.release`). `CacheIndex.clear` says that "a store that is open
keeps working -- its next read simply fetches", and `remote_sources.usage`
says the store "reappears with its first byte". These tests hold both to
that: after its part of the cache is emptied, the same store object goes on
reading, caching and being listed, and making it available offline pins it.

The wrapped store is an in-memory zarr store standing in for a bucket, so
nothing here touches the network.
"""

import asyncio
from types import SimpleNamespace

from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from plexora.server.models import remote_sources
from plexora.server.utils import remote_store
from tests.remote_fixtures import cache_root  # noqa: F401

ROOT = "s3://audit-bucket/cleared.zarr"


def _open(values):
    """The process's one cached store for ROOT, over an in-memory bucket."""
    inner = MemoryStore()
    prototype = default_buffer_prototype()

    async def fill():
        for key, data in values.items():
            await inner.set(key, prototype.buffer.from_bytes(data))

    asyncio.run(fill())
    store = remote_store.ChunkCacheStore(inner, ROOT)
    with remote_store._stores_lock:
        remote_store._stores[ROOT] = store
    opened, _ = remote_store.open_store(ROOT)
    assert opened is store
    return store


VALUES = {f"0/c/0/{i}": bytes([i]) * 64 for i in range(4)}


def test_an_open_store_goes_on_reading_and_caching_after_clear(cache_root):
    store = _open(VALUES)
    assert store.read("0/c/0/0") == VALUES["0/c/0/0"]

    remote_sources.clear(store.store_id)  # Clear on this store's card

    # Every later read fetches and is kept again, rather than failing on the
    # index with "FOREIGN KEY constraint failed".
    assert store.read("0/c/0/1") == VALUES["0/c/0/1"]
    assert store.read("0/c/0/2") == VALUES["0/c/0/2"]
    row = remote_store.cache_index().store_row(store.store_id)
    assert row is not None and row["bytes"] == 128


def test_an_open_store_goes_on_reading_after_remove(cache_root):
    store = _open(VALUES)
    assert store.read("0/c/0/0") == VALUES["0/c/0/0"]

    remote_sources.remove_source(store.store_id)  # Remove on the card

    assert store.read("0/c/0/1") == VALUES["0/c/0/1"]
    assert store.read("0/c/0/3") == VALUES["0/c/0/3"]
    # A key that is not there reads as missing, as zarr's probes expect.
    assert store.read("absent/zarr.json") is None


def test_an_open_store_goes_on_reading_after_its_project_is_deleted(cache_root):
    store = _open(VALUES)
    assert store.read("0/c/0/0") == VALUES["0/c/0/0"]
    project = SimpleNamespace(
        image=SimpleNamespace(src=ROOT),
        segmentation=SimpleNamespace(source=None, derived=None),
        dataset=None, spatial_layers=[])

    # Deleting the project evicts its store; re-importing the same address in
    # the same server process reuses the cached store object.
    assert remote_sources.release(project, []) == [ROOT]

    assert store.read("0/c/0/1") == VALUES["0/c/0/1"]
    assert store.read("0/c/0/2") == VALUES["0/c/0/2"]


def test_pinning_a_cleared_store_records_the_pin(cache_root):
    store = _open(VALUES)
    assert store.read("0/c/0/0") == VALUES["0/c/0/0"]
    remote_sources.clear(store.store_id)

    # What start_pin does before it starts the fetch job.
    index = store.index
    index.pin(store.store_id, True)

    assert remote_store.cache_index().is_pinned(store.store_id)
