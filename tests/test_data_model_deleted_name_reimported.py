"""A project re-created under a deleted one's name opens its own data.

The server keeps one project loaded and decides "already loaded" on its name.
When that project was deleted and a different image and table imported under
the same name, the viewer's boot (`/init_database`) found the name already
loaded and went on serving the deleted project's channels and cells: nothing
reloaded and `load_generation` -- which keys the tile cache and its ETags --
stayed where it was.
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


def _table(path, n_cells):
    pl.DataFrame({
        "CellID": np.arange(1, n_cells + 1, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, n_cells),
        "Y_centroid": np.linspace(10, 200, n_cells),
        "CD45": np.linspace(0, 3, n_cells),
    }).write_csv(path)
    return path


def _import(client, name, *paths):
    response = client.post("/import/sample", json={
        "paths": [str(p) for p in paths], "name": name, "answers": {},
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    assert Project.find(name) is not None


def test_viewer_boot_after_delete_and_reimport_serves_the_new_project(client, tmp_path):
    name = "audit_recreated_sample"
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()

    _import(client, name, _image(old_dir / "old.ome.tif", 2), _table(old_dir / "old.csv", 4))
    assert client.get(f"/init_database?datasource={name}").status_code == 200
    assert data_model.get_datasource_df().height == 4
    assert len(data_model.get_channel_names(name)) == 2
    generation = data_model.load_generation

    assert client.post(f"/project/{name}/delete").status_code == 200
    assert Project.find(name) is None
    _import(client, name, _image(new_dir / "new.ome.tif", 3), _table(new_dir / "new.csv", 9))

    # The viewer opening the re-created project. Its page goes on whatever this
    # answers (services/dataLayer.js does not read the status), so what counts
    # is what the server holds afterwards.
    booted = client.get(f"/init_database?datasource={name}")

    assert data_model.get_datasource_df().height == 9, \
        "the deleted project's cell table is still being served"
    assert len(data_model.get_channel_names(name)) == 3, \
        "the deleted project's channels are still being served"
    assert data_model.load_generation > generation, \
        "tiles cached for the deleted project would still be served"
    assert booted.status_code == 200, booted.get_data(as_text=True)
