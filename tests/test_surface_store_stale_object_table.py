"""Cell surfaces after the 3-D mask is replaced, without measuring again.

The object table (`objects_v1.parquet`) describes the mask it was measured
from, and its manifest names that mask. When the project's mask is replaced
the table still sits in the project's folder; a cell-surface build must not
take its boxes, sizes or voxel numbers for the new mask's cells.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from plexora.server.surface import mesh as mesh_mod

SX, SZ = 0.5, 2.0
#: Wider than one 1024-px tile of the label store, so a build reads tiles by box.
DEPTH, HEIGHT, WIDTH = 8, 64, 1100


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    return license_issuer


def _write_stack(path):
    data = np.full((1, DEPTH, HEIGHT, WIDTH), 100, np.uint16)
    metadata = {"axes": "CZYX", "PhysicalSizeX": SX, "PhysicalSizeY": SX,
                "PhysicalSizeZ": SZ, "Channel": {"Name": ["DNA"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, metadata=metadata)


def _settle(name, layer_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


def _measured_then_replaced(tmp_path, name):
    """A project whose mask was measured (cell 5, small, near x = 0) and then
    replaced without measuring again: cell 5 is now larger and elsewhere, and
    cell 21 is new, in the second 1024-px tile, far from any old box."""
    from plexora.server.volume import objects as volume_objects

    _write_stack(tmp_path / f"{name}.ome.tif")
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")
    old = np.zeros((DEPTH, HEIGHT, WIDTH), np.uint16)
    old[2:5, 8:16, 8:16] = 5
    tifffile.imwrite(tmp_path / f"{name}_mask_a.tif", old)
    datasets.add_layers(name, tmp_path / f"{name}_mask_a.tif", wait=True)
    volume_objects.measure_project_objects(Project.find(name), units="physical", attach=False)

    new = np.zeros((DEPTH, HEIGHT, WIDTH), np.uint16)
    new[1:6, 20:50, 300:340] = 5
    new[1:7, 10:50, 1040:1080] = 21
    tifffile.imwrite(tmp_path / f"{name}_mask_b.tif", new)
    datasets.add_layers(name, tmp_path / f"{name}_mask_b.tif", wait=True)
    table_path, _ = volume_objects.object_paths(Project.find(name))
    assert table_path.exists()          # the old measurement is still in the folder
    return new


def _build(client, name):
    response = client.post(f"/volume/{name}/surfaces",
                           json={"type": "label_surfaces", "smoothing": "none"})
    assert response.status_code == 202, response.get_json()
    layer_id = response.get_json()["layer"]
    assert _settle(name, layer_id)["status"] == "ready", layer_jobs.get(name, layer_id)
    return layer_id


def test_every_cell_of_a_replaced_mask_gets_a_surface(tmp_path, client, surfaces_license):
    """The new mask's cells, wherever they are, are meshed: the old table's
    boxes do not decide which tiles of the new mask are read."""
    name = "stalecellsgeom"
    _measured_then_replaced(tmp_path, name)
    layer_id = _build(client, name)
    surface = mesh_mod.from_bytes(client.get(f"/volume/{name}/surfaces/{layer_id}/mesh").data)
    assert sorted(surface.ids().tolist()) == [5, 21]


def test_a_replaced_masks_surfaces_do_not_carry_the_old_cells_voxel_numbers(
        tmp_path, client, surfaces_license):
    """voxel_count (and the other voxel_* columns) beside a cell's surface are
    that cell's, not those of the old mask's cell that had the same id."""
    name = "stalecellsjoin"
    new = _measured_then_replaced(tmp_path, name)
    layer_id = _build(client, name)
    objects = client.get(f"/volume/{name}/surfaces/{layer_id}/objects?ids=5").get_json()
    row = dict(zip(objects["columns"], objects["rows"][0]))
    assert row["object"] == 5
    assert row.get("voxel_count") in (None, int((new == 5).sum())), row
