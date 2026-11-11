"""The page's OME-Zarr reader on stores zarr-python 2 era writers left.

A `.zarray` with no `dimension_separator` over chunks stored nested ("0/0/0",
NestedDirectoryStore before zarr 2.8) read as the fill value in both readers:
Python's (tests/test_legacy_zarr_v2.py) and the page's
(client/src/js/node/zarrReader.js), which defaulted to "." the same way. Here
every tile the page reads is compared with the tile route's answer, as
tests/test_browser_zarr_node.py does for modern stores.
"""

from __future__ import annotations

import hashlib

import pytest

import plexora
from plexora.browser import node_spec
from tests.ngff_fixtures import write_legacy_v2
from tests.test_browser_zarr_node import NODE, ZSTDDEC, _probe, _python_tile, _register

pytestmark = pytest.mark.skipif(not NODE or not ZSTDDEC.is_dir(),
                                reason="needs node and plexora/client/node_modules (npm ci)")

CASES = {
    "nested_u2": {"dtype": "<u2", "nested": True},
    "nested_be_u1": {"dtype": ">u1", "nested": True},
    "nested_first_missing": {"dtype": "<u2", "nested": True, "missing": [(0, 0, 0)],
                             "fill_value": 0},
    "flat_no_key_u1": {"dtype": ">u1", "nested": False},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_tile_is_the_tile_routes(tmp_path, case):
    store, _ = write_legacy_v2(tmp_path / f"{case}.zarr", shape=(2, 300, 260),
                               chunks=(1, 64, 64), levels=3, **CASES[case])
    entry = _register(tmp_path, case, store, tile=128)
    spec = node_spec.describe(case, local=True)
    assert spec["served"] is True, spec
    found = _probe(store, spec, tmp_path)
    assert "refused" not in found, found
    # The look at the chunk spelling travels in the seed with the metadata.
    assert found["seededAsks"] == 0
    client = plexora.app.test_client()
    keys = [channel["src"].rstrip("/").split("/")[-1] for channel in entry["imageData"]]
    compared = 0
    for tile, got in found["tiles"].items():
        channel, level, at = tile.split("/")
        tx, ty = at.split("_")
        want, size = _python_tile(client, case, keys[int(channel)], level, tx, ty)
        assert (got["width"], got["height"]) == size, tile
        assert got["hash"] == hashlib.sha256(want).hexdigest(), f"{case} {tile} differs"
        compared += 1
    assert compared == len(found["tiles"]) > 0
