"""Gates already in `uns['gates']` are read back by the marker each row names.

`/get_gates_from_anndata` exists for gates set up outside Plexora before
import. A table written by another tool need not list every var, nor list
them in var order; its index says which marker each row belongs to. Reading
rows by position instead seeds the sidebar with thresholds on the wrong
markers, without a word.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from plexora import api
from plexora.plugins.gating.server import anndata_gates


def _write(path, gates_index, values):
    obs = pd.DataFrame({"imageid": ["img"] * 4}, index=[f"c{i}" for i in range(4)])
    var = pd.DataFrame(index=["CD3", "CD8", "CD20"])
    adata = ad.AnnData(X=np.zeros((4, 3), dtype=np.float32), obs=obs, var=var)
    adata.uns["gates"] = pd.DataFrame({"img": values}, index=pd.Index(gates_index))
    adata.write_h5ad(path)
    return api.TableSource(kind="anndata", path=str(path),
                           subset={"column": "imageid", "value": "img"})


def test_a_table_listing_only_some_markers_seeds_those_markers(tmp_path):
    source = _write(tmp_path / "subset.h5ad", ["CD20", "CD8"], [7.0, 3.0])

    loaded = anndata_gates.load_gates_from_anndata(source, "ds")

    assert loaded["gates"] == {"CD20": pytest.approx(7.0), "CD8": pytest.approx(3.0)}


def test_a_table_in_another_order_than_var_is_read_by_name(tmp_path):
    source = _write(tmp_path / "reordered.h5ad", ["CD20", "CD3", "CD8"], [7.0, 1.0, 3.0])

    loaded = anndata_gates.load_gates_from_anndata(source, "ds")

    assert loaded["gates"] == {
        "CD3": pytest.approx(1.0),
        "CD8": pytest.approx(3.0),
        "CD20": pytest.approx(7.0),
    }
