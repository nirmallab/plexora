"""A surface built before somebody typed the stack's z step is not the surface
of the same request after it: the spacing shapes the build (its units, the
z pooling, the smoothing frame, what min_size means), so the cache key and
the list route's `stale` flag have to follow it, not only the file."""

import time
from dataclasses import replace

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource, reregister_image, set_pixel_size
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from plexora.server.surface import store as surface_store

SX = 0.5
ISO = {"type": "isosurface", "channel": "CD31", "iso": 500, "smoothing": "light",
       "min_size": 50}


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    return license_issuer


def _write_stack_without_z_step(path, depth=16, size=64):
    """Two channels, a bright ball in CD31; the file states x and y, not z."""
    z, y, x = np.mgrid[:depth, :size, :size].astype(np.float32)
    r = np.sqrt((x - size / 2) ** 2 + (y - size / 2) ** 2 + ((z - depth / 2) * 4) ** 2)
    sphere = (1000 * np.clip(1 - r / 32.0, 0, 1)).astype(np.uint16)
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


def test_typing_a_z_step_makes_the_same_request_a_new_surface(tmp_path, client,
                                                              surfaces_license):
    name = "surfzstep"
    _write_stack_without_z_step(tmp_path / f"{name}.ome.tif")
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")
    # A stack that states no z step is read as cycles until somebody says
    # depth (the edit page's z reading, project_routes._apply_edit).
    Project.mutate(name, lambda current: current.patch(
        image=replace(current.image, z_semantics="depth")))
    reregister_image(name)
    assert Project.find(name).image.depth == 16

    first = client.post(f"/volume/{name}/surfaces", json=ISO)
    assert first.status_code == 202, first.get_json()
    old_id = first.get_json()["layer"]
    assert _settle(name, old_id)["status"] == "ready", layer_jobs.get(name, old_id)
    manifest = client.get(f"/volume/{name}/surfaces/{old_id}/manifest").get_json()
    # Built with no z step: counted in voxels.
    assert manifest["spacing"]["units"] == "voxel"
    old_key = Project.find(name).layer(old_id).extra["cacheKey"]

    # Somebody types the z step (the Depth card / set_z_spacing).
    set_pixel_size(name, 2.0, axis="z")
    project = Project.find(name)
    assert project.image.pixel_size_z and project.image.pixel_size_z["value"] == 2.0

    # The surface on record was built under the old spacing: it is stale ...
    entry = next(s for s in client.get(f"/volume/{name}/surfaces").get_json()["surfaces"]
                 if s["id"] == old_id)
    assert entry["stale"] is True, (
        "the list route calls the voxel-unit surface current after the z step was typed")

    # ... and the same request is a different surface, not the old one handed back.
    assert surface_store.cache_key(project, surface_store.SurfaceSpec.from_dict(ISO)) \
        != old_key, "the cache key ignores the typed z step"
    again = client.post(f"/volume/{name}/surfaces", json=ISO)
    body = again.get_json()
    assert body["layer"] != old_id, (
        f"the same request returned the surface built without a z step: {body}")
    assert again.status_code == 202 and body["created"] is True
    new_id = body["layer"]
    assert _settle(name, new_id)["status"] == "ready", layer_jobs.get(name, new_id)
    rebuilt = client.get(f"/volume/{name}/surfaces/{new_id}/manifest").get_json()
    assert rebuilt["spacing"]["units"] == "um"
    assert rebuilt["spacing"]["scale"] == pytest.approx([SX, SX, 2.0])
