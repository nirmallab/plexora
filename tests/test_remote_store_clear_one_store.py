"""Clearing one store's part of the chunk cache while that store is open.

Settings > Web data has a Clear button and a Remove button on every store's
card. `CacheIndex.clear` promises that "a store that is open keeps working --
its next read simply fetches", and that has to hold for one store as much as
for the whole cache: the store object outlives the clear (one per root per
process), so whatever the clear removes from the index has to come back, or
every later read of that store fails.
"""

import numpy as np
import zarr

from plexora.server.models import layer_jobs, remote_sources
from plexora.server.utils import remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _open_image(tmp_path, server):
    write_ngff(tmp_path / "served" / "img.zarr", version="0.5")
    url = server.url("img.zarr")
    store, sub = remote_store.open_store(url)
    return url, store, zarr.open_group(store, path=sub, mode="r")


def test_an_open_store_refetches_after_its_own_part_is_cleared(tmp_path, http_store):
    server = http_store()
    _, store, group = _open_image(tmp_path, server)
    np.asarray(group["0"][0, :16, :16])
    remote_sources.clear(store.store_id)  # Clear on this store's card
    server.clear()
    # A region read before (now cleared) and one never read: both fetch and
    # both are kept again, instead of failing on the index.
    np.asarray(group["0"][0, :16, :16])
    np.asarray(group["0"][2, -16:, -16:])
    assert server.count(method="GET") >= 1
    row = remote_store.cache_index().store_row(store.store_id)
    assert row is not None and row["bytes"] > 0


def test_an_open_store_records_misses_after_it_is_removed(tmp_path, http_store):
    server = http_store()
    _, store, _ = _open_image(tmp_path, server)
    remote_sources.remove_source(store.store_id)  # Remove on the card
    # zarr's optional metadata probes: a key that is not there reads as None.
    assert store.read("nope/zarr.json") is None


def test_keeping_a_cleared_store_offline_pins_it(tmp_path, http_store, monkeypatch):
    server = http_store()
    url, store, group = _open_image(tmp_path, server)
    np.asarray(group["0"][0, :16, :16])
    remote_sources.clear(store.store_id)
    # Only the pin itself is under test, not the fetch job it starts.
    monkeypatch.setattr(layer_jobs, "start_task", lambda *a, **k: {"status": "pending"})
    remote_sources.start_pin(url)
    assert remote_store.cache_index().is_pinned(store.store_id)
