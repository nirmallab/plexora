"""Saving regions into a SpatialData store must leave the store's root index alone.

A SpatialData root is zarr v3 with a consolidated index, and real stores (the
ones an older spatialdata wrote) hold their tables as zarr v2 groups under it.
Rebuilding that root index drops every v2 table from it, and every reader that
trusts the index -- spatialdata.read_zarr, napari-spatialdata, squidpy -- then
reports the store as having no tables. adapters.py's module docstring makes
this a rule ("Never re-consolidate a store root"); an annotation export is the
last thing that should break it.
"""

import json

import pytest

from plexora.plugins.roi.server import adapters
from plexora.plugins.roi.tests.test_roi_adapters import annotations, register
from plexora.plugins.gating.tests.test_anndata_gates import _consolidated_store

spatialdata = pytest.importorskip("spatialdata")


def _root_index_keys(store):
    document = json.loads((store / "zarr.json").read_text(encoding="utf-8"))
    return set((document.get("consolidated_metadata") or {}).get("metadata") or {})


def test_saving_regions_keeps_a_v2_table_in_the_store_root_index(tmp_path, monkeypatch):
    """The exemplar layout: a consolidated v3 root over a table written as zarr
    v2. After Save, the table is still listed in the root index and still read
    by spatialdata itself, next to the new shapes element."""
    store = _consolidated_store(tmp_path, "mixed.zarr")
    assert "tables/cells" in _root_index_keys(store), "fixture lost its table index"
    assert list(spatialdata.read_zarr(store).tables) == ["cells"], "fixture unreadable"

    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")

    assert "tables/cells" in _root_index_keys(store)
    reopened = spatialdata.read_zarr(store)
    assert list(reopened.tables) == ["cells"]
    assert "plexora_rois" in reopened.shapes
