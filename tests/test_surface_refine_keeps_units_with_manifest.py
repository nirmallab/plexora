"""An edit re-measures a surface's objects table; the manifest has to say the
units that table is in. A surface built before the stack's z step was known
is counted in voxels; when somebody types the z step and then edits the
surface (remove_small, say), the objects table and the manifest must still
agree, or the centroid neighbours look for columns that are not there and the
answer is a 500."""

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource, reregister_image, set_pixel_size
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from plexora.server.surface import store as surface_store

SX = 0.5
ISO = {"type": "isosurface", "channel": "CD31", "iso": 500, "smoothing": "light"}


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    return license_issuer


def _write_stack_without_z_step(path, depth=16, size=64):
    """Two channels, two bright balls in CD31; the file states x and y, not z."""
    z, y, x = np.mgrid[:depth, :size, :size].astype(np.float32)
    big = np.sqrt((x - 20) ** 2 + (y - 20) ** 2 + ((z - depth / 2) * 2) ** 2)
    small = np.sqrt((x - 48) ** 2 + (y - 48) ** 2 + ((z - depth / 2) * 2) ** 2)
    ball = np.maximum(1000 * np.clip(1 - big / 20.0, 0, 1), 1000 * np.clip(1 - small / 8.0, 0, 1))
    sphere = ball.astype(np.uint16)
    data = np.stack([np.full_like(sphere, 100), sphere])
    metadata = {"axes": "CZYX", "PhysicalSizeX": SX, "PhysicalSizeY": SX,
                "Channel": {"Name": ["DNA", "CD31"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, metadata=metadata)


def _settle(name, layer_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


def test_an_edit_after_the_z_step_is_typed_keeps_manifest_and_objects_in_one_unit(
        tmp_path, client, surfaces_license):
    name = "surfrefineunits"
    _write_stack_without_z_step(tmp_path / f"{name}.ome.tif")
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")
    # A stack that states no z step is read as cycles until somebody says
    # depth (the edit page's z reading, project_routes._apply_edit).
    Project.mutate(name, lambda current: current.patch(
        image=replace(current.image, z_semantics="depth")))
    reregister_image(name)
    assert Project.find(name).image.depth == 16

    made = client.post(f"/volume/{name}/surfaces", json=ISO)
    assert made.status_code == 202, made.get_json()
    layer_id = made.get_json()["layer"]
    assert _settle(name, layer_id)["status"] == "ready", layer_jobs.get(name, layer_id)
    base = f"/volume/{name}/surfaces/{layer_id}"
    # Built with no z step: counted in voxels.
    assert client.get(f"{base}/manifest").get_json()["spacing"]["units"] == "voxel"

    # Somebody types the z step (the Depth card / set_z_spacing), then edits
    # the surface.
    set_pixel_size(name, 2.0, axis="z")
    edited = client.post(f"{base}/refine", json={"op": "remove_small", "min_size": 1})
    assert edited.status_code == 200, edited.get_json()

    layer = Project.find(name).layer(layer_id)
    frame = pl.read_parquet(Path(layer.src) / surface_store.OBJECTS)

    # The centroid neighbours find the columns they read ...
    target = int(frame["object"][0])
    near = client.get(f"{base}/neighbours?mode=centroid&object={target}&within=1000")
    assert near.status_code == 200, near.data[:400]

    # ... because the manifest and the objects table say the same units.
    manifest = client.get(f"{base}/manifest").get_json()
    unit = "um" if manifest["spacing"]["units"] == "um" else "vox"
    measured = [c for c in frame.columns if c.startswith(("centroid_x_", "mesh_volume_"))]
    assert f"centroid_x_{unit}" in frame.columns and f"mesh_volume_{unit}3" in frame.columns, (
        f"the manifest says {manifest['spacing']['units']!r} but objects.parquet holds {measured}")
    assert near.get_json()["units"] == ("um" if unit == "um" else "voxel")
