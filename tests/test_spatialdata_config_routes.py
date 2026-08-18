"""Route-level check of the SpatialData import UI backend:
POST /list_spatialdata_tables (fills the form's table picker) ->
POST /import_spatialdata (inspects the chosen table + renders the config
page) -> POST /save_datasource_config (registers it). Mirrors
test_datasource_config_routes.py's AnnData walk, which shares every step
after the table is chosen -- isolated from the real app data directory the
same way.
"""

import json

import anndata as ad
import numpy as np
import pandas as pd
import spatialdata as sd
import tifffile

import plexora


def _write_image(path, size=256, channels=3):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


def _make_adata(n=12, markers=("MarkerA", "MarkerB", "MarkerC"), image_ids=("only_image",)):
    obs_rows = []
    obs_names = []
    for image_id in image_ids:
        for i in range(n // len(image_ids)):
            obs_names.append(f"{image_id}_cell_{i}")
            obs_rows.append(image_id)
    total = len(obs_names)
    obs = pd.DataFrame(
        {
            "image_id": obs_rows,
            "cell_type": (["typeA", "typeB"] * ((total // 2) + 1))[:total],
        },
        index=obs_names,
    )
    adata = ad.AnnData(
        X=np.random.default_rng(0).random((total, len(markers))).astype(np.float32),
        obs=obs,
        var=pd.DataFrame(index=list(markers)),
    )
    adata.obsm["spatial"] = np.stack(
        [
            np.linspace(10, 200, total, dtype=np.float64),
            np.linspace(10, 200, total, dtype=np.float64),
        ],
        axis=1,
    )
    return adata


def _write_store(path, tables=None):
    sd.SpatialData(tables=tables if tables is not None else {"cells": _make_adata()}).write(path)
    return path


def _isolate(tmp_path, monkeypatch, config="{}"):
    """Point every reader of the app data directory at tmp_path.

    data_model.py does `from plexora import config_json_path, data_path` --
    a by-value import bound at module load -- so patching only the plexora
    module leaves data_model reading the real user config.json. That matters
    for /save_datasource_config, whose final load_datasource(reload=True)
    goes through data_model.load_config().
    """
    from plexora.server.models import data_model

    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    monkeypatch.setattr(data_model, "data_path", tmp_path)
    monkeypatch.setattr(data_model, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(config, encoding="utf-8")
    return plexora.app.test_client()


def test_upload_page_offers_the_spatialdata_source_type(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)

    response = client.get("/upload_page")

    assert response.status_code == 200
    assert b'data-type="spatialdata"' in response.data
    assert b'id="spatialdata_form"' in response.data
    # The store is a directory, so its Browse button must open a folder picker.
    assert b'data-browse-target="spatialdata_zarr_store"' in response.data
    assert b'data-browse-mode="directory"' in response.data


def test_list_tables_reports_every_table_with_its_shape(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    store = _write_store(
        tmp_path / "s.zarr",
        {"cells": _make_adata(n=12), "embeddings": _make_adata(n=4, markers=tuple(f"e{i}" for i in range(8)))},
    )

    response = client.post("/list_spatialdata_tables", json={"path": str(store)})

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["tables"] == [
        {"name": "cells", "n_obs": 12, "n_var": 3},
        {"name": "embeddings", "n_obs": 4, "n_var": 8},
    ]


def test_list_tables_rejects_a_path_that_is_not_a_store(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()

    assert client.post("/list_spatialdata_tables", json={"path": str(plain)}).status_code == 400
    assert client.post("/list_spatialdata_tables", json={"path": str(tmp_path / "nope")}).status_code == 400


def test_import_spatialdata_then_save_then_viewer_page(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    image_path = tmp_path / "image.tif"
    _write_image(image_path)
    store = _write_store(tmp_path / "s.zarr")

    inspect_response = client.post(
        "/import_spatialdata",
        data={
            "name": "sd_dataset",
            "channel_file": str(image_path),
            "label_file": "",
            "zarr_store": str(store),
            "spatialdata_table": "cells",
        },
    )
    assert inspect_response.status_code == 200
    assert b"sd_dataset" in inspect_response.data
    assert b"initDatasourceConfig" in inspect_response.data
    assert b"MarkerA" in inspect_response.data
    assert b"SpatialData Datasource" in inspect_response.data

    save_response = client.post(
        "/save_datasource_config",
        json={
            "name": "sd_dataset",
            "image": str(image_path),
            "segmentation": None,
            "features": str(store),
            "table": "cells",
            "coordinate_source": "obsm",
            "obsm_key": "spatial",
            "feature_source": "X",
        },
    )
    assert save_response.status_code == 200
    assert save_response.get_json()["success"] is True

    config = json.loads((tmp_path / "config.json").read_text())
    entry = config["sd_dataset"]
    assert entry["data_type"] == "spatialdata"
    data_source = entry["featureData"][0]["dataSource"]
    assert data_source["format"] == "spatialdata"
    assert data_source["table"] == "cells"
    # The store root is recorded, not the table subpath -- a plugin needing
    # the store's other elements opens it from here.
    assert data_source["path"] == str(store)

    assert client.get("/sd_dataset").status_code == 200


def test_import_spatialdata_requires_a_table_choice(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    image_path = tmp_path / "image.tif"
    _write_image(image_path)
    store = _write_store(tmp_path / "s.zarr")

    response = client.post(
        "/import_spatialdata",
        data={
            "name": "sd_dataset",
            "channel_file": str(image_path),
            "zarr_store": str(store),
            "spatialdata_table": "",
        },
    )

    assert response.status_code == 200
    assert b"Select which table" in response.data
    assert json.loads((tmp_path / "config.json").read_text()) == {}


def test_import_spatialdata_reports_an_unknown_table(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    image_path = tmp_path / "image.tif"
    _write_image(image_path)
    store = _write_store(tmp_path / "s.zarr")

    response = client.post(
        "/import_spatialdata",
        data={
            "name": "sd_dataset",
            "channel_file": str(image_path),
            "zarr_store": str(store),
            "spatialdata_table": "not_a_table",
        },
    )

    assert response.status_code == 200
    assert b"Could not read SpatialData table" in response.data


def test_import_spatialdata_rejects_a_missing_store(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    image_path = tmp_path / "image.tif"
    _write_image(image_path)

    response = client.post(
        "/import_spatialdata",
        data={
            "name": "sd_dataset",
            "channel_file": str(image_path),
            "zarr_store": str(tmp_path / "missing.zarr"),
            "spatialdata_table": "cells",
        },
    )

    assert response.status_code == 200
    assert b"SpatialData store (.zarr) path does not exist." in response.data


def test_import_spatialdata_rejects_a_duplicate_dataset_name(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch, config=json.dumps({"existing": {}}))

    image_path = tmp_path / "image.tif"
    _write_image(image_path)
    store = _write_store(tmp_path / "s.zarr")

    response = client.post(
        "/import_spatialdata",
        data={
            "name": "existing",
            "channel_file": str(image_path),
            "zarr_store": str(store),
            "spatialdata_table": "cells",
        },
    )

    assert response.status_code == 200
    assert b"already exists" in response.data


def test_save_rejects_ambiguous_multi_image_table_without_subset(tmp_path, monkeypatch):
    """AnnDataAdapter's ambiguity guard is inherited, so nothing is written
    when the chosen table spans several images and no subset was picked."""
    client = _isolate(tmp_path, monkeypatch)
    image_path = tmp_path / "image.tif"
    _write_image(image_path, channels=1)
    store = _write_store(
        tmp_path / "s.zarr",
        {"cells": _make_adata(n=20, markers=("MarkerA",), image_ids=("img_a", "img_b"))},
    )

    response = client.post(
        "/save_datasource_config",
        json={
            "name": "ambiguous_sd",
            "image": str(image_path),
            "features": str(store),
            "table": "cells",
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
    assert "ambiguous_sd" not in json.loads((tmp_path / "config.json").read_text())
