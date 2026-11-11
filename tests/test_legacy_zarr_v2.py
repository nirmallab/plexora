"""Zarr v2 stores as zarr-python 2 era writers left them, opened by zarr 3.

Two failures from a measurement over real public samples:

* old bioformats2raw wrote 8-bit images with a big-endian dtype (">u1"), and
  zarr 3 refuses the array outright ("No Zarr data type found ...");
* ome-zarr-py / NestedDirectoryStore before zarr 2.8 stored chunks nested
  ("0/0/0") under a `.zarray` naming no `dimension_separator`; zarr 3 looks
  for "0.0.0", finds nothing, and the image is the fill value -- black, no
  error.

Both are fixed at the store boundary (`remote_store.LegacyV2`), so they are
tested on a local folder and through the chunk cache over HTTP, and the user's
files must come out unchanged.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from plexora.server.utils import ome_zarr, remote_store
from tests.ngff_fixtures import write_legacy_v2
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _plane(pyramid, level=0):
    return np.asarray(pyramid[str(level)][:, :, :])


def _with_fill(data, chunks, missing, fill):
    expected = data.copy()
    for coords in missing:
        window = tuple(slice(i * c, (i + 1) * c) for i, c in zip(coords, chunks))
        expected[window] = fill
    return expected


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


# -- metadata zarr 3 refuses --------------------------------------------------


@pytest.mark.parametrize("dtype", [">u1", "<u1", ">i1"])
def test_a_one_byte_dtype_with_a_byte_order_opens(tmp_path, dtype):
    store, data = write_legacy_v2(tmp_path / "old.zarr", dtype=dtype, levels=2)
    before = _snapshot(store)

    pyramid = ome_zarr.open_image(store)

    assert len(pyramid) == 2
    plane = _plane(pyramid)
    assert plane.dtype.itemsize == 1 and plane.dtype.kind == np.dtype(dtype).kind
    np.testing.assert_array_equal(plane, data)
    assert _snapshot(store) == before  # read, never rewritten


def test_a_bioformats2raw_series_with_u1_resolves_and_opens(tmp_path):
    root = tmp_path / "b2r.zarr"
    root.mkdir()
    (root / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    (root / ".zattrs").write_text(json.dumps({"bioformats2raw.layout": 3}))
    _, data = write_legacy_v2(root / "0", dtype=">u1", levels=2)

    resolved = ome_zarr.resolve_image_path(root)
    np.testing.assert_array_equal(_plane(ome_zarr.open_image(resolved)), data)


@pytest.mark.parametrize("extra, check", [
    ({"order": None}, lambda doc: doc["order"] == "C"),
    ({"fill_value": None}, lambda doc: doc["fill_value"] is None),
    ({"dimension_separator": None}, lambda doc: "dimension_separator" not in doc),
])
def test_missing_or_null_keys_are_filled_in(extra, check):
    doc = {"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "|u1",
           "compressor": None, "fill_value": 0, "order": "C", "filters": None}
    for key in extra:
        doc.pop(key, None)
    if "dimension_separator" in extra:
        doc["dimension_separator"] = None
    assert remote_store.fix_v2_array_metadata(doc)
    assert check(doc)


@pytest.mark.parametrize("dtype, fill, expected", [
    ("|u1", -1, 255), ("<u2", 0.5, 0), ("<u2", 7.0, 7), ("<i2", float("nan"), None),
])
def test_an_integer_fill_the_dtype_cannot_hold_is_cast(dtype, fill, expected):
    doc = {"dtype": dtype, "fill_value": fill, "order": "C"}
    remote_store.fix_v2_array_metadata(doc)
    assert doc["fill_value"] == expected


@pytest.mark.parametrize("dtype, canonical", [
    (">u1", "|u1"), ("<i1", "|i1"), (">b1", "|b1"), ("u1", "|u1"),
    (">u2", ">u2"), ("<f4", "<f4"), ("|u1", "|u1"),
])
def test_dtype_spellings(dtype, canonical):
    doc = {"dtype": dtype, "fill_value": 0, "order": "C"}
    remote_store.fix_v2_array_metadata(doc)
    assert doc["dtype"] == canonical


def test_a_store_needing_none_of_it_is_left_byte_for_byte():
    legacy = remote_store.LegacyV2()
    raw = json.dumps({"zarr_format": 2, "shape": [4], "chunks": [2], "dtype": "<u2",
                      "compressor": None, "fill_value": 0, "order": "C",
                      "filters": None, "dimension_separator": "."}).encode()
    assert legacy.note("0/.zarray", raw) is raw
    assert legacy.ndim == {}


@pytest.mark.parametrize("extra", [{"order": None}, {"fill_value": None}])
def test_an_array_without_order_or_fill_value_opens(tmp_path, extra):
    store, data = write_legacy_v2(tmp_path / "old.zarr", dtype="<u2", zarray_extra=extra)
    np.testing.assert_array_equal(_plane(ome_zarr.open_image(store)), data)


# -- nested chunks under a `.zarray` that does not say so ---------------------


@pytest.mark.parametrize("consolidated", [False, True])
def test_nested_chunks_without_a_separator_key_are_read(tmp_path, consolidated):
    store, data = write_legacy_v2(tmp_path / "nested.zarr", dtype="<u2", nested=True,
                                  levels=2, consolidated=consolidated)
    before = _snapshot(store)

    pyramid = ome_zarr.open_image(store)

    np.testing.assert_array_equal(_plane(pyramid), data)
    np.testing.assert_array_equal(_plane(pyramid, 1), data[..., ::2, ::2])
    assert _snapshot(store) == before


def test_nested_chunks_with_the_first_one_missing_are_read(tmp_path):
    missing = [(0, 0, 0)]
    store, data = write_legacy_v2(tmp_path / "sparse.zarr", dtype="<u2", nested=True,
                                  missing=missing, fill_value=7)
    plane = _plane(ome_zarr.open_image(store))
    np.testing.assert_array_equal(plane, _with_fill(data, (1, 32, 32), missing, 7))


def test_flat_chunks_without_a_separator_key_still_read(tmp_path):
    missing = [(0, 0, 0)]
    store, data = write_legacy_v2(tmp_path / "flat.zarr", dtype="<u2", nested=False,
                                  missing=missing, fill_value=3)
    plane = _plane(ome_zarr.open_image(store))
    np.testing.assert_array_equal(plane, _with_fill(data, (1, 32, 32), missing, 3))


def test_an_array_with_no_chunks_written_opens_as_its_fill(tmp_path):
    store, data = write_legacy_v2(tmp_path / "empty.zarr", dtype="<u2", nested=True,
                                  missing="all", fill_value=5)
    plane = _plane(ome_zarr.open_image(store))
    assert plane.shape == data.shape and (plane == 5).all()


def test_the_separator_is_decided_once_per_array(tmp_path, monkeypatch):
    store, _ = write_legacy_v2(tmp_path / "nested.zarr", dtype="<u2", nested=True)
    calls = []
    original = remote_store.LegacyV2.decide

    def counting(self, parent, *args, **kwargs):
        calls.append(parent)
        return original(self, parent, *args, **kwargs)

    monkeypatch.setattr(remote_store.LegacyV2, "decide", counting)
    pyramid = ome_zarr.open_image(store)
    _plane(pyramid)
    _plane(pyramid)
    assert calls == ["0"]


# -- the same through the chunk cache, over HTTP ------------------------------


def _remote(http_store, tmp_path, **kwargs):
    served = tmp_path / "served"
    store, data = write_legacy_v2(served / "old.zarr", **kwargs)
    server = http_store()
    return server, server.url("old.zarr"), data


def test_a_remote_u1_store_opens(tmp_path, http_store):
    server, url, data = _remote(http_store, tmp_path, dtype=">u1", levels=2)
    np.testing.assert_array_equal(_plane(ome_zarr.open_image(url)), data)


def test_a_remote_nested_store_is_read_and_probed_once(tmp_path, http_store):
    server, url, data = _remote(http_store, tmp_path, dtype="<u2", nested=True, levels=2)
    pyramid = ome_zarr.open_image(url)
    np.testing.assert_array_equal(_plane(pyramid), data)
    np.testing.assert_array_equal(_plane(pyramid, 1), data[..., ::2, ::2])
    # Never a dotted chunk asked for beyond the one probe per array.
    dotted = [p for m, p, s in server.requests if p.rsplit("/", 1)[-1].count(".") == 2]
    assert sorted(dotted) == ["/old.zarr/0/0.0.0", "/old.zarr/1/0.0.0"]

    server.clear()
    ome_zarr.prefetch_level(pyramid["0"])
    np.testing.assert_array_equal(_plane(ome_zarr.open_image(url)), data)
    assert not [r for r in server.requests if r[2] == 404]


def test_a_remote_nested_store_without_its_first_chunk(tmp_path, http_store):
    missing = [(0, 0, 0)]
    served = tmp_path / "served"
    write_legacy_v2(served / "old.zarr", dtype="<u2", nested=True,
                    missing=missing, fill_value=9)
    _, data = write_legacy_v2(tmp_path / "copy.zarr", dtype="<u2")
    server = http_store("listing")
    plane = _plane(ome_zarr.open_image(server.url("old.zarr")))
    np.testing.assert_array_equal(plane, _with_fill(data, (1, 32, 32), missing, 9))
