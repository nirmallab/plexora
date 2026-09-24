"""Wide tables: features listed, not loaded, read one at a time on demand.

A whole transcriptome over a million bins is ~90 GB as float32, so past
`WIDE_FEATURE_LIMIT` the loaded frame is ids and coordinates and each feature
is read when something asks. Every assertion compares a lazily read column
with the column the EAGER path produces for the same file, over the three
layouts an AnnData's X can have -- so the lazy path cannot be fast and wrong.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from plexora.server.models import data_model
from plexora.server.models.adapters import AnnDataAdapter
from plexora.server.models.adapters import anndata_adapter
from plexora.server.models.project import ColumnRoles, DataSpec
from plexora.server.providers.local import LocalTableProvider

GENES = [f"g{i}" for i in range(6)]


def _adata(layout, n=50, subset=False):
    rng = np.random.default_rng(4)
    dense = rng.poisson(0.7, size=(n, len(GENES))).astype(np.float32)
    x = {"dense": dense, "csr": sparse.csr_matrix(dense),
         "csc": sparse.csc_matrix(dense)}[layout]
    obs = pd.DataFrame({"sample": (["a", "b"] * n)[:n] if subset else ["a"] * n},
                       index=[f"c{i}" for i in range(n)])
    adata = ad.AnnData(X=x, obs=obs, var=pd.DataFrame(index=GENES))
    adata.obsm["spatial"] = np.column_stack((np.arange(n), np.arange(n) * 2.0))
    return adata, dense


def _spec(path, subset=None, log=False):
    return DataSpec(type="anndata", src=str(path), features={"source": "X"},
                    coordinates={"source": "obsm", "obsm_key": "spatial"},
                    subset=subset or {}, is_transformed=log,
                    roles=ColumnRoles(cell_id="id", x="X", y="Y"))


@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setenv("PLEXORA_WIDE_FEATURE_LIMIT", "3")


@pytest.mark.parametrize("layout", ["dense", "csr", "csc"])
@pytest.mark.parametrize("log", [False, True])
def test_a_lazy_column_is_the_eager_column(tmp_path, monkeypatch, layout, log):
    adata, _ = _adata(layout)
    path = tmp_path / f"{layout}.h5ad"
    adata.write_h5ad(path)
    eager = AnnDataAdapter(_spec(path, log=log)).load_table()
    assert not eager.lazy_features and "g2" in eager.table.columns

    monkeypatch.setenv("PLEXORA_WIDE_FEATURE_LIMIT", "3")
    adapter = AnnDataAdapter(_spec(path, log=log))
    lazy = adapter.load_table()
    assert lazy.lazy_features
    assert lazy.feature_columns == GENES
    assert set(lazy.table.columns) == {"id", "X", "Y", "obs_id"}
    for name in GENES:
        assert np.array_equal(adapter.read_feature_column(name),
                              eager.table[name].to_numpy())
    with pytest.raises(KeyError):
        adapter.read_feature_column("nope")


def test_a_subset_is_applied_to_a_lazy_column(tmp_path, wide):
    adata, dense = _adata("csc", subset=True)
    path = tmp_path / "subset.h5ad"
    adata.write_h5ad(path)
    adapter = AnnDataAdapter(_spec(path, subset={"column": "sample", "value": "b"}))
    table = adapter.load_table()
    assert table.table.height == 25
    assert np.array_equal(adapter.read_feature_column("g1"), dense[1::2, 1])


def test_a_wide_table_is_described_column_by_column(tmp_path, wide):
    adata, dense = _adata("csr")
    path = tmp_path / "describe.h5ad"
    adata.write_h5ad(path)
    provider = LocalTableProvider(_spec(path))
    provider.load()
    described = provider.describe()
    for i, name in enumerate(GENES):
        assert described[name] == data_model._describe_column(dense[:, i])
    assert "X" in described                  # the frame's own columns too


def test_filter_columns_reads_what_the_frame_does_not_hold(tmp_path, wide):
    adata, dense = _adata("csc")
    path = tmp_path / "filter.h5ad"
    adata.write_h5ad(path)
    provider = LocalTableProvider(_spec(path))
    provider.load()
    assert provider.lazy_features
    columns = provider.filter_columns(["g3", "X"])
    assert np.array_equal(columns["g3"], dense[:, 3])
    assert np.array_equal(columns["X"], np.arange(50, dtype=np.float32))
    cells = provider.all_cells(["id", "g0"], float)
    assert np.array_equal(cells.reshape(-1, 2)[:, 1], dense[:, 0])


def test_a_narrow_table_is_untouched(tmp_path):
    adata, _ = _adata("csc")
    path = tmp_path / "narrow.h5ad"
    adata.write_h5ad(path)
    assert anndata_adapter.wide_feature_limit() == 1024
    table = AnnDataAdapter(_spec(path)).load_table()
    assert not table.lazy_features
    assert set(GENES) <= set(table.table.columns)


def test_the_feature_cache_is_bounded(tmp_path, wide, monkeypatch):
    class Table:
        lazy_features = True
        reads = 0

        def read_feature_column(self, name):
            Table.reads += 1
            return np.zeros(1000, dtype=np.float32)

    monkeypatch.setattr(data_model, "_FEATURE_CACHE_BYTES", 2500 * 4)
    data_model._feature_column_cache.clear()
    read = data_model._cached_feature_reader(Table())
    read("a"), read("a"), read("b"), read("c")
    assert Table.reads == 3
    assert len(data_model._feature_column_cache) == 2
    read("a")
    assert Table.reads == 4                  # the oldest was evicted
    data_model._feature_column_cache.clear()
