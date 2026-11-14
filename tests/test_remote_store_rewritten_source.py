"""A file or zarr store rewritten at the same web address is read as its new self.

A pipeline re-run writes the same path (a registered slide, a label mask, an
OME-Zarr re-uploaded to the same key). The chunk cache is kept across restarts,
and the fingerprint of the address changes when the object does -- so the
bytes read after that change must be the new object's, never the old cached
blocks of the same key, and never a mix of the two versions.
"""

import numpy as np
import zarr

from plexora.server.utils import remote_store
from tests.remote_fixtures import cache_root, http_store, touch_later  # noqa: F401

_OLD_SIZE = 192 * 1024
_NEW_SIZE = 256 * 1024


def test_a_file_replaced_at_its_address_reads_as_the_new_file(tmp_path, http_store,
                                                              cache_root):
    served = tmp_path / "served"
    path = served / "slide.ome.tif"
    path.write_bytes(b"\x01" * _OLD_SIZE)
    server = http_store()
    url = server.url("slide.ome.tif")

    before = remote_store.fingerprint(url)
    assert remote_store.open_file(url).pread(0, 16) == b"\x01" * 16
    remote_store.cache_index().flush()

    # The source is rewritten in place: longer, different bytes, newer mtime.
    path.write_bytes(b"\x02" * _NEW_SIZE)
    touch_later(path)
    remote_store._reset_for_tests(cache_root)  # a new process, same disk cache

    after = remote_store.fingerprint(url)
    assert after is not None and after.key != before.key
    reopened = remote_store.open_file(url)
    assert reopened.size == _NEW_SIZE
    head = reopened.pread(0, 16)
    tail = reopened.pread(_OLD_SIZE, 16)
    assert tail == b"\x02" * 16
    assert head == b"\x02" * 16, (
        "the head of the rewritten file came from the old cached blocks "
        f"({head[:4]!r}) while bytes past the old end came from the new file "
        f"({tail[:4]!r})")


def test_a_file_replaced_in_the_same_process_reads_as_the_new_file(tmp_path, http_store):
    served = tmp_path / "served"
    path = served / "mask.tif"
    path.write_bytes(b"\x01" * _OLD_SIZE)
    server = http_store()
    url = server.url("mask.tif")

    before = remote_store.fingerprint(url)
    assert remote_store.open_file(url).pread(0, 16) == b"\x01" * 16

    path.write_bytes(b"\x02" * _OLD_SIZE)
    touch_later(path)
    remote_store._probes.clear()  # the probe's ten seconds have passed

    after = remote_store.fingerprint(url)
    assert after is not None and after.key != before.key
    assert remote_store.open_file(url).pread(0, 16) == b"\x02" * 16


def test_a_zarr_array_rewritten_at_its_address_reads_its_new_chunks(tmp_path, http_store,
                                                                     cache_root):
    served = tmp_path / "served"
    array_path = served / "img.zarr"
    first = zarr.open_array(str(array_path), mode="w", shape=(64, 64), chunks=(32, 32),
                            dtype="uint16", zarr_format=3)
    first[:] = 1
    server = http_store()
    url = server.url("img.zarr")

    before = remote_store.fingerprint(url)
    store, sub = remote_store.open_store(url)
    old = np.asarray(zarr.open_array(store, path=sub, mode="r")[:])
    assert (old == 1).all()
    remote_store.cache_index().flush()

    # The analyst re-uploads the array to the same key with new values.
    second = zarr.open_array(str(array_path), mode="w", shape=(64, 64), chunks=(32, 32),
                             dtype="uint16", zarr_format=3)
    second[:] = 2
    second.attrs["run"] = 2  # so its metadata document, and its ETag, change too
    touch_later(array_path / "zarr.json")
    remote_store._reset_for_tests(cache_root)  # a new process, same disk cache

    after = remote_store.fingerprint(url)
    assert after is not None and after.key != before.key
    store, sub = remote_store.open_store(url)
    new = np.asarray(zarr.open_array(store, path=sub, mode="r")[:])
    assert (new == 2).all(), (
        f"values {sorted(set(np.unique(new).tolist()))} read after the array was "
        "rewritten: the old cached chunks were served")
