"""The AnnData multi-image guard and the answers that should satisfy it.

`AnnDataAdapter._plan_rows` refuses a table whose obs has a column NAMED like
an image identifier ("core", "fov", "roi", "region", ...) holding more than one
value, unless a subset is chosen. That refusal is a question with three answers
in the project record, and the guard must honour all of them:

- a subset ("load image B"), which it does;
- an image-id column, which it does (and checks that column instead);
- "this table covers one image" (`DataSpec.single_image`), which is what
  DataSpec's own docstring says replaces the column-name heuristic.

And the import must never refuse a file it did not first ask about: when
inspection lets a file through as ordinary (`deferred_spec` is None), `plan()`
has to be able to read it. Finally, Plexora's own "Map to cells" write-back must
not turn a project that opened fine into one that refuses to open.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile

from plexora import api
from plexora.datasource import deferred_spec
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
from tests.helpers import anndata_spec, use_data_root


def _write(path, obs_columns, n):
    adata = ad.AnnData(
        X=np.arange(n * 3, dtype=np.float32).reshape(n, 3),
        obs=pd.DataFrame(obs_columns, index=[f"cell_{i}" for i in range(n)]),
        var=pd.DataFrame(index=[f"marker_{i}" for i in range(3)]),
    )
    adata.obsm["spatial"] = np.column_stack(
        [np.arange(n, dtype=float) * 10 + 5, np.arange(n, dtype=float) * 10 + 5])
    adata.write_h5ad(path)
    return path


def test_a_single_image_answer_loads_a_tma_whose_obs_names_its_cores(tmp_path):
    """One TMA slide exported as one .h5ad, with a 'core' column naming each
    core. The user answered "this table covers one image", so the whole table
    loads: that answer is exactly what the record keeps instead of the
    column-name heuristic."""
    path = _write(tmp_path / "tma.h5ad",
                  {"core": ["A1", "A1", "B2", "B2"]}, 4)
    spec = anndata_spec(path, single_image=True)
    assert spec.single_image is True and spec.roles.image_id is None

    planned = AnnDataAdapter(spec).plan()

    assert planned.rows == 4


def test_an_integer_image_column_is_asked_about_rather_than_refused(tmp_path):
    """An .h5ad whose image column holds integers (imageid 1, 1, 2, 2, 3, 3).
    Either the import defers and asks which image (`unresolved=('subset',)`),
    or the table reads. What it must not do is pass inspection as an ordinary
    one-image file and then have plan() refuse it outright."""
    path = _write(tmp_path / "numeric_ids.h5ad",
                  {"imageid": np.array([1, 1, 2, 2, 3, 3], dtype=np.int64)}, 6)

    deferred = deferred_spec(path, "anndata")
    if deferred is None:
        # Inspection called the file ordinary, so the importer goes straight
        # on to plan() it (datasource.py, _register_anndata) -- which must
        # then succeed rather than refuse the import.
        spec = DataSpec(type="anndata", src=str(path),
                        coordinates={"source": "obsm", "obsm_key": "spatial"},
                        features={"source": "X"},
                        roles=ColumnRoles(cell_id="id", x="X", y="Y"))
        AnnDataAdapter(spec).plan()
    else:
        assert deferred.unresolved == ("subset",)


# -- Plexora's own write-back ---------------------------------------------

_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def test_mapping_rois_to_cells_does_not_stop_a_single_image_project_opening(
        tmp_path, monkeypatch):
    """"Map to cells" with the destination name 'roi' writes `roi_category`
    and `roi_name` into obs. A single-image project that opened before the
    write must still open after it: the columns annotate cells, they do not
    say which image the cells are in."""
    pytest.importorskip("shapely")
    from plexora.plugins.roi.server import adapters, mapping, schema
    from plexora.plugins.roi.server.operations import apply_operations

    use_data_root(monkeypatch, tmp_path / "data")
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)

    # Two cells inside the ROI drawn below, two outside it.
    n = 4
    table = tmp_path / "one_image.h5ad"
    adata = ad.AnnData(
        X=np.zeros((n, 3), dtype=np.float32),
        obs=pd.DataFrame({"cellid": [f"c{i}" for i in range(n)]},
                         index=[f"c{i}" for i in range(n)]),
        var=pd.DataFrame(index=[f"marker_{i}" for i in range(3)]),
    )
    adata.obsm["spatial"] = np.array(
        [[10, 10], [20, 20], [200, 200], [210, 210]], dtype=float)
    adata.write_h5ad(table)

    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    spec = DataSpec(
        type="anndata", src=str(table),
        roles=ColumnRoles(cell_id="cellid", x="X", y="Y"),
        columns=ColumnGroups(markers=("marker_0",), metadata=("id",)),
        features={"source": "X"}, obs_id_field="cellid",
        coordinates={"source": "obsm", "obsm_key": "spatial"},
        single_image=True)
    Project(
        name="roi_writeback_guard",
        image=ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=({"name": "DNA", "fullname": "DNA", "src": "/generated/x/"},
                      {"name": "CD3", "fullname": "CD3", "src": "/generated/y/"}),
            width=256, height=256, max_level=1, tile_width=1024, tile_height=1024,
            num_channels=2),
        segmentation=SegmentationSpec(),
        dataset=spec,
    ).save()

    # It opens before the write.
    assert AnnDataAdapter(spec).plan().rows == n

    dataset = api.project_data("roi_writeback_guard")
    state = schema.default_state(256, 256)
    state = apply_operations(state, [{
        "op": "category.create",
        "category": {"id": "c-1", "label": "Tumor", "color": "#e04c4c", "sort_order": 1}}])
    state = apply_operations(state, [{
        "op": "roi.create", "image": "default",
        "feature": {"id": "r-1", "category_id": "c-1", "name": "Tumor 1",
                    "geometry": {"type": "Polygon", "coordinates": [
                        [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}}}])
    entry = state["images"][schema.DEFAULT_IMAGE]
    frame = dataset.table.frame()
    labels, names = mapping.assign(
        entry["features"], state["categories"],
        frame[dataset.schema.x].to_list(), frame[dataset.schema.y].to_list())
    result = adapters.write_cell_columns(dataset, labels, names, prefix="roi")
    assert result["columns"] == ["roi_category", "roi_name"]

    # And it still opens after Plexora's own write.
    assert AnnDataAdapter(spec).plan().rows == n
