"""AnnData files written by anndata before 0.7 (and by 0.7.x itself).

The fixtures are built with h5py directly, field by field, in the layouts
those versions wrote -- the same layouts as anndata's own archived test files
(`adata_v0.5.0.h5ad`, `adata_v0.7.x.h5ad`):

* < 0.7: `obs`/`var`/`obsm` are compound datasets, the index the first field,
  categorical labels in `uns/<column>_categories`, a sparse matrix tagged
  `h5sparse_format`/`h5sparse_shape`.
* 0.7.x: groups, but a categorical column is a dataset of codes whose
  `categories` attr is an HDF5 object reference to `__categories/<column>`.

Read through the adapter's own lazy path and compared with
`anndata.read_h5ad`, which still reads both.
"""

import warnings
from dataclasses import replace

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import pytest

from plexora.server.models.adapters import AnnDataAdapter
from plexora.server.models.adapters.anndata_adapter import (
    LegacyAnnDataError,
    _LazyObs,
    _encoding_of,
    _matrix_shape,
    var_names,
)
from plexora.server.models.adapters.inspection import inspect_anndata
from plexora.server.models.project import ColumnRoles, DataSpec

N = 12
MARKERS = ["CD3", "CD20", "PanCK"]
PHASES = ["G1", "S", "G2M"]
TYPES = ["T cell", "B cell", "Tumour"]


def _x():
    x = np.zeros((N, len(MARKERS)), dtype=np.float32)
    x[np.arange(N), np.arange(N) % len(MARKERS)] = np.arange(1, N + 1)
    return x


def _spatial():
    return np.stack([np.arange(N) * 10.0, np.arange(N) * 5.0 + 1], axis=1)


def _cell_type_codes():
    return (np.arange(N) % 3).astype(np.int8)


def _write_v05(path, sparse=True):
    """anndata 0.5/0.6's layout."""
    import scipy.sparse as sp

    with h5py.File(path, "w") as f:
        obs = np.zeros(N, dtype=[("index", "S8"), ("cell_type", "i1"),
                                 ("area", "<f4"), ("phase", "i1"),
                                 ("note", "S6")])
        obs["index"] = [f"c{i}".encode() for i in range(N)]
        obs["cell_type"] = _cell_type_codes()
        obs["area"] = np.arange(N, dtype=np.float32) * 1.5
        obs["phase"] = (np.arange(N) % 3)[::-1].astype(np.int8)
        obs["note"] = [f"n{i}".encode() for i in range(N)]
        f.create_dataset("obs", data=obs)

        var = np.zeros(len(MARKERS), dtype=[("index", "S8"), ("mean", "<f4")])
        var["index"] = [name.encode() for name in MARKERS]
        f.create_dataset("var", data=var)

        obsm = np.zeros(N, dtype=[("spatial", "<f8", (2,)), ("X_pca", "<f4", (4,))])
        obsm["spatial"] = _spatial()
        f.create_dataset("obsm", data=obsm)

        uns = f.create_group("uns")
        uns.create_dataset("cell_type_categories",
                           data=np.array([t.encode() for t in TYPES]))
        uns.create_dataset("phase_categories",
                           data=np.array([p.encode() for p in PHASES]))

        if sparse:
            matrix = sp.csr_matrix(_x())
            group = f.create_group("X")
            group.attrs["h5sparse_format"] = "csr"
            group.attrs["h5sparse_shape"] = np.array(matrix.shape)
            group.create_dataset("data", data=matrix.data)
            group.create_dataset("indices", data=matrix.indices)
            group.create_dataset("indptr", data=matrix.indptr)
        else:
            f.create_dataset("X", data=_x())


def _write_v07(path):
    """anndata 0.7.x's layout: referenced categories."""
    str_dtype = h5py.special_dtype(vlen=str)
    with h5py.File(path, "w") as f:
        f.create_dataset("X", data=_x())
        obs = f.create_group("obs")
        obs.attrs["_index"] = "_index"
        obs.attrs["encoding-type"] = "dataframe"
        obs.attrs["encoding-version"] = "0.1.0"
        obs.attrs["column-order"] = ["cell_type", "grade", "area"]
        obs.create_dataset("_index", data=np.array([f"c{i}" for i in range(N)],
                                                    dtype=object), dtype=str_dtype)
        cats = obs.create_group("__categories")
        labels = cats.create_dataset("cell_type", data=np.array(TYPES, dtype=object),
                                     dtype=str_dtype)
        labels.attrs["ordered"] = False
        grades = cats.create_dataset("grade", data=np.array(["low", "mid", "high"],
                                                            dtype=object),
                                     dtype=str_dtype)
        grades.attrs["ordered"] = True
        codes = obs.create_dataset("cell_type", data=_cell_type_codes())
        codes.attrs["categories"] = labels.ref
        grade = obs.create_dataset("grade", data=(np.arange(N) % 3)[::-1].astype(np.int8))
        grade.attrs["categories"] = grades.ref
        obs.create_dataset("area", data=np.arange(N, dtype=np.float64))

        var = f.create_group("var")
        var.attrs["_index"] = "_index"
        var.attrs["encoding-type"] = "dataframe"
        var.attrs["encoding-version"] = "0.1.0"
        var.attrs.create("column-order", [], dtype=str_dtype)
        var.create_dataset("_index", data=np.array(MARKERS, dtype=object),
                           dtype=str_dtype)

        f.create_group("obsm").create_dataset("spatial", data=_spatial())
        f.create_group("uns")


def _reference(path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ad.read_h5ad(path)


def _same(mine, theirs):
    def text(series):
        return [("NA" if pd.isna(v) else str(v)) for v in series.astype(object)]
    return text(mine) == text(theirs)


def _spec(path, celltype=None):
    return DataSpec(
        type="anndata", src=str(path), coordinates={"source": "obsm",
                                                    "obsm_key": "spatial"},
        features={"source": "X"}, subset={},
        roles=ColumnRoles(cell_id="id", x="X", y="Y", celltype=celltype))


@pytest.fixture(params=["v05_sparse", "v05_dense", "v07"])
def legacy_file(request, tmp_path):
    path = tmp_path / f"{request.param}.h5ad"
    if request.param == "v07":
        _write_v07(path)
    else:
        _write_v05(path, sparse=request.param == "v05_sparse")
    return path


def test_every_obs_column_matches_anndata(legacy_file):
    """Each column, whole and subset, reads as `anndata.read_h5ad` reads it:
    categoricals as their labels (not the codes), in the file's level order,
    ordered when the file says so."""
    reference = _reference(legacy_file).obs
    rows = np.array([1, 2, 7, N - 1])
    with h5py.File(legacy_file, "r") as handle:
        obs = _LazyObs(handle)
        assert obs.columns == list(reference.columns)
        assert obs.n_rows == N
        assert list(obs.index_take(None)) == list(reference.index)
        assert list(obs.index_take(rows)) == list(reference.index[rows])
        for column in reference.columns:
            assert _same(obs[column], reference[column]), column
            assert _same(obs.take(column, rows), reference[column].iloc[rows]), column
            if hasattr(reference[column], "cat"):
                assert list(obs[column].cat.categories) == \
                    list(reference[column].cat.categories)
                assert obs[column].cat.ordered == reference[column].cat.ordered
        assert var_names(handle) == list(_reference(legacy_file).var_names)


def test_categorical_shows_labels_not_codes(legacy_file):
    """The 0.7 bug in one line: `cell_type` used to come back as 0, 1, 2."""
    with h5py.File(legacy_file, "r") as handle:
        values = list(_LazyObs(handle)["cell_type"].astype(object))
    assert values[:3] == TYPES


def test_load_table_matches_anndata(legacy_file):
    reference = _reference(legacy_file)
    adapter = AnnDataAdapter(_spec(legacy_file, celltype="cell_type"))
    table = adapter.load_table()
    assert table.feature_columns == list(reference.var_names)
    expected = reference.X.toarray() if hasattr(reference.X, "toarray") else reference.X
    np.testing.assert_allclose(table.table.select(MARKERS).to_numpy(), expected)
    np.testing.assert_allclose(table.table["X"].to_numpy(), reference.obsm["spatial"][:, 0])
    np.testing.assert_allclose(table.table["Y"].to_numpy(), reference.obsm["spatial"][:, 1])
    assert list(table.table["cell_type"]) == list(reference.obs["cell_type"].astype(str))
    assert list(table.table["obs_id"]) == list(reference.obs_names)

    column = adapter.read_obs_column("cell_type")
    assert list(column.values) == list(reference.obs["cell_type"].astype(object))
    assert column.categories == tuple(TYPES)

    sample = adapter.sample_features(5)
    assert sorted(sample) == sorted(MARKERS)


def test_subset_by_a_legacy_categorical(legacy_file):
    """A subset value is a label; matching it against codes found nothing."""
    spec = _spec(legacy_file)
    spec = replace(spec, subset={"column": "cell_type", "value": "B cell"})
    table = AnnDataAdapter(spec).load_table()
    assert table.table.height == len([i for i in range(N) if i % 3 == 1])
    np.testing.assert_allclose(table.table["X"].to_numpy(),
                               _spatial()[1::3, 0])


def test_inspection_reads_legacy_layouts(legacy_file):
    reference = _reference(legacy_file)
    document = inspect_anndata(str(legacy_file))
    assert document["obs_count"] == N
    assert document["var_names"] == list(reference.var_names)
    assert "spatial" in document["obsm_keys"]
    spatial = next(e for e in document["obsm"] if e["name"] == "spatial")
    assert spatial["shape"] == [N, 2]
    by_name = {c["name"]: c for c in document["obs_columns"]}
    assert by_name["cell_type"]["values"] == sorted(TYPES)
    assert by_name["cell_type"]["is_subset_candidate"]


def test_legacy_sparse_matrix_is_recognised(tmp_path):
    path = tmp_path / "v05.h5ad"
    _write_v05(path, sparse=True)
    with h5py.File(path, "r") as handle:
        assert _encoding_of(handle["X"]) == "csr_matrix"
        assert _matrix_shape(handle["X"]) == (N, len(MARKERS))


def test_unknown_layout_says_to_resave(tmp_path):
    """A layout nothing here understands is a sentence, not an AttributeError."""
    path = tmp_path / "odd.h5ad"
    with h5py.File(path, "w") as f:
        f.create_dataset("obs", data=np.arange(N))  # a frame that is not one
        f.create_dataset("X", data=_x())
    with pytest.raises(LegacyAnnDataError) as raised:
        AnnDataAdapter(_spec(path)).plan()
    message = str(raised.value)
    assert "anndata" in message and "Re-save" in message
    assert "write_h5ad" in message
    with pytest.raises(ValueError):
        inspect_anndata(str(path))


def test_register_legacy_file_end_to_end(tmp_path, monkeypatch):
    """The import a user runs: register, then load, on a pre-0.7 file."""
    import tifffile

    from plexora import datasource
    from plexora.server.models import data_model
    from tests.helpers import use_data_root

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    image = tmp_path / "image.tif"
    tifffile.imwrite(image, np.zeros((2, 128, 128), dtype=np.uint8))
    cells = tmp_path / "cells.h5ad"
    _write_v05(cells, sparse=True)

    entry = datasource.register_anndata_datasource(
        name="legacy_sample", image=image, features=cells, data_dir=data_dir,
        coordinate_source="obsm", obsm_key="spatial")
    assert set(MARKERS) <= set(entry["dataset"]["columns"]["markers"])
    data_model.load_datasource("legacy_sample", reload=True)
    assert data_model.datasource.height == N
