"""The cells on a plane, for the thresholding panel (`/volume/<ds>/plane_cells`).

The panel counts the plane on screen's cells -- the outlines it draws -- by
the gate's calls, and a gate is always applied to each cell's whole-cell value.
So what has to hold, on a real project with a 3-D mask: the route lists exactly
the cells with voxels on the plane (from the measurement's plane index), each
with its value in the loaded table -- the number its gate reads, the same on
every plane; a project measured before the index existed still answers, from
the cells' boxes in depth, and says so; and what cannot be answered says why.
"""

import json
import shutil
import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.volume import objects, planes
from tests import volume_fixtures as vf

DEPTH = 12


@pytest.fixture
def client():
    return plexora.app.test_client()


def _labels():
    labels = np.zeros((DEPTH, 64, 64), dtype=np.uint16)
    labels[1:10, 8:20, 8:20] = 5
    labels[3:8, 8:20, 20:30] = 14          # touching 5
    labels[0:4, 30:40, 30:44] = 9          # cut by the first plane
    labels[1:3, 44:56, 44:56] = 21         # concave in z: planes 1-2 and 8-9
    labels[8:10, 44:56, 44:56] = 21
    return labels


def _project(tmp_path, client, name="pc"):
    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                  depth=DEPTH, height=64, width=64, names=["DNA", "CD8"],
                                  physical_z=1.0)
    labels = _labels()
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    assert client.post(f"/volume/{name}/objects", json={"units": "physical"}).status_code == 202
    deadline = time.time() + 60
    while time.time() < deadline:
        task = client.get(f"/volume/{name}/objects").get_json().get("task") or {}
        if task.get("status") not in (None, "pending", "running"):
            assert task["status"] == "ready", task
            break
        time.sleep(0.05)
    # The table was attached after the project loaded: the page reloads to use it.
    data_model.load_datasource(name, reload=True)
    return labels


def _plane(client, name, z, column="CD8"):
    response = client.get(f"/volume/{name}/plane_cells?z={z}&column={column}")
    assert response.status_code == 200, response.get_json()
    header = json.loads(response.headers["X-Plane-Cells"])
    n = header["n"]
    ids = np.frombuffer(response.data[:4 * n], "<u4")
    values = np.frombuffer(response.data[4 * n:], "<f4")
    return header, ids, values


def _whole(name, column="CD8"):
    data_model.ensure_loaded(name)
    frame = data_model.get_datasource_df()
    return dict(zip(frame["label"].to_list(), data_model.get_all_cells(name, [column]).tolist()))


def test_a_measurement_writes_which_cells_are_on_each_plane(tmp_path, client):
    _project(tmp_path, client)
    table, manifest = objects.object_paths(Project.find("pc"))
    assert planes.index_for(table) is not None
    assert json.loads(manifest.read_text())["planes"]["rows"] > 0


def test_a_plane_lists_exactly_its_cells_with_their_whole_cell_values(tmp_path, client):
    labels = _project(tmp_path, client)
    whole = _whole("pc")
    for z in range(DEPTH):
        header, ids, values = _plane(client, "pc", z)
        assert header["source"] == "index" and header["z"] == z
        assert ids.tolist() == [int(v) for v in np.unique(labels[z]) if v]
        assert values.tolist() == pytest.approx([whole[int(cell)] for cell in ids])
    # The concave cell is not on the planes inside its box it has no voxels on.
    assert 21 not in _plane(client, "pc", 5)[1].tolist()


def test_measured_before_the_index_a_plane_answers_from_the_boxes(tmp_path, client):
    labels = _project(tmp_path, client)
    table, _ = objects.object_paths(Project.find("pc"))
    shutil.rmtree(planes.folder_for(table))
    header, ids, _ = _plane(client, "pc", 5)
    assert header["source"] == "bbox"
    # The box answer counts the concave cell on a plane inside its box.
    assert ids.tolist() == [5, 14, 21]


def test_a_plane_outside_the_stack_or_an_unmeasured_mask_is_refused(tmp_path, client):
    _project(tmp_path, client)
    assert client.get(f"/volume/pc/plane_cells?z={DEPTH}&column=CD8").status_code == 400
    assert client.get("/volume/pc/plane_cells?z=-1&column=CD8").status_code == 400

    path, _ = vf.write_ome_volume(tmp_path / "raw.ome.tif", axes="CZYX", channels=2,
                                  depth=6, height=64, width=64, names=["DNA", "CD8"],
                                  physical_z=1.0)
    register_image_datasource("raw", path)
    refused = client.get("/volume/raw/plane_cells?z=2&column=CD8")
    assert refused.status_code == 400 and refused.get_json()["reason"] == "not_measured"
