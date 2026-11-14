"""The centroid tile cache must follow the table's read spec, not just the file.

An AnnData or SpatialData project's table is the file PLUS the read spec: which
image of a multi-image .h5ad (`subset`), which obsm array holds the positions
(`coordinates`), which table of a store. Changing any of those leaves the file,
and the adapter's synthesized 'id'/'X'/'Y' role names, exactly as they were, so
a cache keyed on those alone keeps drawing the old cells at the old positions.
"""
import numpy as np

from plexora.server.models import centroid_tiles

from tests.helpers import anndata_spec, entry, use_data_root


def _geometry(project_entry):
    return {**project_entry, "width": 1024, "height": 1024,
            "tileWidth": 256, "tileHeight": 256, "maxLevel": 1}


def _write_two_image_anndata(path, with_image_column=True):
    """Image A: ten cells in the top-left tile, ids 100..109.
    Image B: four cells in the bottom-right tile, ids 200..203.
    `X_umap` holds every cell in the top-left tile, `spatial` the real places."""
    import anndata as ad
    import pandas as pd

    n_a, n_b = 10, 4
    image = ["A"] * n_a + ["B"] * n_b
    label = list(range(100, 100 + n_a)) + list(range(200, 200 + n_b))
    columns = {"image": pd.Categorical(image), "label": label}
    if not with_image_column:
        # One image's worth of cells as far as the adapter can tell.
        del columns["image"]
    obs = pd.DataFrame(columns, index=[f"cell-{i}" for i in range(n_a + n_b)])
    var = pd.DataFrame(index=["MarkerA"])
    marker = np.concatenate([np.full(n_a, 5.0), np.full(n_b, 5.0)])[:, None]
    adata = ad.AnnData(X=marker.astype(np.float32), obs=obs, var=var)
    spatial = np.concatenate([
        np.stack([np.arange(n_a) * 10 + 20, np.arange(n_a) * 10 + 20], axis=1),
        np.stack([np.arange(n_b) * 10 + 800, np.arange(n_b) * 10 + 800], axis=1),
    ]).astype(np.float64)
    adata.obsm["spatial"] = spatial
    adata.obsm["X_umap"] = np.stack(
        [np.arange(n_a + n_b) * 5 + 10, np.arange(n_a + n_b) * 5 + 10], axis=1
    ).astype(np.float64)
    adata.write_h5ad(path)


def _config(h5ad_path, name, *, subset=None, obsm_key="spatial"):
    return {name: _geometry(entry(name, dataset=anndata_spec(
        h5ad_path,
        coordinates={"source": "obsm", "obsm_key": obsm_key},
        subset=subset,
        obs_id_field="label",
        cell_id="label",
        image_id=None,
        single_image=subset is None,
    )))}


def test_switching_the_subset_to_another_image_redraws_that_images_cells(
        tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "data")
    h5ad = tmp_path / "two_images.h5ad"
    _write_two_image_anndata(h5ad)
    name = "subset_switch"

    image_a = _config(h5ad, name, subset={"column": "image", "value": "A"})
    first = centroid_tiles.get_tiles(image_a, name, 0, [{"x": 0, "y": 0}])
    assert sorted(first["id"].tolist()) == list(range(100, 110))

    # The user picks image B of the same file.
    image_b = _config(h5ad, name, subset={"column": "image", "value": "B"})
    top_left = centroid_tiles.get_tiles(image_b, name, 0, [{"x": 0, "y": 0}])
    bottom_right = centroid_tiles.get_tiles(image_b, name, 0, [{"x": 3, "y": 3}])

    assert top_left.tolist() == [], "image A's cells are still drawn for image B"
    assert sorted(bottom_right["id"].tolist()) == [200, 201, 202, 203]


def test_switching_the_coordinate_source_moves_the_centroids(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "data")
    h5ad = tmp_path / "embedding.h5ad"
    _write_two_image_anndata(h5ad, with_image_column=False)
    name = "coordinate_switch"

    umap = _config(h5ad, name, obsm_key="X_umap")
    first = centroid_tiles.get_tiles(umap, name, 0, [{"x": 0, "y": 0}])
    assert len(first) == 14  # every cell sits in the top-left tile in UMAP space

    # The user corrects the coordinate source to the real positions.
    spatial = _config(h5ad, name, obsm_key="spatial")
    bottom_right = centroid_tiles.get_tiles(spatial, name, 0, [{"x": 3, "y": 3}])
    top_left = centroid_tiles.get_tiles(spatial, name, 0, [{"x": 0, "y": 0}])

    assert sorted(bottom_right["id"].tolist()) == [200, 201, 202, 203], (
        "the UMAP positions are still drawn after switching to 'spatial'")
    assert sorted(top_left["id"].tolist()) == list(range(100, 110))


def test_gated_tiles_after_a_subset_switch_and_restart_gate_the_new_images_cells(
        tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "data")
    h5ad = tmp_path / "gated.h5ad"
    _write_two_image_anndata(h5ad)
    name = "gated_switch"

    image_a = _config(h5ad, name, subset={"column": "image", "value": "A"})
    centroid_tiles.get_tiles(image_a, name, 0, [{"x": 0, "y": 0}])

    # A server restart: nothing in memory, only the cache on disk.
    centroid_tiles._filter_tables.clear()

    image_b = _config(h5ad, name, subset={"column": "image", "value": "B"})
    gates = {"MarkerA": [1.0, 9.0]}
    top_left = centroid_tiles.get_tiles(image_b, name, 0, [{"x": 0, "y": 0}], gates=gates)
    bottom_right = centroid_tiles.get_tiles(image_b, name, 0, [{"x": 3, "y": 3}], gates=gates)

    assert top_left.tolist() == []
    assert sorted(bottom_right["id"].tolist()) == [200, 201, 202, 203]
