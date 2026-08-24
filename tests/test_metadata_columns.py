"""Reading one annotation column, whatever the project was imported from.

This exists because the two halves of the contract disagreed. A project's
`metadata_columns` names every annotation column the source carries -- for
AnnData and SpatialData that is the whole of `.obs` -- while the table the
adapter materializes holds only id/X/Y/the id field/the markers/the celltype
column. So a tool that read `frame()[column]` worked on every CSV and returned
nothing at all for the two formats whose metadata lives somewhere else, which is
precisely the bug a suite written against sample CSVs cannot see.

The alignment cases are the ones worth the setup cost. A column read from `.obs`
that is not subset the same way `load_table()` subset it does not fail -- it
returns the right number of values for the wrong cells, and paints a picture
that looks entirely reasonable.
"""

import anndata as ad
import numpy as np
import pandas as pd
import polars as pl
import pytest
import tifffile

import plexora
from plexora import api
from plexora.server.models import data_model, database_model
from tests.helpers import use_data_root

#: data_model keeps the loaded project in module globals, so a test that loads
#: one leaves the next file served its table. See the note in SKILL.md.
_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def isolate_data_model(monkeypatch):
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_metadata_column_cache", {})


def _redirect(monkeypatch, data_dir):
    config_path = data_dir / "config.json"
    use_data_root(monkeypatch, data_dir)
    isolate_data_model(monkeypatch)


def _write_image(path, size=256, channels=2):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


# --------------------------------------------------------------------------
# CSV -- the frame is the file
# --------------------------------------------------------------------------

@pytest.fixture
def csv_project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"
    _write_image(image_path)
    pl.DataFrame({
        "CellID": np.arange(6, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 6, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 6, dtype=np.float32),
        "MarkerA": np.linspace(0, 5, 6, dtype=np.float32),
        "phenotype": ["Tumor", "CD8 T", "Tumor", "B cell", "CD8 T", "Tumor"],
        "area": np.linspace(50, 300, 6, dtype=np.float64),
    }).write_csv(csv_path)

    _redirect(monkeypatch, data_dir)
    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="csv_meta", image=image_path, features=csv_path,
        x="X_centroid", y="Y_centroid", segmentation=None, data_dir=data_dir,
    )
    return api.dataset("csv_meta")


def test_a_csv_column_comes_straight_off_the_frame(csv_project):
    column = csv_project.table.metadata_values("phenotype")
    assert list(column.values) == ["Tumor", "CD8 T", "Tumor", "B cell", "CD8 T", "Tumor"]
    assert column.name == "phenotype"
    # A CSV header states no level order, so the caller is free to sort.
    assert column.categories is None


def test_a_csv_numeric_column_keeps_its_numbers(csv_project):
    column = csv_project.table.metadata_values("area")
    assert column.values.dtype.kind == "f"
    assert column.values[0] == pytest.approx(50.0)


def test_values_line_up_with_the_frame(csv_project):
    frame = csv_project.table.frame()
    column = csv_project.table.metadata_values("phenotype")
    assert len(column.values) == frame.height


def test_an_unknown_column_raises(csv_project):
    with pytest.raises(KeyError):
        csv_project.table.metadata_values("no_such_column")


def test_the_csv_adapter_has_no_second_place_to_look(csv_project):
    """CsvAdapter.read_obs_column returns None rather than raising: the frame
    was the whole answer, so "not in the frame" already means unknown."""
    from plexora.server.models.adapters import CsvAdapter

    adapter = CsvAdapter(csv_project.project.dataset)
    assert adapter.read_obs_column("phenotype") is None


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------

def test_a_column_is_cached_and_the_cache_is_bounded(csv_project):
    data_model._metadata_column_cache.clear()
    first = csv_project.table.metadata_values("phenotype")
    second = csv_project.table.metadata_values("phenotype")
    assert first is second, "the second read should not re-derive the array"
    assert len(data_model._metadata_column_cache) <= data_model._METADATA_COLUMN_CACHE_MAX


def test_reloading_the_datasource_drops_cached_columns(csv_project):
    csv_project.table.metadata_values("phenotype")
    assert data_model._metadata_column_cache
    data_model.load_datasource("csv_meta", reload=True)
    assert data_model._metadata_column_cache == {}, (
        "a reload means the file changed underneath us; a cached column would "
        "be values from the previous table"
    )


# --------------------------------------------------------------------------
# AnnData -- obs is not in the loaded frame
# --------------------------------------------------------------------------

def _write_adata(path, n=10, subset_column=None, ordered_phenotype=False):
    phenotypes = (["Tumor", "CD8 T"] * ((n // 2) + 1))[:n]
    obs = pd.DataFrame(
        {
            "phenotype": pd.Categorical(
                phenotypes,
                # Deliberately NOT alphabetical: this is the order the file
                # states, and the whole point of carrying it is that sorting
                # would produce a different one.
                categories=["Tumor", "CD8 T"] if ordered_phenotype else None,
                ordered=ordered_phenotype,
            ),
            "confidence": np.linspace(0.1, 0.9, n),
        },
        index=[f"cell_{i}" for i in range(n)],
    )
    if subset_column:
        obs[subset_column] = ["image_a"] * (n // 2) + ["image_b"] * (n - n // 2)
    var = pd.DataFrame(index=["MarkerA", "MarkerB"])
    x = np.linspace(0, 5, n * 2, dtype=np.float32).reshape(n, 2)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 200, n), np.linspace(10, 200, n)], axis=1)
    adata.write_h5ad(path)
    return adata


@pytest.fixture
def anndata_project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path, n=10, ordered_phenotype=True)

    _redirect(monkeypatch, data_dir)
    from plexora import datasource as datasource_module

    datasource_module.register_anndata_datasource(
        name="anndata_meta", image=image_path, features=h5ad_path,
        data_dir=data_dir,
    )
    return api.dataset("anndata_meta")


def test_an_obs_column_is_absent_from_the_loaded_frame(anndata_project):
    """The gap this whole accessor exists to close, stated as a test so a later
    change to what load_table() materializes is noticed rather than silently
    making the accessor redundant (or, worse, half-redundant)."""
    frame = anndata_project.table.frame()
    assert "phenotype" not in frame.columns
    assert "phenotype" in anndata_project.table.metadata_columns


def test_an_obs_column_is_readable_anyway(anndata_project):
    column = anndata_project.table.metadata_values("phenotype")
    assert len(column.values) == anndata_project.table.frame().height
    assert set(column.values) == {"Tumor", "CD8 T"}


def test_a_categorical_keeps_the_order_the_file_declared(anndata_project):
    """Sorting would put "CD8 T" first. The file says otherwise, and a legend
    the user recognises is worth more than an alphabetical one."""
    column = anndata_project.table.metadata_values("phenotype")
    assert column.categories == ("Tumor", "CD8 T")


def test_a_continuous_obs_column_stays_numeric(anndata_project):
    column = anndata_project.table.metadata_values("confidence")
    assert column.values.dtype.kind == "f"
    assert column.categories is None


def test_an_unknown_obs_column_raises(anndata_project):
    with pytest.raises(KeyError):
        anndata_project.table.metadata_values("not_a_column")


def test_reading_obs_does_not_need_the_expression_matrix(anndata_project, monkeypatch):
    """_read_obs() reads the obs element alone. Asserted by making the full read
    explode: if anything reaches for X, this fails.

    The table is warmed first, since loading the project legitimately reads the
    whole file once. What must not happen is paying that again per column --
    which is what `self._read_adata().obs` would have cost, on every switch of
    the variable dropdown.
    """
    from plexora.server.models.adapters import AnnDataAdapter

    anndata_project.table.frame()

    def explode(self):
        raise AssertionError("read_obs_column must not materialize X")

    monkeypatch.setattr(AnnDataAdapter, "_read_adata", explode)
    data_model._metadata_column_cache.clear()
    column = anndata_project.table.metadata_values("confidence")
    assert len(column.values) == 10


# --------------------------------------------------------------------------
# Subset alignment -- the case that fails silently
# --------------------------------------------------------------------------

@pytest.fixture
def subset_project(tmp_path, monkeypatch):
    """An .h5ad covering two images, registered as one of them.

    load_table() keeps half the rows. An obs read that did not would hand back
    twice as many values, or -- worse, once truncated -- the right count taken
    from the wrong cells.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path, n=10, subset_column="sample")

    _redirect(monkeypatch, data_dir)
    from plexora import datasource as datasource_module

    datasource_module.register_anndata_datasource(
        name="subset_meta", image=image_path, features=h5ad_path,
        data_dir=data_dir, subset_by="sample", subset_value="image_b",
    )
    return api.dataset("subset_meta")


def test_a_subset_project_reads_only_its_own_rows(subset_project):
    frame = subset_project.table.frame()
    column = subset_project.table.metadata_values("confidence")
    assert frame.height == 5
    assert len(column.values) == 5


def test_the_subset_values_are_the_right_half(subset_project):
    """Not just the right COUNT. The confidences are a monotone ramp over all
    ten source rows, so the second image's five are the top half -- getting the
    first five instead is exactly the failure that looks fine on screen."""
    column = subset_project.table.metadata_values("confidence")
    assert column.values.min() > 0.5


def test_a_length_mismatch_is_refused_rather_than_drawn(subset_project, monkeypatch):
    """The guard for an adapter whose obs read and whose table stop agreeing.
    Nothing is returned rather than values attached to whichever cells sit at
    those row numbers."""
    from plexora.server.models.adapters import AnnDataAdapter, MetadataColumn

    monkeypatch.setattr(
        AnnDataAdapter, "read_obs_column",
        lambda self, name: MetadataColumn(name=name, values=np.zeros(999)),
    )
    data_model._metadata_column_cache.clear()
    with pytest.raises(ValueError, match="999 values"):
        subset_project.table.metadata_values("confidence")
