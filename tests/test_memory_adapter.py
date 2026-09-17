"""Reading a table out of memory, against reading the same table off disk.

The claim `plexora/memory.py` rests on is that a snapshot taken with anndata's
own element writer is indistinguishable, to the adapter, from the .h5ad the
caller would otherwise have written. These tests are that claim: everything is
asserted as an EQUALITY against the file-backed result rather than against a
hand-written expectation, so a divergence shows up as a difference between the
two paths rather than as two tests that have drifted together.
"""

import numpy as np
import pandas as pd
import pytest

from plexora import memory as memory_api
from plexora.server.models.adapters.anndata_adapter import AnnDataAdapter
from plexora.server.models.adapters.memory_adapter import (
    MemoryAnnDataAdapter,
    MemoryFrameAdapter,
)
from plexora.server.models.project import ColumnGroups, ColumnRoles, DataSpec

ad = pytest.importorskip("anndata")


def _make_adata(slides=("s1",)):
    """One image's worth of cells unless asked for more.

    Several slides in one table is a real shape and the subset test needs it,
    but it is not the default here: the adapter refuses to load a multi-image
    table without a subset (deliberately -- see `_likely_image_identifier_columns`),
    and that refusal is not what most of these tests are about.
    """
    rows = 60
    rng = np.random.default_rng(7)
    obs = pd.DataFrame(
        {
            "leiden": pd.Categorical(rng.choice(["a", "b", "c"], rows)),
            "slide": rng.choice(list(slides), rows),
            "area": rng.random(rows) * 100,
        },
        index=[f"cell{i}" for i in range(rows)],
    )
    obj = ad.AnnData(
        X=rng.random((rows, 4)).astype("float32"),
        obs=obs,
        var=pd.DataFrame(index=["CD3", "CD4", "CD8", "DAPI"]),
    )
    obj.obsm["spatial"] = (rng.random((rows, 2)) * 500).astype("float32")
    obj.obsm["X_embedding"] = rng.random((rows, 128)).astype("float32")
    obj.layers["scaled"] = obj.X * 2
    return obj


@pytest.fixture
def adata():
    return _make_adata()


def _spec(src, **overrides):
    fields = dict(
        type="anndata", src=str(src),
        coordinates={"source": "obsm", "obsm_key": "spatial"},
        features={"source": "X"},
        roles=ColumnRoles(x="X", y="Y", cell_id="id"),
    )
    fields.update(overrides)
    return DataSpec(**fields)


def _on_disk(adata, tmp_path, **overrides):
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)
    return AnnDataAdapter(_spec(path, **overrides))


def _in_memory(adata, **overrides):
    snapshot = memory_api.snapshot_anndata(
        adata, obsm_keys=["spatial"], log=lambda *a, **k: None,
        layer=overrides.get("features", {}).get("layer"))
    return MemoryAnnDataAdapter(_spec("memory://cells", **overrides), snapshot.group)


# -- the tables are the same table ---------------------------------------


def test_the_loaded_frames_are_identical(adata, tmp_path):
    disk = _on_disk(adata, tmp_path).load_table()
    memory = _in_memory(adata).load_table()

    assert memory.table.equals(disk.table)
    assert memory.feature_columns == disk.feature_columns
    assert (memory.id_column, memory.x_column, memory.y_column) == (
        disk.id_column, disk.x_column, disk.y_column)


def test_the_plans_agree_about_the_table_they_describe(adata, tmp_path):
    disk = _on_disk(adata, tmp_path).plan()
    memory = _in_memory(adata).plan()

    assert memory.rows == disk.rows
    assert memory.feature_columns == disk.feature_columns
    assert memory.obs_columns == disk.obs_columns
    assert memory.table_columns == disk.table_columns


def test_a_snapshot_offers_only_the_matrices_it_holds(adata, tmp_path):
    """The two honest differences, asserted rather than left to be discovered.

    A file's OTHER matrices -- the layers it is not being read through, an
    embedding too wide to be coordinates -- are not copied into memory, so a
    project registered this way does not offer them as a read spec to switch
    to. Changing which matrix is read means calling `plexora.view` again with
    the new one, which in a notebook is the line above.
    """
    disk = _on_disk(adata, tmp_path).plan()
    memory = _in_memory(adata).plan()

    assert disk.layers == ["scaled"] and memory.layers == []
    assert [entry["name"] for entry in memory.obsm] == ["spatial"]
    assert "X_embedding" in [entry["name"] for entry in disk.obsm]


def test_a_subset_selects_the_same_rows(tmp_path):
    adata = _make_adata(slides=("s1", "s2"))
    subset = {"column": "slide", "value": "s1"}
    disk = _on_disk(adata, tmp_path, subset=subset).load_table()
    memory = _in_memory(adata, subset=subset).load_table()

    assert 0 < memory.table.height < adata.n_obs
    assert memory.table.equals(disk.table)


def test_a_layer_is_read_as_the_feature_source(adata, tmp_path):
    features = {"source": "layer", "layer": "scaled"}
    disk = _on_disk(adata, tmp_path, features=features).load_table()
    memory = _in_memory(adata, features=features).load_table()

    assert memory.table.equals(disk.table)
    assert memory.table["CD3"].to_list() != pytest.approx(
        adata.X[:, 0].tolist())


def test_an_obs_column_reads_the_same_values_and_the_same_level_order(adata, tmp_path):
    disk = _on_disk(adata, tmp_path).read_obs_column("leiden")
    memory = _in_memory(adata).read_obs_column("leiden")

    assert list(memory.values) == list(disk.values)
    assert memory.categories == disk.categories


def test_an_obs_column_the_table_does_not_have_is_a_key_error(adata):
    with pytest.raises(KeyError):
        _in_memory(adata).read_obs_column("nope")


# -- what the snapshot chooses to hold -----------------------------------


def test_a_wide_obsm_is_left_out_and_said_so(adata):
    said = []
    snapshot = memory_api.snapshot_anndata(adata, log=said.append)

    assert "spatial" in snapshot.group["obsm"]
    assert "X_embedding" not in snapshot.group["obsm"]
    assert any("X_embedding" in line for line in said)


def test_a_wide_obsm_is_copied_when_it_is_asked_for(adata):
    snapshot = memory_api.snapshot_anndata(
        adata, obsm_keys=["X_embedding"], log=lambda *a, **k: None)

    assert "X_embedding" in snapshot.group["obsm"]


def test_only_the_named_layer_is_copied_not_x_as_well(adata):
    """The point of naming a feature source: a file's other matrices are not
    what this viewer is going to read, and copying them is the duplication the
    whole module exists to avoid."""
    snapshot = memory_api.snapshot_anndata(adata, layer="scaled",
                                           log=lambda *a, **k: None)

    assert "scaled" in snapshot.group["layers"]
    assert "X" not in snapshot.group


def test_a_layer_that_is_not_there_is_refused_with_the_ones_that_are(adata):
    with pytest.raises(memory_api.MemoryDataError) as excinfo:
        memory_api.snapshot_anndata(adata, layer="nope")
    assert "scaled" in str(excinfo.value)


def test_an_obsm_that_is_not_there_is_refused(adata):
    with pytest.raises(memory_api.MemoryDataError):
        memory_api.snapshot_anndata(adata, obsm_keys=["nope"])


# -- a flat table --------------------------------------------------------


def test_a_dataframe_normalizes_exactly_as_the_csv_of_it_would(tmp_path):
    from plexora.server.models.adapters.csv_adapter import CsvAdapter

    frame = pd.DataFrame({
        "CellID": range(20),
        "X_centroid": np.linspace(0, 100, 20),
        "Y_centroid": np.linspace(0, 80, 20),
        "CD3": np.linspace(1, 5, 20),
    })
    path = tmp_path / "cells.csv"
    frame.to_csv(path, index=False)
    spec = DataSpec(
        type="csv", src=str(path),
        roles=ColumnRoles(x="X_centroid", y="Y_centroid", cell_id="CellID"),
        columns=ColumnGroups(markers=("CD3",)),
    )

    from_file = CsvAdapter(spec).load_table()
    snapshot = memory_api.snapshot_frame(frame)
    from_memory = MemoryFrameAdapter(spec, snapshot.frame).load_table()

    assert from_memory.table.equals(from_file.table)
    assert from_memory.feature_columns == from_file.feature_columns


def test_a_polars_frame_is_taken_as_it_is():
    import polars as pl

    frame = pl.DataFrame({"a": [1, 2, 3]})
    assert memory_api.snapshot_frame(frame).frame is frame
