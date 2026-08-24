"""End-to-end check that an AnnData-backed datasource can be registered and
then loaded through the real runtime path (data_model.load_datasource's
data_type='anndata' dispatch), not just the isolated adapter. Mirrors
test_optional_segmentation.py's pattern.
"""

import anndata as ad
import numpy as np
import pandas as pd
import tifffile

from plexora import datasource
from plexora.server.models import data_model
from tests.helpers import use_data_root


def _write_image(path, size=256, channels=2):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


def _write_adata(path, n=10):
    obs = pd.DataFrame({"cell_type": (["typeA", "typeB"] * ((n // 2) + 1))[:n]}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA", "MarkerB"])
    x = np.linspace(0, 5, n * 2, dtype=np.float32).reshape(n, 2)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 200, n, dtype=np.float64), np.linspace(10, 200, n, dtype=np.float64)], axis=1
    )
    adata.write_h5ad(path)


def test_register_and_load_anndata_datasource(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path)

    entry = datasource.register_anndata_datasource(
        name="anndata_sample",
        image=image_path,
        features=h5ad_path,
        data_dir=data_dir,
    )

    assert entry["dataset"]["type"] == "anndata"
    assert entry["dataset"]["roles"]["x"] == "X"
    assert entry["dataset"]["roles"]["y"] == "Y"
    # var_names are markers and obs columns are metadata -- the file already
    # draws the line the CSV classification screen exists to draw, so nothing
    # asks the user to confirm it.
    assert "MarkerA" in entry["dataset"]["columns"]["markers"]
    assert entry["segmentation"] is None

    data_model.load_datasource("anndata_sample", reload=True)

    assert data_model.datasource.height == 10
    assert "MarkerA" in data_model.datasource.columns
    assert data_model.channels is not None


def test_default_id_field_is_positional_id_not_obs_names(tmp_path, monkeypatch):
    """get_all_cells() packs [idField, X, Y] into one array and casts it all
    to uint32 for the fast binary cell-loading path -- idField must default
    to something numeric. adata.obs_names (materialized as the 'obs_id'
    column) is typically a string like a cell barcode, not a small integer,
    so idField must default to the adapter's positional 'id' column instead.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    # Non-numeric obs_names, like real cell-segmentation-derived barcodes --
    # this is what get_all_cells previously crashed on.
    _write_adata(h5ad_path)

    entry = datasource.register_anndata_datasource(
        name="string_id_sample",
        image=image_path,
        features=h5ad_path,
        data_dir=data_dir,
    )

    assert entry["dataset"]["roles"]["cell_id"] == "id"

    data_model.load_datasource("string_id_sample", reload=True)

    result = data_model.get_all_cells("string_id_sample", ["id", "X", "Y"], int)
    assert result.dtype == np.uint32
    assert len(result) == 10 * 3
