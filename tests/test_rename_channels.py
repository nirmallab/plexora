"""Tests for the post-hoc channel rename path: datasource.rename_channels()
directly, and the /upload_channels route that now uses it (replacing the old
feature that applied per-channel display settings -- color/range/active --
from an uploaded CSV) to fix gating/channel auto-matching on an already-
registered datasource without re-running image pyramid generation.
"""

import io
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile

import plexora
from plexora import datasource
from plexora.server.models import data_model


def _write_image(path, size=256, channels=2):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


def _write_adata(path, n=10):
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=["MarkerA", "MarkerB"])
    x = np.random.default_rng(0).random((n, 2)).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 50, n, dtype=np.float64), np.linspace(10, 50, n, dtype=np.float64)], axis=1
    )
    adata.write_h5ad(path)


def _register(tmp_path, name="rename_sample"):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path)
    _write_adata(h5ad_path)
    datasource.register_anndata_datasource(
        name=name,
        image=image_path,
        features=h5ad_path,
        coordinate_source="obsm",
        obsm_key="spatial",
        data_dir=data_dir,
    )
    return data_dir


def test_rename_channels_updates_config(tmp_path):
    data_dir = _register(tmp_path)

    entry = datasource.rename_channels("rename_sample", ["DAPI", "CD3"], data_dir=data_dir)

    fullnames = [c["fullname"] for c in entry["imageData"]]
    assert fullnames == ["DAPI", "CD3"]
    config = json.loads((data_dir / "config.json").read_text())
    assert [c["fullname"] for c in config["rename_sample"]["imageData"]] == ["DAPI", "CD3"]


def test_rename_channels_rejects_wrong_length(tmp_path):
    data_dir = _register(tmp_path)

    with pytest.raises(ValueError, match="channel_names has 1 entries but 'rename_sample' has 2 channels"):
        datasource.rename_channels("rename_sample", ["OnlyOne"], data_dir=data_dir)


def test_rename_channels_rejects_unknown_datasource(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="No datasource named 'missing'"):
        datasource.rename_channels("missing", ["A", "B"], data_dir=data_dir)


def test_upload_channels_route_renames_without_header(tmp_path, monkeypatch):
    data_dir = _register(tmp_path)
    monkeypatch.setattr(plexora, "data_path", data_dir)
    monkeypatch.setattr(plexora, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)
    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")

    client = plexora.app.test_client()
    response = client.post(
        "/upload_channels",
        data={
            "datasource": "rename_sample",
            "file": (io.BytesIO(b"DAPI\nCD3\n"), "channels.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    config = json.loads((data_dir / "config.json").read_text())
    assert [c["fullname"] for c in config["rename_sample"]["imageData"]] == ["DAPI", "CD3"]
    assert data_model.source == "rename_sample"


def test_upload_channels_route_drops_header_row(tmp_path, monkeypatch):
    data_dir = _register(tmp_path)
    monkeypatch.setattr(plexora, "data_path", data_dir)
    monkeypatch.setattr(plexora, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)
    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")

    client = plexora.app.test_client()
    response = client.post(
        "/upload_channels",
        data={
            "datasource": "rename_sample",
            "file": (io.BytesIO(b"channel_name\nDAPI\nCD3\n"), "channels.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    config = json.loads((data_dir / "config.json").read_text())
    assert [c["fullname"] for c in config["rename_sample"]["imageData"]] == ["DAPI", "CD3"]


def test_upload_channels_route_rejects_wrong_length(tmp_path, monkeypatch):
    data_dir = _register(tmp_path)
    monkeypatch.setattr(plexora, "data_path", data_dir)
    monkeypatch.setattr(plexora, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)
    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")

    client = plexora.app.test_client()
    response = client.post(
        "/upload_channels",
        data={
            "datasource": "rename_sample",
            "file": (io.BytesIO(b"DAPI\nCD3\nCD8\nCD20\n"), "channels.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert "4 entries but 'rename_sample' has 2 channels" in body["error"]
    config = json.loads((data_dir / "config.json").read_text())
    assert [c["fullname"] for c in config["rename_sample"]["imageData"]] == ["MarkerA", "MarkerB"]
