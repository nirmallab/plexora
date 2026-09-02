from dataclasses import replace

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from plexora.server.models.project import ColumnRoles, DataSpec
from plexora.server.models.adapters import AnnDataAdapter, get_adapter
from plexora.server.models.adapters.anndata_adapter import (
    DEFAULT_ID_COLUMN,
    is_likely_image_identifier_name,
)
from plexora.server.models.adapters.inspection import (
    inspect_anndata,
    propose_read_spec,
)


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
    """A DataSpec from the read-spec kwargs these tests already speak.

    They were written against the old nested `dataSource` dict, so this keeps
    that vocabulary at the call sites and translates once, here.
    """
    overrides = dict(data_source_overrides or {})
    return DataSpec(
        type="anndata",
        src=overrides.get("path") or "",
        coordinates=overrides.get("coordinates") or {},
        features=overrides.get("features") or {"source": "X"},
        subset=overrides.get("subset") or {},
        obs_id_field=overrides.get("obs_id_field"),
        is_transformed=bool(overrides.get("apply_log_transform")),
        roles=ColumnRoles(
            cell_id=overrides.get("obs_id_field") or "id",
            x="X",
            y="Y",
            celltype=celltype,
        ),
    )


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
    obs_ids = normalized.table[DEFAULT_ID_COLUMN].to_list()
    assert obs_ids == expected_ids
    assert not any(obs_id.startswith("image_01") for obs_id in obs_ids)
    assert not any(obs_id.startswith("image_03") for obs_id in obs_ids)


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


# --------------------------------------------------------------------------
# Which image the rows belong to
#
# The old guard fired only when a column-name heuristic recognised the name, so
# a table keyed on "roi" or "core" loaded whole and drew several images' cells
# over one image with nothing said. An answered image-id role is a better
# signal than any heuristic, and is checked in preference to it.
# --------------------------------------------------------------------------

def _multi_image_adata(column, n=6):
    adata = _make_single_image_adata(n=n)
    adata.obs[column] = (["one", "two"] * n)[:n]
    return adata


def _config_with_image_id(path, column):
    config = _feature_config({"path": str(path), "coordinates": {},
                              "features": {"source": "X"}})
    return replace(config, roles=replace(config.roles, image_id=column))


def test_a_named_image_column_is_checked_even_when_its_name_says_nothing(tmp_path):
    """"condition" is not a name the heuristic recognises, which is the whole
    point: the user told us this column identifies the image, so it gets asked
    rather than a guess about which column to ask. Without this the table
    loaded whole and drew two images' cells over one image."""
    path = tmp_path / "condition.h5ad"
    _multi_image_adata("condition").write_h5ad(path)
    assert is_likely_image_identifier_name("condition") is False

    with pytest.raises(ValueError, match="covers 2 images"):
        AnnDataAdapter(_config_with_image_id(path, "condition")).load_table()


def test_a_named_image_column_with_one_value_loads(tmp_path):
    """The answer that says this table is one image. Nothing to refuse."""
    path = tmp_path / "one_condition.h5ad"
    adata = _make_single_image_adata(n=6)
    adata.obs["condition"] = ["only"] * 6
    adata.write_h5ad(path)

    normalized = AnnDataAdapter(_config_with_image_id(path, "condition")).load_table()

    assert normalized.table.height == 6


def test_a_subset_answers_the_question_so_the_column_is_not_re_checked(tmp_path):
    """Having picked one image, the column legitimately has one value left --
    re-raising on the pre-subset table would refuse the very fix it asked for."""
    path = tmp_path / "subset_condition.h5ad"
    _multi_image_adata("condition").write_h5ad(path)
    config = _config_with_image_id(path, "condition")
    config = replace(config, subset={"column": "condition", "value": "one"})

    normalized = AnnDataAdapter(config).load_table()

    assert normalized.table.height == 3


# --------------------------------------------------------------------------
# What the importer proposes, and what it can only propose
#
# propose_read_spec picks a coordinate source from the file's own structure so
# the import page can ask for a path and nothing else. Its coordinate branch had
# no unit coverage at all -- it was only ever exercised through the routes --
# which is how a name-only preference went unexamined for so long. What it
# returns is a PREFILL: the surfaces put it in front of the user to confirm,
# because nothing here can tell a position from an embedding.
# --------------------------------------------------------------------------

def test_the_importer_proposes_a_conventionally_named_obsm_array(tmp_path):
    path = tmp_path / "spatial.h5ad"
    _make_single_image_adata(n=6).write_h5ad(path)

    proposal = propose_read_spec(inspect_anndata(path))

    assert proposal["coordinates"] == {"source": "obsm", "obsm_key": "spatial"}


def test_the_proposal_cannot_tell_a_position_from_an_embedding(tmp_path):
    """The reason it is a prefill and not a decision. Both arrays are (n, 2)
    float32 and only the name separates them -- so a file whose UMAP happens to
    be called "spatial" would be proposed as the cell positions, and the user
    has to be shown both to catch it."""
    path = tmp_path / "ambiguous.h5ad"
    adata = _make_single_image_adata(n=6)
    adata.obsm["X_umap"] = adata.obsm["spatial"][:, ::-1].copy()
    adata.write_h5ad(path)

    inspection = inspect_anndata(path)
    proposal = propose_read_spec(inspection)

    assert proposal["coordinates"] == {"source": "obsm", "obsm_key": "spatial"}
    # Both are offered downstream, with the shapes that show they are
    # indistinguishable.
    assert {e["name"]: e["shape"] for e in inspection["obsm"]} == {
        "spatial": [6, 2], "X_umap": [6, 2]}


def test_the_importer_falls_back_to_obs_columns(tmp_path):
    path = tmp_path / "obs_only.h5ad"
    adata = _make_single_image_adata(n=6)
    del adata.obsm["spatial"]
    adata.obs["X_centroid"] = np.arange(6, dtype=np.float64)
    adata.obs["Y_centroid"] = np.arange(6, dtype=np.float64)
    adata.write_h5ad(path)

    proposal = propose_read_spec(inspect_anndata(path))

    assert proposal["coordinates"] == {
        "source": "obs", "x_column": "X_centroid", "y_column": "Y_centroid"}


def test_an_unresolvable_coordinate_source_is_left_unanswered(tmp_path):
    """Not an error at import: it leaves the question open for whoever needs
    coordinates to ask it, which is now a real control rather than a blank."""
    path = tmp_path / "nothing.h5ad"
    adata = _make_single_image_adata(n=6)
    del adata.obsm["spatial"]
    adata.write_h5ad(path)

    assert propose_read_spec(inspect_anndata(path))["coordinates"] == {}


def test_inspection_reports_every_obsm_array_with_its_shape(tmp_path):
    path = tmp_path / "shapes.h5ad"
    adata = _make_single_image_adata(n=6)
    adata.obsm["X_pca"] = np.zeros((6, 50), dtype=np.float32)
    adata.write_h5ad(path)

    obsm = {e["name"]: e["shape"] for e in inspect_anndata(path)["obsm"]}

    assert obsm == {"spatial": [6, 2], "X_pca": [6, 50]}


# ---------------------------------------------------------------------------
# The plan()/stream() split: what keeps a large file openable.
#
# These are the regression net for the change that made a 60-image .h5ad
# importable. The failure they guard against is not an exception -- it is
# somebody putting a full read back on the loading path, which shows up only as
# a machine running out of memory on data no test fixture is big enough to be.
# ---------------------------------------------------------------------------


def _sparse_adata(n=64, k=5, layout="csr"):
    import scipy.sparse as sp

    rng = np.random.default_rng(7)
    dense = rng.random((n, k)).astype(np.float32)
    matrix = {"csr": sp.csr_matrix, "csc": sp.csc_matrix}[layout](dense)
    obs = pd.DataFrame({"image_id": ["img_a"] * n}, index=[f"cell_{i}" for i in range(n)])
    adata = ad.AnnData(X=matrix, obs=obs, var=pd.DataFrame(index=[f"m{j}" for j in range(k)]))
    adata.obsm["spatial"] = np.stack([np.arange(n), np.arange(n) * 2.0], axis=1)
    return adata, dense


def test_plan_never_opens_the_matrix(tmp_path, monkeypatch):
    """The one test that stops this regressing.

    `plan()` answers from obs and var alone, so sabotaging the whole-file read
    must not disturb it. If someone reintroduces `_read_adata()` on the planning
    path -- which is exactly what `load_table()` used to open with -- this fails
    immediately instead of a user's machine failing later.
    """
    path = tmp_path / "planned.h5ad"
    _make_multi_image_adata(per_image=9).write_h5ad(path)

    def explode(self):
        raise AssertionError("plan() must not read the whole file")

    monkeypatch.setattr(AnnDataAdapter, "_read_adata", explode)

    config = _feature_config({
        "path": str(path), "coordinates": {}, "features": {"source": "X"},
        "subset": {"column": "image_id", "value": "image_02"},
    })
    planned = AnnDataAdapter(config).plan()

    assert planned.rows == 9
    assert planned.feature_columns == ["protein_0", "protein_1", "protein_2"]
    assert planned.obs_columns == ["image_id"]
    # And the row selection is what h5py fancy indexing requires.
    assert list(planned.row_indices) == sorted(planned.row_indices)


def test_plan_raises_the_spec_errors_without_reading_the_matrix(tmp_path, monkeypatch):
    """Every read-spec error is a planning error.

    This is the contract `_reload_or_restore` depends on: a bad answer has to
    come back as a ValueError in milliseconds so the previous project can be
    put back, rather than after a full read.
    """
    path = tmp_path / "errors.h5ad"
    _make_multi_image_adata(per_image=5).write_h5ad(path)
    monkeypatch.setattr(AnnDataAdapter, "_read_adata",
                        lambda self: pytest.fail("plan() read the matrix"))

    def plan_with(**overrides):
        base = {"path": str(path), "coordinates": {}, "features": {"source": "X"}}
        base.update(overrides)
        return lambda: AnnDataAdapter(_feature_config(base)).plan()

    with pytest.raises(ValueError, match="more than one distinct"):
        plan_with()()
    with pytest.raises(ValueError, match="not found in adata.obs"):
        plan_with(subset={"column": "nope", "value": "x"})()
    with pytest.raises(ValueError, match="No observations match"):
        plan_with(subset={"column": "image_id", "value": "image_99"})()
    with pytest.raises(ValueError, match="not found in adata.obsm"):
        plan_with(subset={"column": "image_id", "value": "image_02"},
                  coordinates={"source": "obsm", "obsm_key": "missing"})()
    with pytest.raises(ValueError, match="not found in adata.layers"):
        plan_with(subset={"column": "image_id", "value": "image_02"},
                  features={"source": "layer", "layer": "absent"})()


@pytest.mark.parametrize("layout", ["dense", "csr", "csc"])
def test_streamed_values_match_the_source_for_every_matrix_layout(tmp_path, layout):
    """A CSR row-block read, a CSC column read and a dense slab must all produce
    the same numbers. CSC takes a genuinely different code path -- row blocks
    against a column-major matrix are pathological, so it is streamed by column
    instead -- and nothing else in the suite exercises it."""
    path = tmp_path / f"{layout}.h5ad"
    if layout == "dense":
        adata, expected = _sparse_adata(layout="csr")
        adata = ad.AnnData(X=expected, obs=adata.obs, var=adata.var,
                           obsm={"spatial": adata.obsm["spatial"]})
    else:
        adata, expected = _sparse_adata(layout=layout)
    adata.write_h5ad(path)

    config = _feature_config({"path": str(path), "coordinates": {},
                              "features": {"source": "X"}})
    table = AnnDataAdapter(config).load_table().table

    got = np.stack([table[f"m{j}"].to_numpy() for j in range(expected.shape[1])], axis=1)
    np.testing.assert_allclose(got, expected)


def test_row_blocks_smaller_than_the_table_still_cover_every_row(tmp_path, monkeypatch):
    """Block boundaries, spans and the short final block.

    The default block is 65536 rows, so every other test in this file reads its
    fixture in a single block and would not notice an off-by-one in the loop.
    """
    from plexora.server.models.adapters import anndata_adapter

    path = tmp_path / "blocks.h5ad"
    adata, expected = _sparse_adata(n=50, k=3, layout="csr")
    adata.write_h5ad(path)
    monkeypatch.setattr(anndata_adapter, "_block_rows", lambda *a, **k: 7)

    config = _feature_config({"path": str(path), "coordinates": {},
                              "features": {"source": "X"}})
    table = AnnDataAdapter(config).load_table().table

    got = np.stack([table[f"m{j}"].to_numpy() for j in range(3)], axis=1)
    np.testing.assert_allclose(got, expected)
    assert table.height == 50


def test_a_scattered_subset_reads_the_right_rows(tmp_path, monkeypatch):
    """A subset whose rows are not contiguous takes the fancy-indexing branch
    rather than the offset-slab one, and must select the same cells."""
    from plexora.server.models.adapters import anndata_adapter

    rng = np.random.default_rng(3)
    n = 60
    dense = rng.random((n, 3)).astype(np.float32)
    # Interleaved, so the kept rows are never adjacent.
    obs = pd.DataFrame({"image_id": [f"img_{i % 3}" for i in range(n)]},
                       index=[f"cell_{i}" for i in range(n)])
    adata = ad.AnnData(X=dense, obs=obs, var=pd.DataFrame(index=["a", "b", "c"]))
    adata.obsm["spatial"] = np.stack([np.arange(n), np.arange(n) * 1.0], axis=1)
    path = tmp_path / "scattered.h5ad"
    adata.write_h5ad(path)
    monkeypatch.setattr(anndata_adapter, "_block_rows", lambda *a, **k: 5)

    config = _feature_config({
        "path": str(path), "coordinates": {}, "features": {"source": "X"},
        "subset": {"column": "image_id", "value": "img_1"},
    })
    table = AnnDataAdapter(config).load_table().table

    wanted = np.asarray([i for i in range(n) if i % 3 == 1])
    got = np.stack([table[c].to_numpy() for c in ("a", "b", "c")], axis=1)
    np.testing.assert_allclose(got, dense[wanted])
    np.testing.assert_allclose(table["X"].to_numpy(), wanted.astype(float))


def test_markers_are_float32_and_coordinates_stay_float64(tmp_path):
    """Deliberate asymmetry: forty markers narrow to float32 and save real
    memory, two coordinate columns stay float64 because a centroid rounded in
    the seventh digit is a cell drawn somewhere else."""
    import polars as pl

    path = tmp_path / "dtypes.h5ad"
    _make_single_image_adata(n=12).write_h5ad(path)
    table = AnnDataAdapter(_feature_config({
        "path": str(path), "coordinates": {}, "features": {"source": "X"}})).load_table().table

    assert table["protein_0"].dtype == pl.Float32
    assert table["X"].dtype == pl.Float64
    assert table["Y"].dtype == pl.Float64
    assert table["id"].dtype == pl.Int64
