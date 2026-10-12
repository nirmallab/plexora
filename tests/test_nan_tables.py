"""A cell table with NaNs in it.

A NaN is a value nobody measured, and real tables have them: a marker that
failed on some cells, a phenotype nobody assigned, a cell whose centroid a
pipeline could not compute. Each is ignored where it is -- never refused:

- a NaN marker value is never gated positive, and is left out of the summary
  statistics and the gate's mixture fit (this already held; pinned here);
- a missing metadata value is a missing level, not a crash -- AnnData's
  categorical celltype with NaN in it used to fail the whole load;
- a NaN x or y is a cell with no position. The row stays (every row-aligned
  array still counts it) but it is nowhere on the image, and the consistency
  report says how many. One such cell used to fail the whole table: sklearn's
  "Input contains NaN" from the hover tree for a CSV or parquet, and the
  AnnData adapter's own "coordinates contain non-finite values".
"""

import json
import pickle

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora import paths
from plexora.server.models import consistency, data_model
from plexora.server.models.adapters.anndata_adapter import AnnDataAdapter

from tests.helpers import anndata_spec, entry, use_data_root

ROWS = 40
#: Rows whose x (3) or y (9) is NaN in `_frame(nan_xy=True)`.
UNPLACED = (3, 9)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A 256x256 image and no table yet -- test_consistency's fixture shape."""
    use_data_root(monkeypatch, tmp_path)
    for name in ("ball_tree", "ball_tree_rows", "source", "config", "seg",
                 "zarray", "channels", "metadata", "_loaded_source", "datasource"):
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": entry("proj", dataset=None, src=str(image),
                                  width=256, height=256)}),
        encoding="utf-8")
    return plexora.app.test_client()


def _frame(*, nan_xy=False):
    rng = np.random.default_rng(0)
    cd3 = rng.uniform(0, 3, ROWS)
    cd3[::5] = np.nan
    phenotype = np.array(["T", "B", "Tumor", "Stroma"] * (ROWS // 4), dtype=object)
    phenotype[::7] = None
    x = rng.uniform(1, 250, ROWS)
    y = rng.uniform(1, 250, ROWS)
    if nan_xy:
        x[UNPLACED[0]] = np.nan
        y[UNPLACED[1]] = np.nan
    return pl.DataFrame({
        "CellID": np.arange(1, ROWS + 1, dtype=np.uint32),
        "X_centroid": x, "Y_centroid": y,
        "CD3": cd3, "CD8": rng.uniform(0, 3, ROWS),
        "phenotype": pl.Series(list(phenotype), dtype=pl.Utf8),
    })


def _attach(client, tmp_path, frame, suffix):
    path = tmp_path / f"cells{suffix}"
    if suffix == ".csv":
        frame.write_csv(path)
    else:
        frame.write_parquet(path)
    response = client.post("/proj/requirements", json={"data": str(path)})
    assert response.status_code == 200, response.get_json()
    return path


def _nearest(client, x, y):
    return client.get(f"/get_nearest_cell?datasource=proj&point_x={x}&point_y={y}").get_json()


# -- markers and metadata ---------------------------------------------------

@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_a_nan_marker_is_never_gated_positive(client, tmp_path, suffix):
    frame = _frame()
    _attach(client, tmp_path, frame, suffix)

    gate = json.dumps({"CD3": [-1.0, 10.0]})   # wide enough to take every value
    ids = {row["id"] for row in client.get(
        f"/get_channel_cell_ids?datasource=proj&filter={gate}").get_json()}

    # `id` is the row's position, which is what every row-aligned array keys on.
    nan_rows = set(np.flatnonzero(np.isnan(frame["CD3"].to_numpy())).tolist())
    assert nan_rows and not ids & nan_rows
    assert len(ids) == ROWS - len(nan_rows)


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_the_summary_and_the_fit_skip_nan(client, tmp_path, suffix):
    _attach(client, tmp_path, _frame(), suffix)

    summary = client.get("/get_database_description?datasource=proj").get_json()
    assert summary["CD3"]["count"] == ROWS - len(range(0, ROWS, 5))

    fit = client.post("/plugins/gating/get_gating_gmm", data=json.dumps(
        {"channel": "CD3", "datasource": "proj", "selection_ids": []}))
    assert fit.status_code == 200
    assert np.isfinite(fit.get_json()["gate"])


# -- coordinates ------------------------------------------------------------

@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_a_nan_coordinate_no_longer_fails_the_table(client, tmp_path, suffix):
    frame = _frame(nan_xy=True)
    _attach(client, tmp_path, frame, suffix)

    # Hover over where every placed cell is: each answers as itself, and the
    # tree's positions map back to the right rows -- a tree built on the placed
    # rows alone, read without that map, would answer with the row two up.
    for row in range(ROWS):
        if row in UNPLACED:
            continue
        x, y = frame["X_centroid"][row], frame["Y_centroid"][row]
        assert _nearest(client, x, y)["CellID"] == row + 1

    # Every row is still in the table.
    summary = client.get("/get_database_description?datasource=proj").get_json()
    assert summary["CD8"]["count"] == ROWS


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_the_report_counts_the_cells_it_cannot_draw(client, tmp_path, suffix):
    _attach(client, tmp_path, _frame(nan_xy=True), suffix)

    report = client.get("/get_consistency_report?datasource=proj").get_json()

    assert [finding["code"] for finding in report] == ["cells_without_position"]
    assert report[0]["message"].startswith(f"2 of {ROWS} cells have no x/y position")


def test_a_table_with_every_cell_placed_reports_nothing(client, tmp_path):
    _attach(client, tmp_path, _frame(), ".csv")
    assert client.get("/get_consistency_report?datasource=proj").get_json() == []


def test_the_cached_tree_keeps_its_row_map(client, tmp_path):
    """The tree is pickled beside the project. Reloaded from that file without
    its rows, every hover after the first NaN row would be off by one."""
    frame = _frame(nan_xy=True)
    _attach(client, tmp_path, frame, ".csv")
    cached = pickle.loads(
        (paths.derived_root("proj") / "ball_tree.pickle").read_bytes())
    expected = [row for row in range(ROWS) if row not in UNPLACED]
    assert cached["rows"].tolist() == expected

    data_model.load_ball_tree("proj")          # from the pickle, this time
    row = ROWS - 1
    hit = _nearest(client, frame["X_centroid"][row], frame["Y_centroid"][row])
    assert hit["CellID"] == row + 1


def test_the_centroid_tiles_route_answers_a_project_without_a_table(client):
    response = client.post("/get_centroid_tiles", data=json.dumps(
        {"datasource": "proj", "level": 0, "tiles": [{"x": 0, "y": 0}]}))
    assert response.status_code == 200
    assert response.headers["X-Centroid-Record-Count"] == "0"


def test_unplaced_findings_are_silent_for_none_and_for_all():
    """All of them is `no_cells`, which says it already."""
    assert consistency._unplaced_findings(0, 10) == []
    assert consistency._unplaced_findings(10, 10) == []
    [finding] = consistency._unplaced_findings(1, 10)
    assert finding["message"].startswith("1 of 10 cell has")


# -- AnnData ----------------------------------------------------------------

ad = pytest.importorskip("anndata")
pd = pytest.importorskip("pandas")


def _h5ad(tmp_path, *, nan_xy=False, all_nan_xy=False):
    frame = _frame(nan_xy=nan_xy).to_pandas()
    obs = pd.DataFrame(
        {"CellID": frame.CellID.astype(int).values,
         "phenotype": pd.Categorical(frame.phenotype.values)},
        index=[str(i) for i in range(ROWS)])
    adata = ad.AnnData(X=frame[["CD3", "CD8"]].to_numpy(dtype=np.float32), obs=obs,
                       var=pd.DataFrame(index=["CD3", "CD8"]))
    spatial = frame[["X_centroid", "Y_centroid"]].to_numpy().copy()
    if all_nan_xy:
        spatial[:] = np.nan
    adata.obsm["spatial"] = spatial
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)
    return path


def test_an_anndata_with_nan_coordinates_loads(tmp_path):
    table = AnnDataAdapter(anndata_spec(_h5ad(tmp_path, nan_xy=True))).load_table().table
    assert table.height == ROWS
    assert table["X"].is_nan().sum() == 1 and table["Y"].is_nan().sum() == 1


def test_an_anndata_with_no_position_at_all_is_still_refused(tmp_path):
    """Not missing values: a read spec pointing at the wrong thing."""
    adapter = AnnDataAdapter(anndata_spec(_h5ad(tmp_path, all_nan_xy=True)))
    with pytest.raises(ValueError, match="no cell has a position"):
        adapter.plan()


def test_a_missing_celltype_in_a_categorical_is_an_empty_string(tmp_path):
    spec = anndata_spec(_h5ad(tmp_path), celltype="phenotype")
    table = AnnDataAdapter(spec).load_table().table

    celltypes = table["phenotype"].to_list()
    assert celltypes[0] == "" and celltypes[7] == ""
    assert celltypes[1] == "B"


def test_an_anndata_with_nans_imports_through_the_route(client, tmp_path):
    response = client.post("/proj/requirements",
                           json={"data": str(_h5ad(tmp_path, nan_xy=True))})
    assert response.status_code == 200, response.get_json()
    codes = [f["code"] for f in
             client.get("/get_consistency_report?datasource=proj").get_json()]
    assert codes == ["cells_without_position"]
