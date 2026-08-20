import json
from pathlib import Path

import numpy as np
import polars as pl

from plexora.server.models import centroid_tiles

from tests.helpers import anndata_spec, csv_spec, entry


def _config(csv_path, name="sample", max_level=3):
    return {name: _geometry(entry(name, dataset=csv_spec(csv_path, cell_id="CellID", x="x", y="y")),
                            max_level)}


def _geometry(project_entry, max_level=3):
    """centroid_tiles only reads the image's tiling geometry, so give it fixed
    numbers rather than a real pyramid."""
    return {**project_entry, "width": 1024, "height": 1024,
            "tileWidth": 256, "tileHeight": 256, "maxLevel": max_level}


def _write_csv(path, count=32):
    xs = np.arange(count, dtype=np.float32) * 20 + 5
    ys = np.arange(count, dtype=np.float32) * 10 + 7
    df = pl.DataFrame(
        {
            "CellID": np.arange(count, dtype=np.uint32) + 100,
            "x": xs,
            "y": ys,
            "MarkerA": np.linspace(0, 10, count),
            "MarkerB": np.linspace(10, 0, count),
        }
    )
    df.write_csv(path)
    return df


def test_centroid_manifest_created_from_external_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "data_path", tmp_path / "data")
    csv_path = tmp_path / "external.csv"
    _write_csv(csv_path)

    manifest = centroid_tiles.get_manifest(_config(csv_path), "sample", build=True)

    assert manifest["status"] == "ready"
    assert manifest["csv_path"] == str(csv_path.resolve())
    assert manifest["point_count"] == 32
    assert (tmp_path / "data" / "sample" / "centroids_v1" / "manifest.json").exists()


def test_centroid_manifest_rebuilds_when_csv_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "data_path", tmp_path / "data")
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path, count=8)
    config = _config(csv_path)

    first = centroid_tiles.get_manifest(config, "sample", build=True)
    _write_csv(csv_path, count=12)
    second = centroid_tiles.get_manifest(config, "sample", build=True)

    assert first["point_count"] == 8
    assert second["point_count"] == 12
    assert second["csv_size"] != first["csv_size"]


def test_centroid_tile_query_returns_requested_tile_points(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "data_path", tmp_path / "data")
    csv_path = tmp_path / "cells.csv"
    df = _write_csv(csv_path, count=16)
    config = _config(csv_path)
    centroid_tiles.get_manifest(config, "sample", build=True)

    records = centroid_tiles.get_tiles(config, "sample", 0, [{"x": 0, "y": 0}])

    assert records.dtype == centroid_tiles.RESPONSE_DTYPE
    expected = df.filter((pl.col("x") < 256) & (pl.col("y") < 256))["CellID"].cast(pl.UInt32).to_numpy()
    np.testing.assert_array_equal(records["id"], expected)


def test_centroid_tile_query_applies_gates_vectorized(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "data_path", tmp_path / "data")
    csv_path = tmp_path / "cells.csv"
    df = _write_csv(csv_path, count=24)
    config = _config(csv_path)
    centroid_tiles.get_manifest(config, "sample", build=True)

    records = centroid_tiles.get_tiles(
        config,
        "sample",
        0,
        [{"x": 0, "y": 0}, {"x": 1, "y": 0}],
        {"MarkerA": [3.0, 7.0], "MarkerB": [2.0, 8.0]},
    )

    expected = df.filter(
        (pl.col("x") < 512)
        & (pl.col("y") < 256)
        & (pl.col("MarkerA") > 3.0)
        & (pl.col("MarkerA") < 7.0)
        & (pl.col("MarkerB") > 2.0)
        & (pl.col("MarkerB") < 8.0)
    )["CellID"].cast(pl.UInt32).to_numpy()
    np.testing.assert_array_equal(records["id"], expected)


def test_low_zoom_tile_query_respects_max_points(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "data_path", tmp_path / "data")
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path, count=200)
    config = _config(csv_path)
    centroid_tiles.get_manifest(config, "sample", build=True)

    records = centroid_tiles.get_tiles(config, "sample", 1, [{"x": 0, "y": 0}], max_points=20)

    assert len(records) <= 20


def _write_anndata(path, n=20):
    import anndata as ad
    import pandas as pd

    # String obs_names (not small integers) -- the realistic case, and the
    # one that broke get_manifest()/get_tiles() before centroid_tiles.py
    # read the raw .h5ad path with pl.read_csv() (a CSV-only assumption).
    obs = pd.DataFrame(index=[f"cell--{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA", "MarkerB"])
    x = np.stack([np.linspace(0, 10, n), np.linspace(10, 0, n)], axis=1).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.arange(n, dtype=np.float64) * 20 + 5, np.arange(n, dtype=np.float64) * 10 + 7], axis=1
    )
    adata.write_h5ad(path)


def test_centroid_manifest_and_tiles_work_for_anndata_datasource(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(centroid_tiles, "data_path", data_dir)
    h5ad_path = tmp_path / "cells.h5ad"
    _write_anndata(h5ad_path, n=20)

    # register_anndata_datasource() requires a real image (it runs
    # convertOmeTiff for pyramid metadata) -- centroid_tiles doesn't touch the
    # image at all, so build the record directly with fixed geometry.
    config = {
        "anndata_sample": _geometry(entry(
            "anndata_sample",
            dataset=anndata_spec(
                h5ad_path,
                coordinates={"source": "obsm", "obsm_key": "spatial"},
            ),
        ))
    }

    manifest = centroid_tiles.get_manifest(config, "anndata_sample", build=True)
    assert manifest["status"] == "ready"
    assert manifest["point_count"] == 20

    records = centroid_tiles.get_tiles(
        config, "anndata_sample", 0, [{"x": 0, "y": 0}], gates={"MarkerA": [2.0, 8.0]}
    )
    assert len(records) > 0
    assert records["id"].dtype == np.uint32
