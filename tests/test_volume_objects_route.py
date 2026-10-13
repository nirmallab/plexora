"""Measuring a 3-D mask's cells from the viewer: the Depth card's routes.

The same measurement the agent's `measure_objects` runs, started from the
page: refused with a reason when there is nothing whole to measure or no z
step for µm, run in the background otherwise, and the table attached with
its own roles already confirmed.
"""

import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from tests import volume_fixtures as vf

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Paid test licence.
pytestmark = pytest.mark.paid


@pytest.fixture
def client():
    return plexora.app.test_client()


def _project(tmp_path, name="obj", physical_z=2.0, mask3d=True):
    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                  depth=6, height=64, width=64, names=["DNA", "CD8"],
                                  physical_z=physical_z)
    if mask3d:
        labels = np.zeros((6, 64, 64), dtype=np.uint16)
        labels[1:4, 10:20, 10:20] = 5
        labels[3:6, 30:40, 30:46] = 9
    else:
        labels = np.zeros((64, 64), dtype=np.uint16)
        labels[10:20, 10:20] = 5
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)


def _settle(client, name, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/volume/{name}/objects").get_json()
        if (state.get("task") or {}).get("status") not in (None, "pending"):
            return state
        time.sleep(0.05)
    raise AssertionError("the measurement did not finish")


def test_a_3d_mask_is_measured_and_attached_from_the_page(tmp_path, client):
    _project(tmp_path)
    before = client.get("/volume/obj/objects").get_json()
    assert before["label_volume"] is True and before["z_step_known"] is True
    assert before["measured"] is None

    started = client.post("/volume/obj/objects", json={"units": "physical"})
    assert started.status_code == 202
    state = _settle(client, "obj")
    assert state["task"]["status"] == "ready", state["task"]
    assert state["result"]["objects"] == 2 and state["result"]["attached"] is True
    assert state["measured"]["objects"] == 2

    project = Project.find("obj")
    assert project.dataset.roles.cell_id == "label" and project.dataset.roles.z == "Z"
    # The roles are the measurement's own columns: not asked again.
    assert {"table", "role:cell_id", "role:x", "role:y", "role:z"} <= set(project.confirmed)


def test_a_flat_mask_has_nothing_whole_to_measure(tmp_path, client):
    _project(tmp_path, name="flat", mask3d=False)
    assert client.get("/volume/flat/objects").get_json()["label_volume"] is False
    refused = client.post("/volume/flat/objects", json={"units": "voxel"})
    assert refused.status_code == 400 and "3-D label volume" in refused.get_json()["error"]


def test_without_a_z_step_microns_are_refused_and_voxels_are_not(tmp_path, client):
    _project(tmp_path, name="noz", physical_z=None)
    # Two channels and no z step would import as cycles; read it as depth.
    assert client.post("/project/noz", json={"zSemantics": "depth"}).status_code == 200
    state = client.get("/volume/noz/objects").get_json()
    assert state["z_step_known"] is False
    refused = client.post("/volume/noz/objects", json={"units": "physical"})
    assert refused.status_code == 400 and refused.get_json()["missing"] == "spacing:z"
    assert client.post("/volume/noz/objects", json={"units": "voxel"}).status_code == 202
    state = _settle(client, "noz")
    assert state["task"]["status"] == "ready", state["task"]
    assert state["result"]["units"] == "voxel"
    layer_jobs.forget("noz")
