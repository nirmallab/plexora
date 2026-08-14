import anndata as ad
import numpy as np
import pandas as pd
import polars as pl
import pytest

from plexora.server.modules.gating import anndata_gates


def _make_multi_image_adata(images=("A", "B", "C"), per_image=5, n_vars=4):
    rng = np.random.default_rng(0)
    obs_rows = []
    obs_names = []
    for image_id in images:
        for i in range(per_image):
            obs_names.append(f"{image_id}_cell_{i}")
            obs_rows.append(image_id)
    n = len(obs_names)
    obs = pd.DataFrame({"imageid": obs_rows}, index=obs_names)
    var = pd.DataFrame(index=[f"marker_{i}" for i in range(n_vars)])
    x = rng.random((n, n_vars)).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    return adata


def _feature_config(path, image_id):
    return {
        "dataSource": {
            "path": str(path),
            "subset": {"column": "imageid", "value": image_id},
        }
    }


def test_first_save_creates_all_image_columns(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)
    x_before = adata.X.copy()
    obs_before = adata.obs.copy()

    result = anndata_gates.save_gates_to_anndata(
        _feature_config(path, "B"),
        datasource_name="ds_b",
        active_gates={"marker_0": 1.5, "marker_2": 3.0},
    )

    assert result["image_id"] == "B"
    assert result["n_active_gates"] == 2
    assert result["n_image_columns"] == 3

    reopened = ad.read_h5ad(path)
    gates = reopened.uns["gates"]
    assert isinstance(gates, pd.DataFrame)
    assert list(gates.index) == list(reopened.var_names)
    assert set(gates.columns) == {"A", "B", "C"}

    assert gates["A"].isna().all()
    assert gates["C"].isna().all()
    assert gates.loc["marker_0", "B"] == pytest.approx(1.5)
    assert gates.loc["marker_2", "B"] == pytest.approx(3.0)
    assert pd.isna(gates.loc["marker_1", "B"])
    assert pd.isna(gates.loc["marker_3", "B"])

    np.testing.assert_array_equal(reopened.X, x_before)
    pd.testing.assert_frame_equal(reopened.obs, obs_before)


def test_second_image_save_leaves_siblings_untouched(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)

    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "B"), "ds_b", {"marker_0": 1.5}
    )
    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"marker_1": 9.0}
    )

    gates = ad.read_h5ad(path).uns["gates"]
    assert gates.loc["marker_0", "B"] == pytest.approx(1.5)
    assert gates.loc["marker_1", "A"] == pytest.approx(9.0)
    assert pd.isna(gates.loc["marker_1", "B"])
    assert gates["C"].isna().all()


def test_resave_full_overwrites_only_that_column(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)

    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "B"), "ds_b", {"marker_0": 1.5, "marker_1": 2.0}
    )
    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"marker_2": 7.0}
    )
    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "B"), "ds_b", {"marker_3": 4.0}
    )

    gates = ad.read_h5ad(path).uns["gates"]
    assert pd.isna(gates.loc["marker_0", "B"])
    assert pd.isna(gates.loc["marker_1", "B"])
    assert gates.loc["marker_3", "B"] == pytest.approx(4.0)
    assert gates.loc["marker_2", "A"] == pytest.approx(7.0)


def test_channel_not_in_var_names_is_skipped(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)

    result = anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"not_a_marker": 5.0, "marker_0": 1.0}
    )
    assert result["n_active_gates"] == 1

    gates = ad.read_h5ad(path).uns["gates"]
    assert "not_a_marker" not in gates.index
    assert gates.loc["marker_0", "A"] == pytest.approx(1.0)


def test_duplicate_var_names_use_first_occurrence(tmp_path):
    """Real multiplexed-imaging panels re-stain the same marker across
    cycles (confirmed against real exemplar data), producing duplicate
    var_names. AnnDataAdapter._deduplicate_names() assigns the plain name
    to the *first* occurrence and suffixes the rest -- a gate literally
    named e.g. "PTPRC" must land on that first occurrence, not the last."""
    path = tmp_path / "dup.h5ad"
    rng = np.random.default_rng(0)
    n = 6
    obs = pd.DataFrame({"imageid": ["A"] * n}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["marker_0", "PTPRC", "marker_2", "PTPRC"])
    adata = ad.AnnData(X=rng.random((n, 4)).astype(np.float32), obs=obs, var=var)
    adata.write_h5ad(path)

    result = anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"PTPRC": 2.5}
    )
    assert result["n_active_gates"] == 1

    reopened = ad.read_h5ad(path)
    gates = reopened.uns["gates"]
    first_idx, second_idx = [i for i, n in enumerate(reopened.var_names) if n == "PTPRC"]
    assert gates.iloc[first_idx]["A"] == pytest.approx(2.5)
    assert pd.isna(gates.iloc[second_idx]["A"])


def test_duplicate_var_names_suffixed_name_reaches_second_occurrence(tmp_path):
    """Regression test: the gating UI's channel list is built from
    AnnDataAdapter._deduplicate_names()'s *display* names (e.g. "PTPRC" for
    the first occurrence, "PTPRC_1" for the second) -- that's exactly what
    the frontend sends as the active_gates key. var_index used to be built
    from the raw (still-duplicated) var_names list instead, which has no
    entry literally named "PTPRC_1" -- so a gate set on the second
    occurrence was silently dropped (no error, just missing from the
    written table and undercounted in n_active_gates)."""
    path = tmp_path / "dup.h5ad"
    rng = np.random.default_rng(0)
    n = 6
    obs = pd.DataFrame({"imageid": ["A"] * n}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["marker_0", "PTPRC", "marker_2", "PTPRC"])
    adata = ad.AnnData(X=rng.random((n, 4)).astype(np.float32), obs=obs, var=var)
    adata.write_h5ad(path)

    result = anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"PTPRC": 2.5, "PTPRC_1": 4.0}
    )
    assert result["n_active_gates"] == 2

    reopened = ad.read_h5ad(path)
    gates = reopened.uns["gates"]
    first_idx, second_idx = [i for i, n in enumerate(reopened.var_names) if n == "PTPRC"]
    assert gates.iloc[first_idx]["A"] == pytest.approx(2.5)
    assert gates.iloc[second_idx]["A"] == pytest.approx(4.0)


def test_resave_with_duplicate_var_names_does_not_explode_row_count(tmp_path):
    """Regression test: realigning an existing saved table against current
    var_names used to be done with a polars join on the var_names column.
    With duplicate var_names (real exemplar orion.h5ad has these -- e.g.
    PTPRC appears twice), a join on a non-unique key produces a row
    explosion (each duplicate on one side matches every duplicate on the
    other), raising `ShapeError: unable to add a column of length N to a
    DataFrame of height M` on the second save. This reproduces that exact
    two-save sequence against a duplicate-var_names file."""
    path = tmp_path / "dup.h5ad"
    rng = np.random.default_rng(0)
    n = 6
    obs = pd.DataFrame({"imageid": ["A"] * n}, index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["marker_0", "PTPRC", "marker_2", "PTPRC"])
    adata = ad.AnnData(X=rng.random((n, 4)).astype(np.float32), obs=obs, var=var)
    adata.write_h5ad(path)

    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"marker_0": 1.0}
    )
    # Second save is the one that used to crash -- it's the only call that
    # exercises the "table_name already in uns" realignment branch.
    result = anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"marker_0": 1.0, "PTPRC": 3.5}
    )
    assert result["n_active_gates"] == 2

    gates = ad.read_h5ad(path).uns["gates"]
    assert len(gates) == 4
    assert gates.loc["marker_0", "A"] == pytest.approx(1.0)


def test_missing_imageid_column_falls_back_to_datasource_name(tmp_path):
    path = tmp_path / "single.h5ad"
    rng = np.random.default_rng(0)
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(6)])
    var = pd.DataFrame(index=["marker_0", "marker_1"])
    adata = ad.AnnData(X=rng.random((6, 2)).astype(np.float32), obs=obs, var=var)
    adata.write_h5ad(path)

    result = anndata_gates.save_gates_to_anndata(
        {"dataSource": {"path": str(path)}}, "my_datasource", {"marker_0": 2.0}
    )
    assert result["image_id"] == "my_datasource"

    gates = ad.read_h5ad(path).uns["gates"]
    assert list(gates.columns) == ["my_datasource"]
    assert gates.loc["marker_0", "my_datasource"] == pytest.approx(2.0)


def test_ambiguous_subset_raises(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)

    with pytest.raises(ValueError, match="does not resolve to a single image"):
        anndata_gates.save_gates_to_anndata(
            {"dataSource": {"path": str(path)}},  # no subset -> spans A/B/C
            "ds_all",
            {"marker_0": 1.0},
        )


def test_var_names_drift_reindexes_existing_table(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata(n_vars=3)
    adata.write_h5ad(path)

    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "A"), "ds_a", {"marker_0": 1.0, "marker_2": 3.0}
    )

    # simulate the panel changing: re-register with an extra var and rewrite
    new_adata = ad.read_h5ad(path)
    new_var = pd.DataFrame(index=list(new_adata.var_names) + ["marker_new"])
    n = new_adata.n_obs
    x = np.hstack([new_adata.X, np.zeros((n, 1), dtype=np.float32)])
    replacement = ad.AnnData(X=x, obs=new_adata.obs, var=new_var)
    replacement.uns["gates"] = new_adata.uns["gates"]
    replacement.write_h5ad(path)

    anndata_gates.save_gates_to_anndata(
        _feature_config(path, "B"), "ds_b", {"marker_new": 9.0}
    )

    gates = ad.read_h5ad(path).uns["gates"]
    assert "marker_new" in gates.index
    assert gates.loc["marker_new", "B"] == pytest.approx(9.0)
    assert gates.loc["marker_0", "A"] == pytest.approx(1.0)


def test_pandas_polars_roundtrip():
    df = pd.DataFrame(
        {"A": [1.0, np.nan, 3.0], "B": [np.nan, 5.0, 6.0]},
        index=pd.Index(["m0", "m1", "m2"], name="var_names"),
    )
    polars_df = anndata_gates._pandas_to_polars(df, "var_names")
    assert isinstance(polars_df, pl.DataFrame)
    back = anndata_gates._polars_to_pandas(polars_df, "var_names")
    pd.testing.assert_frame_equal(back, df)
