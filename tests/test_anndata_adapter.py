import anndata as ad
import numpy as np
import pandas as pd
import pytest

from plexora.server.models.adapters import AnnDataAdapter, get_adapter
from plexora.server.models.adapters.anndata_adapter import (
    DEFAULT_ID_COLUMN,
    is_likely_image_identifier_name,
)
from plexora.server.models.adapters.inspection import inspect_anndata


def _make_single_image_adata(n=20):
    rng = np.random.default_rng(0)
    obs = pd.DataFrame(
        {"cell_type": (["typeA", "typeB"] * ((n // 2) + 1))[:n]},
        index=[f"cell_{i}" for i in range(n)],
    )
    var = pd.DataFrame(index=[f"protein_{i}" for i in range(4)])
    x = rng.random((n, 4)).astype(np.float32)
    protein_layer = rng.random((n, 4)).astype(np.float32) * 10
    spatial = np.stack([np.arange(n, dtype=np.float64) * 5, np.arange(n, dtype=np.float64) * 3], axis=1)
    adata = ad.AnnData(X=x, obs=obs, var=var, layers={"protein": protein_layer})
    adata.obsm["spatial"] = spatial
    return adata


def _make_multi_image_adata(per_image=15):
    """3 images at numerically distinct coordinate ranges so a subset bug
    (wrong rows, or coordinates from the wrong image) is detectable."""
    rng = np.random.default_rng(1)
    image_offsets = {"image_01": 0.0, "image_02": 10_000.0, "image_03": 20_000.0}
    obs_rows = []
    spatial_rows = []
    obs_names = []
    for image_id, offset in image_offsets.items():
        for i in range(per_image):
            obs_names.append(f"{image_id}_cell_{i}")
            obs_rows.append(image_id)
            spatial_rows.append([offset + i, offset + i * 2])
    n = len(obs_names)
    obs = pd.DataFrame({"image_id": obs_rows}, index=obs_names)
    var = pd.DataFrame(index=[f"protein_{i}" for i in range(3)])
    x = rng.random((n, 3)).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.asarray(spatial_rows, dtype=np.float64)
    return adata


def _feature_config(data_source_overrides=None, celltype=None):
    config = {
        "src": None,
        "dataSource": {"format": "anndata", "path": None, **(data_source_overrides or {})},
    }
    if celltype:
        config["celltype"] = celltype
    return config


def test_get_adapter_returns_anndata_adapter():
    assert get_adapter("anndata") is AnnDataAdapter


def test_obsm_spatial_autodetected(tmp_path):
    path = tmp_path / "single.h5ad"
    adata = _make_single_image_adata()
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    normalized = AnnDataAdapter(config).load_table()

    assert normalized.table.height == adata.n_obs
    assert normalized.x_column == "X"
    assert normalized.y_column == "Y"
    np.testing.assert_allclose(normalized.table["X"].to_numpy(), adata.obsm["spatial"][:, 0])
    np.testing.assert_allclose(normalized.table["Y"].to_numpy(), adata.obsm["spatial"][:, 1])


def test_obs_coordinate_fallback(tmp_path):
    path = tmp_path / "obs_coords.h5ad"
    adata = _make_single_image_adata(n=10)
    del adata.obsm["spatial"]
    adata.obs["x_coordinate"] = np.arange(10, dtype=np.float64)
    adata.obs["y_coordinate"] = np.arange(10, dtype=np.float64) * 2
    adata.write_h5ad(path)

    config = _feature_config({
        "path": str(path),
        "coordinates": {"source": "obs", "x_column": "x_coordinate", "y_column": "y_coordinate"},
        "features": {"source": "X"},
    })
    normalized = AnnDataAdapter(config).load_table()

    np.testing.assert_allclose(normalized.table["X"].to_numpy(), adata.obs["x_coordinate"].to_numpy())
    np.testing.assert_allclose(normalized.table["Y"].to_numpy(), adata.obs["y_coordinate"].to_numpy())


def test_coordinate_source_required_without_obsm_spatial(tmp_path):
    path = tmp_path / "no_spatial.h5ad"
    adata = _make_single_image_adata(n=6)
    del adata.obsm["spatial"]
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    with pytest.raises(ValueError, match="obsm"):
        AnnDataAdapter(config).load_table()


def test_layer_feature_source_uses_layer_not_x(tmp_path):
    path = tmp_path / "layer.h5ad"
    adata = _make_single_image_adata(n=8)
    adata.write_h5ad(path)

    config = _feature_config({
        "path": str(path),
        "coordinates": {"source": "obsm", "obsm_key": "spatial"},
        "features": {"source": "layer", "layer": "protein"},
    })
    normalized = AnnDataAdapter(config).load_table()

    assert set(normalized.feature_columns) == {f"protein_{i}" for i in range(4)}
    np.testing.assert_allclose(
        normalized.table["protein_0"].to_numpy(), adata.layers["protein"][:, 0]
    )
    assert not np.allclose(normalized.table["protein_0"].to_numpy(), adata.X[:, 0])


def test_missing_layer_raises_clear_error(tmp_path):
    path = tmp_path / "missing_layer.h5ad"
    _make_single_image_adata(n=4).write_h5ad(path)

    config = _feature_config({
        "path": str(path),
        "coordinates": {"source": "obsm", "obsm_key": "spatial"},
        "features": {"source": "layer", "layer": "does_not_exist"},
    })
    with pytest.raises(ValueError, match="does_not_exist"):
        AnnDataAdapter(config).load_table()


def test_obs_id_preserved_and_default_id_column(tmp_path):
    path = tmp_path / "ids.h5ad"
    adata = _make_single_image_adata(n=5)
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    normalized = AnnDataAdapter(config).load_table()

    assert normalized.source_obs_ids == list(adata.obs_names)
    assert normalized.table[DEFAULT_ID_COLUMN].to_list() == list(adata.obs_names)


def test_multi_image_subset_correctness(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata(per_image=15)
    adata.write_h5ad(path)

    config = _feature_config({
        "path": str(path),
        "coordinates": {},
        "features": {"source": "X"},
        "subset": {"column": "image_id", "value": "image_02"},
    })
    normalized = AnnDataAdapter(config).load_table()

    assert normalized.table.height == 15
    # image_02's coordinates are offset by 10_000 -- both far above image_01's
    # range and far below image_03's, so this also catches "subset applied
    # after coordinates resolved" bugs.
    x_values = normalized.table["X"].to_numpy()
    assert (x_values >= 10_000).all()
    assert (x_values < 20_000).all()

    expected_ids = list(adata.obs_names[adata.obs["image_id"] == "image_02"])
    assert normalized.source_obs_ids == expected_ids
    assert not any(obs_id.startswith("image_01") for obs_id in normalized.source_obs_ids)
    assert not any(obs_id.startswith("image_03") for obs_id in normalized.source_obs_ids)


def test_ambiguous_multi_image_without_subset_raises(tmp_path):
    path = tmp_path / "multi_ambiguous.h5ad"
    _make_multi_image_adata(per_image=5).write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    with pytest.raises(ValueError, match="image_id"):
        AnnDataAdapter(config).load_table()


def test_single_image_without_identifier_column_does_not_require_subset(tmp_path):
    path = tmp_path / "single_no_subset_needed.h5ad"
    _make_single_image_adata(n=6).write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    normalized = AnnDataAdapter(config).load_table()

    assert normalized.table.height == 6


def test_unknown_subset_value_raises(tmp_path):
    path = tmp_path / "multi_bad_value.h5ad"
    _make_multi_image_adata(per_image=4).write_h5ad(path)

    config = _feature_config({
        "path": str(path),
        "coordinates": {},
        "features": {"source": "X"},
        "subset": {"column": "image_id", "value": "image_99"},
    })
    with pytest.raises(ValueError, match="image_99"):
        AnnDataAdapter(config).load_table()


def test_inspect_anndata_flags_image_id_as_subset_candidate(tmp_path):
    path = tmp_path / "inspect_multi.h5ad"
    _make_multi_image_adata(per_image=5).write_h5ad(path)

    result = inspect_anndata(path)

    assert result["data_type"] == "anndata"
    assert result["obs_count"] == 15
    assert "spatial" in result["obsm_keys"]
    image_id_entry = next(c for c in result["obs_columns"] if c["name"] == "image_id")
    assert image_id_entry["is_subset_candidate"] is True
    assert set(image_id_entry["values"]) == {"image_01", "image_02", "image_03"}


def test_inspect_anndata_lists_layers_and_var_names(tmp_path):
    path = tmp_path / "inspect_single.h5ad"
    adata = _make_single_image_adata(n=6)
    adata.write_h5ad(path)

    result = inspect_anndata(path)

    assert result["layers"] == ["protein"]
    assert set(result["var_names"]) == {f"protein_{i}" for i in range(4)}
    assert result["n_var"] == 4


# Regression tests below were added after real exemplar data (an orion.h5ad
# with 18 markers including a duplicate, an obs column literally named "id",
# and an "imageid" obs column with no underscore) surfaced bugs the synthetic
# fixtures above didn't cover.

def test_duplicate_var_names_are_deduplicated_not_rejected(tmp_path):
    """Real cyclic-immunofluorescence panels commonly re-stain/re-image the
    same marker (e.g. PTPRC/CD45 twice) -- confirmed against real exemplar
    data. This must auto-resolve, not hard-fail."""
    path = tmp_path / "dup_markers.h5ad"
    n = 6
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["PTPRC", "CD68", "PTPRC"])  # duplicate marker name
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 3)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(0, 10, n), np.linspace(0, 10, n)], axis=1
    )
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    normalized = AnnDataAdapter(config).load_table()

    assert normalized.table.height == n
    assert normalized.feature_columns == ["PTPRC", "CD68", "PTPRC_1"]
    assert set(normalized.feature_columns).issubset(normalized.table.columns)


def test_obs_id_field_colliding_with_reserved_column_raises(tmp_path):
    """Real exemplar data has an obs column literally named 'id'. Without a
    guard, picking it as obs_id_field would silently overwrite the adapter's
    own positional 'id' column (everything downstream keys off) instead of
    erroring -- must raise instead of corrupting data."""
    path = tmp_path / "id_column.h5ad"
    n = 5
    obs = pd.DataFrame({"id": [f"real-id-{i}" for i in range(n)]}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA"])
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 1)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack([np.linspace(0, 10, n), np.linspace(0, 10, n)], axis=1)
    adata.write_h5ad(path)

    config = _feature_config({
        "path": str(path), "coordinates": {}, "features": {"source": "X"},
        "obs_id_field": "id",
    })
    with pytest.raises(ValueError, match="reserved"):
        AnnDataAdapter(config).load_table()


def test_celltype_column_colliding_with_reserved_column_raises(tmp_path):
    path = tmp_path / "celltype_collision.h5ad"
    n = 5
    # a real obs column literally named "X" -- distinct from the resolved
    # coordinate column the adapter also names "X"
    obs = pd.DataFrame({"X": ["typeA", "typeB"] * 2 + ["typeA"]}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA"])
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 1)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack([np.linspace(0, 10, n), np.linspace(0, 10, n)], axis=1)
    adata.write_h5ad(path)

    config = _feature_config(
        {"path": str(path), "coordinates": {}, "features": {"source": "X"}},
        celltype="X",  # collides with the resolved X coordinate column name
    )
    with pytest.raises(ValueError, match="reserved"):
        AnnDataAdapter(config).load_table()


def test_is_likely_image_identifier_name_matches_separator_variants():
    for name in ("imageid", "image_id", "Image ID", "IMAGE-ID", "image id"):
        assert is_likely_image_identifier_name(name), name


def test_is_likely_image_identifier_name_rejects_ordinary_annotations():
    for name in ("phenotype", "leiden", "simplified_leiden", "cell_type", "condition"):
        assert not is_likely_image_identifier_name(name), name


def test_inspect_anndata_does_not_flag_ordinary_categorical_columns_as_likely_identifier(tmp_path):
    """Mirrors the real exemplar data shape: a single-image file (imageid has
    only 1 distinct value) with ordinary phenotype/cluster annotations should
    not be flagged as a likely multi-image identifier, even though those
    columns are still valid, optional subset candidates."""
    path = tmp_path / "single_image_real_shape.h5ad"
    n = 10
    obs = pd.DataFrame(
        {
            "imageid": ["only_image"] * n,
            "phenotype": (["typeA", "typeB"] * (n // 2)),
            "leiden": (["0", "1", "2"] * ((n // 3) + 1))[:n],
        },
        index=[f"cell_{i}" for i in range(n)],
    )
    var = pd.DataFrame(index=["MarkerA"])
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 1)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack([np.linspace(0, 10, n), np.linspace(0, 10, n)], axis=1)
    adata.write_h5ad(path)

    result = inspect_anndata(path)

    by_name = {c["name"]: c for c in result["obs_columns"]}
    assert by_name["imageid"]["is_subset_candidate"] is False  # nunique == 1
    assert by_name["phenotype"]["is_subset_candidate"] is True
    assert by_name["phenotype"]["likely_multi_image_identifier"] is False
    assert by_name["leiden"]["likely_multi_image_identifier"] is False

    # Loading without a subset must still succeed -- imageid has only 1 value,
    # so it's not actually ambiguous despite matching the identifier-name list.
    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    normalized = AnnDataAdapter(config).load_table()
    assert normalized.table.height == n


def test_multi_image_with_no_separator_identifier_name_is_detected(tmp_path):
    """Same as test_ambiguous_multi_image_without_subset_raises but using the
    no-separator 'imageid' naming convention found in real exemplar data,
    instead of 'image_id'."""
    path = tmp_path / "multi_imageid.h5ad"
    n = 10
    obs = pd.DataFrame({"imageid": (["img_a"] * 5) + (["img_b"] * 5)}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA"])
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 1)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack([np.linspace(0, 10, n), np.linspace(0, 10, n)], axis=1)
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {}, "features": {"source": "X"}})
    with pytest.raises(ValueError, match="imageid"):
        AnnDataAdapter(config).load_table()
