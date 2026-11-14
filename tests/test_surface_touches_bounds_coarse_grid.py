"""An isosurface cut by the edge of what was extracted says it touches the bounds.

A structure the extraction cuts -- at the volume's edge or at the edge of a
region box -- is capped flat and must come out `touches_bounds` True, so the
manifest's `touching_bounds` counts it and nobody takes its volume for a whole
object's. That has to hold at every level the planner may pick, not only at
level 0: at a coarse level the cap sits on the coarse grid's outermost voxel
centres, which are more than half a level-0 voxel inside the edge.
"""

import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import layer_jobs
from plexora.server.surface import isosurface, marching
from plexora.server.surface import mesh as mesh_mod

SX, SZ = 0.5, 2.0
DEPTH, SIZE = 8, 64


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    return license_issuer


def _write_pyramid(path, bright, levels=3):
    """Two channels (DNA flat, CD31 = `bright`), pyramidal OME-TIFF, 2x per level."""
    flat = np.full_like(bright, 100)
    data = np.stack([flat, bright])
    metadata = {"axes": "CZYX", "PhysicalSizeX": SX, "PhysicalSizeY": SX,
                "PhysicalSizeZ": SZ, "Channel": {"Name": ["DNA", "CD31"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, subifds=levels - 1, metadata=metadata)
        for level in range(1, levels):
            step = 2 ** level
            writer.write(data[..., ::step, ::step], subfiletype=1)


def _settle(name, layer_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


def _build(client, name, body):
    response = client.post(f"/volume/{name}/surfaces", json=body)
    assert response.status_code == 202, response.get_json()
    layer_id = response.get_json()["layer"]
    job = _settle(name, layer_id)
    assert job["status"] == "ready", job
    manifest = client.get(f"/volume/{name}/surfaces/{layer_id}/manifest").get_json()
    objects = client.get(f"/volume/{name}/surfaces/{layer_id}/objects").get_json()
    rows = [dict(zip(objects["columns"], row)) for row in objects["rows"]]
    return manifest, rows


def test_a_slab_cut_by_the_volume_edge_is_flagged_at_a_4x_level(tmp_path, client,
                                                                 surfaces_license):
    """Level 2 (4x) of a stack whose bright slab runs into x = 0: the one
    object is cut by the volume's edge and must be counted as touching it."""
    bright = np.zeros((DEPTH, SIZE, SIZE), np.uint16)
    bright[2:6, 24:40, 0:16] = 1000          # runs into the x = 0 edge
    name = "touchcoarse"
    _write_pyramid(tmp_path / f"{name}.ome.tif", bright)
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")

    manifest, rows = _build(client, name, {"type": "isosurface", "channel": "CD31",
                                           "iso": 500, "smoothing": "none", "level": 2})
    assert manifest["build"]["plan"]["level"] == 2
    assert manifest["build"]["plan"]["grid"]["scale"][0] == pytest.approx(4.0)
    assert len(rows) == 1
    row = rows[0]
    assert row["closed"] is True
    # The cap is on the coarse grid's first voxel centre: 1.5 level-0 voxels in.
    assert row["bbox_x0"] == pytest.approx(1.5)
    assert row["touches_bounds"] is True, row
    assert manifest["objects"]["touching_bounds"] == 1


def test_an_object_cut_by_a_region_box_is_flagged(tmp_path, client, surfaces_license):
    """At level 0, a region box whose left edge runs through a bright block:
    the block is cut by the box and must be counted as touching the bounds."""
    bright = np.zeros((DEPTH, SIZE, SIZE), np.uint16)
    bright[2:6, 24:40, 24:40] = 1000         # well inside the volume
    name = "touchregion"
    _write_pyramid(tmp_path / f"{name}.ome.tif", bright, levels=1)
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")

    manifest, rows = _build(client, name, {"type": "isosurface", "channel": "CD31",
                                           "iso": 500, "smoothing": "none", "level": 0,
                                           "region": {"box": [32, 0, 32, 64]}})
    assert manifest["build"]["plan"]["level"] == 0
    assert len(rows) == 1
    row = rows[0]
    assert row["closed"] is True
    # Capped on the region's first column, x = 32: the block is cut there.
    assert row["bbox_x0"] == pytest.approx(32.0)
    assert row["touches_bounds"] is True, row
    assert manifest["objects"]["touching_bounds"] == 1


@pytest.mark.parametrize("factor", [4, 8])
def test_measure_flags_an_edge_cut_isosurface_on_a_coarse_grid(factor):
    """The engine alone, as build_layer measures it: an isosurface marched on
    a grid `factor` times coarser than level 0, of a slab that runs into x = 0,
    measured against the whole volume's dims."""
    width0 = 128
    grid_w = width0 // factor
    vol = np.zeros((6, grid_w, grid_w), np.float32)
    vol[2:4, grid_w // 4: grid_w // 2, 0:grid_w // 4] = 100   # clear of every edge but x = 0
    grid = marching.Grid.of(vol.shape, factor_xy=(factor, factor))
    surface, _ = isosurface.extract(lambda a, b: vol[a:b + 1], grid, 50.0, scale=(1, 1, 1))
    columns = mesh_mod.measure(surface, dims=(width0, width0, 6))
    assert surface.n_objects == 1
    assert bool(columns["closed"][0])
    assert bool(columns["touches_bounds"][0]), float(columns["bbox_x0"][0])
