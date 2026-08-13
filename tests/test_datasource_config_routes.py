"""Route-level check of the Stage 3 standalone AnnData import UI backend:
GET /upload_page (renders) -> POST /import_anndata (inspects + renders the
config page) -> POST /save_datasource_config (registers, same as
register_anndata_datasource) -> GET /<name> (viewer page lists it). Isolated
from the real app data directory via monkeypatching plexora.data_path/
config_json_path, which register_anndata_datasource()/get_config() both read
live (local imports / same-module globals), matching the pattern already
proven in test_register_anndata_datasource.py.
"""

import json

import anndata as ad
import numpy as np
import pandas as pd
import tifffile

import plexora


def _write_image(path, size=256, channels=2):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


def _write_adata(path, n=12):
    obs = pd.DataFrame(
        {"image_id": ["only_image"] * n, "cell_type": (["typeA", "typeB"] * ((n // 2) + 1))[:n]},
        index=[f"cell_{i}" for i in range(n)],
    )
    var = pd.DataFrame(index=["MarkerA", "MarkerB", "MarkerC"])
    x = np.random.default_rng(0).random((n, 3)).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 200, n, dtype=np.float64), np.linspace(10, 200, n, dtype=np.float64)], axis=1
    )
    adata.write_h5ad(path)


def test_upload_page_renders_source_type_tabs(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    client = plexora.app.test_client()

    response = client.get("/upload_page")

    assert response.status_code == 200
    assert b"source-type-tab" in response.data
    assert b"AnnData" in response.data


def test_import_anndata_then_save_then_viewer_page(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path)

    client = plexora.app.test_client()

    inspect_response = client.post(
        "/import_anndata",
        data={
            "name": "route_test_dataset",
            "channel_file": str(image_path),
            "label_file": "",
            "h5ad_file": str(h5ad_path),
        },
    )
    assert inspect_response.status_code == 200
    assert b"route_test_dataset" in inspect_response.data
    assert b"initDatasourceConfig" in inspect_response.data
    assert b"MarkerA" in inspect_response.data

    save_response = client.post(
        "/save_datasource_config",
        json={
            "name": "route_test_dataset",
            "image": str(image_path),
            "segmentation": None,
            "features": str(h5ad_path),
            "coordinate_source": "obsm",
            "obsm_key": "spatial",
            "feature_source": "X",
        },
    )
    assert save_response.status_code == 200
    save_data = save_response.get_json()
    assert save_data["success"] is True
    assert save_data["name"] == "route_test_dataset"

    config = json.loads((tmp_path / "config.json").read_text())
    assert config["route_test_dataset"]["data_type"] == "anndata"

    viewer_response = client.get("/route_test_dataset")
    assert viewer_response.status_code == 200
    assert b"route_test_dataset" in viewer_response.data


def test_import_anndata_rejects_duplicate_dataset_name(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"existing": {}}), encoding="utf-8")

    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path)

    client = plexora.app.test_client()
    response = client.post(
        "/import_anndata",
        data={
            "name": "existing",
            "channel_file": str(image_path),
            "label_file": "",
            "h5ad_file": str(h5ad_path),
        },
    )

    assert response.status_code == 200
    assert b"already exists" in response.data


def test_save_datasource_config_rejects_ambiguous_multi_image_without_subset(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "multi.h5ad"
    _write_image(image_path)

    n = 20
    obs = pd.DataFrame({"image_id": (["img_a"] * 10) + (["img_b"] * 10)}, index=[f"c{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA"])
    adata = ad.AnnData(X=np.random.default_rng(0).random((n, 1)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 200, n, dtype=np.float64), np.linspace(10, 200, n, dtype=np.float64)], axis=1
    )
    adata.write_h5ad(h5ad_path)

    client = plexora.app.test_client()
    response = client.post(
        "/save_datasource_config",
        json={
            "name": "ambiguous_dataset",
            "image": str(image_path),
            "segmentation": None,
            "features": str(h5ad_path),
            "coordinate_source": "obsm",
            "obsm_key": "spatial",
            "feature_source": "X",
            "subset_by": None,
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert "image_id" in body["error"]
    assert "ambiguous_dataset" not in json.loads((tmp_path / "config.json").read_text())


def test_import_anndata_previews_derived_channel_names(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3)  # matches this file's 3-marker _write_adata below
    _write_adata(h5ad_path)

    client = plexora.app.test_client()
    response = client.post(
        "/import_anndata",
        data={
            "name": "preview_test_dataset",
            "channel_file": str(image_path),
            "label_file": "",
            "h5ad_file": str(h5ad_path),
        },
    )

    assert response.status_code == 200
    assert b"adata.var_names" in response.data
    assert b"MarkerA" in response.data


def test_previewed_channel_names_are_what_gets_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3)  # matches this file's 3-marker _write_adata below
    _write_adata(h5ad_path)

    client = plexora.app.test_client()
    inspect_response = client.post(
        "/import_anndata",
        data={
            "name": "derived_named_dataset",
            "channel_file": str(image_path),
            "label_file": "",
            "h5ad_file": str(h5ad_path),
        },
    )
    assert inspect_response.status_code == 200
    assert b"adata.var_names" in inspect_response.data
    assert b"MarkerA" in inspect_response.data

    save_response = client.post(
        "/save_datasource_config",
        json={
            "name": "derived_named_dataset",
            "image": str(image_path),
            "segmentation": None,
            "features": str(h5ad_path),
            "coordinate_source": "obsm",
            "obsm_key": "spatial",
            "feature_source": "X",
            "channel_names": ["MarkerA", "MarkerB", "MarkerC"],
        },
    )
    assert save_response.status_code == 200
    assert save_response.get_json()["success"] is True

    config = json.loads((tmp_path / "config.json").read_text())
    channel_fullnames = [c["fullname"] for c in config["derived_named_dataset"]["imageData"]]
    assert channel_fullnames == ["MarkerA", "MarkerB", "MarkerC"]
