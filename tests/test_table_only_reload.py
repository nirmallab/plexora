"""A tool's set-up answer about the table keeps the image open.

Saving a tool's set-up form (`POST /<project>/requirements`) with a column's
role reloads the project, because the table is read differently. It used to
re-open the image too and forget everything measured of it: on the website,
LSP11385's 66 GB OME-TIFF had its 570 pages parsed and its overview read again
(1.1 s of the save's 1.5 s), and the warm-up measured every channel again.
Now such a reload keeps the image open, with its windows, contrast fits,
stats and overview (data_model `keep_image`), and drops what the table feeds,
as any reload does. An image changed on disk, a new data file or a new mask
still reload everything.
"""

import os

import numpy as np
import pytest


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _project(tmp_path, name="tableonly"):
    import tifffile

    from plexora import datasets
    from plexora.datasource import register_image_datasource

    rng = np.random.default_rng(1)
    image = rng.integers(0, 4000, (2, 128, 128), dtype=np.uint16)
    labels = np.zeros((128, 128), dtype=np.uint16)
    rows = []
    for i in range(9):
        y, x = 8 + (i // 3) * 40, 8 + (i % 3) * 40
        labels[y:y + 10, x:x + 10] = i + 1
        rows.append(f"{i + 1},{x + 5},{y + 5},{100 + i},{200 + i}")
    path = tmp_path / f"{name}.ome.tif"
    tifffile.imwrite(path, image, ome=True, metadata={"axes": "CYX", "Channel": {"Name": ["DNA", "CD8"]}})
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    csv = tmp_path / f"{name}_cells.csv"
    csv.write_text("CellID,X_centroid,Y_centroid,DNA,CD8\n" + "\n".join(rows) + "\n")
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    datasets.add_layers(name, csv, wait=True)
    return name, path


def _measured(name):
    """The image measured (a channel's window) and a plugin's table-derived
    entry, as a session would have them before the form is saved."""
    from plexora.server.models import data_model

    data_model.init(name)
    data_model.get_channel_quantization_window("DNA", name)
    data_model.gmm_cache_get_or_set((name, ("a_plugin", 1)), lambda: "from the table")
    assert ("qwindow", name, "DNA") in data_model._gmm_cache
    return data_model.channels, data_model.load_generation


def test_a_role_answer_keeps_the_image_and_what_was_measured_of_it(tmp_path, client):
    from plexora.server.models import data_model

    name, _path = _project(tmp_path)
    channels, generation = _measured(name)

    reply = client.post(f"/{name}/requirements", json={"tool": "roi", "roles": {"cell_id": "CellID"}})

    assert reply.get_json()["reloaded"] is True
    assert data_model.channels is channels  # not opened again
    assert ("qwindow", name, "DNA") in data_model._gmm_cache
    assert (name, ("a_plugin", 1)) not in data_model._gmm_cache  # the table's goes
    assert data_model.load_generation == generation + 1
    assert data_model.get_datasource_description(name) is not None


def test_an_image_changed_on_disk_is_opened_again(tmp_path, client):
    from plexora.server.models import data_model

    name, path = _project(tmp_path)
    channels, _generation = _measured(name)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    client.post(f"/{name}/requirements", json={"tool": "roi", "roles": {"cell_id": "CellID"}})

    assert data_model.channels is not channels
    assert ("qwindow", name, "DNA") not in data_model._gmm_cache


def test_any_other_reload_opens_everything_as_before(tmp_path):
    from plexora.server.models import data_model

    name, _path = _project(tmp_path)
    channels, _generation = _measured(name)

    data_model.load_datasource(name, reload=True)

    assert data_model.channels is not channels
    assert data_model._gmm_cache == {}


def test_only_a_local_image_read_the_same_way_is_kept(tmp_path):
    from plexora.server.models import data_model
    from plexora.server.providers.local import LocalImageProvider

    name, path = _project(tmp_path)
    _measured(name)
    same = LocalImageProvider(str(path))
    assert data_model._image_identity(same) == data_model._image_identity(data_model._providers.image)
    assert data_model._image_identity(LocalImageProvider(str(path), rgb=True)) != \
        data_model._image_identity(same)
    assert data_model._image_identity(object()) is None


def test_the_image_entries_are_told_from_the_tables():
    from plexora.server.models.data_model import _of_the_image

    assert _of_the_image(("qwindow", "p", "DNA"))
    assert _of_the_image(("p", "DNA")) and _of_the_image(("p", "DNA", ("z", 3)))
    assert not _of_the_image(("p", ("gating", "CD8", "sel")))
    assert not _of_the_image("p")
