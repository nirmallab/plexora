"""Plexora's own cell columns stay Plexora's own after its next write into obs.

ROI "Map to cells" with the destination name 'roi' writes `roi_category` and
`roi_name` into obs, and marks them as Plexora's own so the multi-image guard
(`AnnDataAdapter._plan_rows`) does not read them as image identifiers. Writing
QC flags to source rewrites obs too (plugins/qc/server/source_write.py unlinks
the group and writes it again): the mark has to survive that, or a project with
no image answer that opened a moment ago refuses to open.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile

from plexora import api
from plexora.server.models import data_model
from plexora.server.models.adapters import AnnDataAdapter
from plexora.server.models.project import (
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    ImageSpec,
    Project,
    SegmentationSpec,
)
from tests.helpers import use_data_root

_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def test_roi_columns_stay_plexoras_own_after_a_qc_source_write(tmp_path, monkeypatch):
    pytest.importorskip("shapely")
    import polars as pl

    from plexora.plugins.qc.server import source_write
    from plexora.plugins.roi.server import adapters, mapping, schema
    from plexora.plugins.roi.server.operations import apply_operations
    from plexora.server.models.adapters.anndata_adapter import PLEXORA_CELL_COLUMNS_ATTR

    use_data_root(monkeypatch, tmp_path / "data")
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)

    # Two cells in each of two ROIs, two outside both. No image column, and
    # no "one image" answer: the guard runs on every open.
    n = 6
    table = tmp_path / "cells.h5ad"
    adata = ad.AnnData(
        X=np.zeros((n, 3), dtype=np.float32),
        obs=pd.DataFrame({"CellID": np.arange(1, n + 1, dtype=np.int64)},
                         index=[f"c{i}" for i in range(n)]),
        var=pd.DataFrame(index=[f"marker_{i}" for i in range(3)]),
    )
    adata.obsm["spatial"] = np.array(
        [[10, 10], [20, 20], [150, 150], [160, 160], [230, 30], [240, 40]], dtype=float)
    adata.write_h5ad(table)

    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    spec = DataSpec(
        type="anndata", src=str(table),
        roles=ColumnRoles(cell_id="CellID", x="X", y="Y"),
        columns=ColumnGroups(markers=("marker_0",), metadata=("id",)),
        features={"source": "X"}, obs_id_field="CellID",
        coordinates={"source": "obsm", "obsm_key": "spatial"})
    assert not spec.single_image and spec.roles.image_id is None
    Project(
        name="roi_mark_qc",
        image=ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=({"name": "DNA", "fullname": "DNA", "src": "/generated/x/"},
                      {"name": "CD3", "fullname": "CD3", "src": "/generated/y/"}),
            width=256, height=256, max_level=1, tile_width=1024, tile_height=1024,
            num_channels=2),
        segmentation=SegmentationSpec(),
        dataset=spec,
    ).save()
    assert AnnDataAdapter(spec).plan().rows == n

    dataset = api.project_data("roi_mark_qc")
    state = schema.default_state(256, 256)
    state = apply_operations(state, [
        {"op": "category.create",
         "category": {"id": "c-1", "label": "Tumor", "color": "#e04c4c", "sort_order": 1}},
        {"op": "category.create",
         "category": {"id": "c-2", "label": "Stroma", "color": "#4c7ce0", "sort_order": 2}},
    ])
    state = apply_operations(state, [
        {"op": "roi.create", "image": "default",
         "feature": {"id": "r-1", "category_id": "c-1", "name": "Tumor 1",
                     "geometry": {"type": "Polygon", "coordinates": [
                         [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}}},
        {"op": "roi.create", "image": "default",
         "feature": {"id": "r-2", "category_id": "c-2", "name": "Stroma 1",
                     "geometry": {"type": "Polygon", "coordinates": [
                         [[120, 120], [200, 120], [200, 200], [120, 200], [120, 120]]]}}},
    ])
    entry = state["images"][schema.DEFAULT_IMAGE]
    frame = dataset.table.frame()
    labels, names = mapping.assign(
        entry["features"], state["categories"],
        frame[dataset.schema.x].to_list(), frame[dataset.schema.y].to_list())
    result = adapters.write_cell_columns(dataset, labels, names, prefix="roi")
    assert result["columns"] == ["roi_category", "roi_name"]
    assert AnnDataAdapter(spec).plan().rows == n

    # Segmentation QC's flags written to source: obs rewritten by the QC plugin.
    seg = pl.DataFrame({"cell_id": list(range(1, n + 1)),
                        "status": ["ok", "under_segmented", "ok", "ok", "ok", "ok"],
                        "under_score": [0.1, 0.9, 0.1, 0.1, 0.1, 0.1],
                        "over_score": [0.0] * n,
                        "partner_id": [0, 3, 0, 0, 0, 0]})
    written = source_write.apply(dataset, {
        "n_rows": n, "image_id": "",
        "seg": {"frame": source_write.encode(seg), "uns": {"fingerprint": "test"}}})
    assert "plexora_seg_qc_status" in written["columns"]
    back = ad.read_h5ad(table)
    assert {"roi_category", "roi_name", "plexora_seg_qc_status"} <= set(back.obs.columns)

    # Still opens after Plexora's second write...
    assert AnnDataAdapter(spec).plan().rows == n

    # ...because of the mark: without it the ROI columns read as image ids.
    import h5py

    with h5py.File(table, "r+") as handle:
        del handle.attrs[PLEXORA_CELL_COLUMNS_ATTR]
    with pytest.raises(ValueError, match="roi_"):
        AnnDataAdapter(spec).plan()
