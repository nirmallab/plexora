"""Deleting the project the server has open lets it go.

Whether a project is "already loaded" is decided on its name. Deleting the
loaded project used to leave the server holding it -- its image, channel list
and cell table -- so a new project imported afterwards under the same name
opened onto the deleted one: the old pixels, the old channels and the old
cells, until some other project was opened or the server restarted.
"""

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def _image(path, n_channels):
    tifffile.imwrite(path, np.zeros((n_channels, 256, 256), dtype=np.uint8))
    return path


def _table(path, marker):
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        marker: np.linspace(0, 3, 4),
    }).write_csv(path)
    return path


def _import(client, name, *paths):
    response = client.post("/import/sample", json={
        "paths": [str(p) for p in paths], "name": name, "answers": {},
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    return Project.load(name)


def test_a_project_imported_under_a_deleted_ones_name_opens_its_own_files(
        client, tmp_path):
    name = "redo_after_delete"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    _import(client, name, _image(first_dir / "v1.ome.tif", 2),
            _table(first_dir / "cells_v1.csv", "CD3"))
    data_model.ensure_loaded(name)
    assert len(data_model.get_channel_names(name)) == 2

    assert client.post(f"/project/{name}/delete").status_code == 200
    assert Project.find(name) is None

    _import(client, name, _image(second_dir / "v2.ome.tif", 5),
            _table(second_dir / "cells_v2.csv", "CD8"))

    data_model.ensure_loaded(name)

    # The image the new record names, not the deleted project's.
    assert "v2.ome.tif" in str(data_model.config[name].get("channelFile")), \
        data_model.config[name].get("channelFile")
    assert len(data_model.get_channel_names(name)) == 5
    # And the new table's cells.
    columns = list(data_model.get_datasource_df().columns)
    assert "CD8" in columns and "CD3" not in columns, columns
