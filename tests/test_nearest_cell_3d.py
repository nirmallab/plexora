"""The right-click pick on a z-stack keeps to the planes on screen.

A measured 3-D object table holds every cell once, whatever its depth; the
pick used to answer the cell nearest in x/y at any depth, so two cells stacked
in z at one spot read back as whichever came first. The client now sends the
planes on screen (`z0`, `z1`) and the pick keeps to cells whose extent
(`bbox_z0..bbox_z1`) overlaps them.
"""

import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from tests import volume_fixtures as vf

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Paid test licence.
pytestmark = pytest.mark.paid


@pytest.fixture
def client():
    return plexora.app.test_client()


def _measured(tmp_path, client, name="stack"):
    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                  depth=6, height=128, width=128, names=["DNA", "CD8"],
                                  physical_z=2.0)
    labels = np.zeros((6, 128, 128), dtype=np.uint16)
    labels[0:3, 10:20, 10:20] = 5    # two cells stacked in z at one spot
    labels[3:6, 10:20, 10:20] = 9
    labels[0:6, 30:40, 30:40] = 7    # a tall cell ...
    labels[5:6, 44:54, 30:40] = 8    # ... beside one on the last plane only
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    assert client.post(f"/volume/{name}/objects", json={"units": "physical"}).status_code == 202
    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get(f"/volume/{name}/objects").get_json()
        if (state.get("task") or {}).get("status") not in (None, "pending"):
            assert state["task"]["status"] == "ready", state["task"]
            # The table was attached to a project already open (image only).
            data_model.load_datasource(name, reload=True)
            return name
        time.sleep(0.05)
    raise AssertionError("the measurement did not finish")


def _label(found):
    return int(found["label"]) if found else None


def test_the_pick_keeps_to_the_planes_on_screen(tmp_path, client):
    name = _measured(tmp_path, client)
    pick = data_model.query_for_closest_cell
    assert _label(pick(15, 15, name, planes=(1, 1))) == 5
    assert _label(pick(15, 15, name, planes=(4, 4))) == 9
    assert _label(pick(15, 15, name, planes=(0, 1))) == 5
    assert _label(pick(15, 15, name, planes=(4, 5))) == 9
    # The tall cell is on every plane; the one-plane cell only on the last.
    assert _label(pick(35, 48, name, planes=(0, 0))) == 7
    assert _label(pick(35, 48, name, planes=(5, 5))) == 8
    # Far from every cell on the plane: nothing, rather than a cell elsewhere.
    assert pick(110, 110, name, planes=(1, 1)) == {}


def test_without_planes_the_pick_is_the_nearest_cell_as_before(tmp_path, client):
    name = _measured(tmp_path, client)
    assert _label(data_model.query_for_closest_cell(35, 48, name)) == 8
    assert data_model.query_for_closest_cell(110, 110, name) != {}


def test_the_route_reads_the_planes_and_info_names_the_z_column(tmp_path, client):
    name = _measured(tmp_path, client)
    got = client.get(f"/get_nearest_cell?point_x=15&point_y=15&datasource={name}"
                     "&z0=4&z1=4").get_json()
    assert _label(got) == 9
    got = client.get(f"/get_nearest_cell?point_x=15&point_y=15&datasource={name}"
                     "&z0=1&z1=1").get_json()
    assert _label(got) == 5
    info = client.get(f"/volume/{name}/info").get_json()
    assert info["table_z"] == "Z"


def test_a_flat_project_has_no_table_z(tmp_path, client):
    path, _ = vf.write_ome_volume(tmp_path / "flat.ome.tif", axes="CZYX", channels=2,
                                  depth=6, height=32, width=32, names=["DNA", "CD8"],
                                  physical_z=2.0)
    register_image_datasource("flat3", path)
    info = client.get("/volume/flat3/info").get_json()
    assert info.get("table_z") is None
