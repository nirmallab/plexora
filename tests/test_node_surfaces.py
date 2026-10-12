"""Surface Studio on a z-stack a data node holds: built there, used here.

A surface is extracted from a whole channel or a whole label volume, so for a
stack on another machine the extraction runs on that machine's node
(`surface.store.extract_on_node`, the code a local build runs) and only the
mesh crosses; the primary measures it, joins its object table and writes the
surface. Every test here builds the same surface from the same files twice --
once on this machine, once through a node -- and asks for the same answer.
"""

from __future__ import annotations

import time

import numpy as np
import polars as pl
import pytest
import tifffile

from tests.node_harness import node_process, register  # noqa: F401 - fixture

SIZE, DEPTH = 256, 8
SX, SZ = 0.5, 1.5


def _write_stack(path):
    """Two channels at 256 px with a second level (a node's default reading
    wants a level of at least 200 px): DNA flat, CD31 a bright ball."""
    z, y, x = np.mgrid[:DEPTH, :SIZE, :SIZE].astype(np.float32)
    r = np.sqrt(((x - 128) * SX) ** 2 + ((y - 128) * SX) ** 2 + ((z - DEPTH / 2) * SZ) ** 2)
    ball = (1000 * np.clip(1 - r / 16.0, 0, 1)).astype(np.uint16)
    data = np.stack([np.full_like(ball, 100), ball])
    metadata = {"axes": "CZYX", "PhysicalSizeX": SX, "PhysicalSizeY": SX,
                "PhysicalSizeZ": SZ, "Channel": {"Name": ["DNA", "CD31"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, subifds=1, metadata=metadata)
        writer.write(data[..., ::2, ::2], subfiletype=1)
    return path


def _write_mask(path):
    labels = np.zeros((DEPTH, SIZE, SIZE), np.uint16)
    labels[1:5, 40:70, 40:72] = 5
    labels[3:8, 100:140, 90:120] = 9
    labels[3:8, 140:170, 90:120] = 11          # touching 9 along y = 140
    labels[0:2, 200:230, 200:240] = 12         # cut by the first plane
    tifffile.imwrite(path, labels, photometric="minisblack")
    return path


def _settle(name, layer_id, timeout=120):
    from plexora.server.models import layer_jobs

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


def _node_status(node, resource_id, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        status = node.get(f"/node/v1/resources/{resource_id}/status").get("resource") or {}
        if status.get("state") in ("ready", "error") or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


def _local(name, image, mask=None):
    from plexora.datasource import register_image_datasource
    from plexora.server.routes.import_routes import attach_segmentation

    register_image_datasource(name, image)
    if mask is not None:
        attach_segmentation(name, str(mask))
        _wait_for_mask(name)


def _on_node(name, image, node_name, mask_id=None):
    """`image` registered here (recording its depth reading), then moved onto
    the node, and the node's mask attached -- the way a project comes to be
    read remotely."""
    from plexora import nodes
    from plexora.datasource import register_image_datasource

    register_image_datasource(name, image)
    nodes.attach_image(name, node=node_name, resource_id="img", channel_names=["DNA", "CD31"])
    if mask_id is not None:
        nodes.attach_segmentation(name, node=node_name, resource_id=mask_id)


def _wait_for_mask(name, timeout=60):
    from plexora.server.models import data_model

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = data_model.get_segmentation_job_status(name)
        if status.get("status") in ("ready", "error"):
            assert status["status"] == "ready", status
            return
        time.sleep(0.1)
    raise AssertionError(f"mask job for {name} did not finish")


def _build(name, spec):
    from plexora.server.models.project import Project
    from plexora.server.surface import store

    answer = store.create(Project.find(name), dict(spec))
    job = _settle(name, answer["layer"])
    assert job["status"] == "ready", job
    layer = Project.find(name).layer(answer["layer"])
    surface, manifest = store.current_mesh(layer)
    frame = pl.read_parquet(f"{layer.src}/{store.OBJECTS}")
    return surface, manifest, frame


def _same_mesh(a, b):
    assert a.n_triangles == b.n_triangles and a.n_triangles > 0
    np.testing.assert_array_equal(a.triangles, b.triangles)
    np.testing.assert_array_equal(a.object_id, b.object_id)
    np.testing.assert_array_equal(a.objects, b.objects)
    np.testing.assert_allclose(a.positions, b.positions, rtol=0, atol=1e-5)


CELLS = {"type": "label_surfaces", "smoothing": "medium"}
ISO = {"type": "isosurface", "channel": "CD31", "iso": 500, "smoothing": "light"}


# -- cell surfaces ----------------------------------------------------------------


def test_cell_surfaces_of_a_node_mask_are_built_there_and_match_local(tmp_path, node_process):
    image = _write_stack(tmp_path / "v.ome.tif")
    mask = _write_mask(tmp_path / "mask.tif")
    _local("here", image, mask)
    here, here_manifest, here_frame = _build("here", CELLS)

    node = node_process(f"segmentation:cells={mask}")
    register("o2", node)
    assert _node_status(node, "cells")["state"] == "ready"
    from plexora import nodes
    from plexora.datasource import register_image_datasource

    register_image_datasource("there", image)
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    there, manifest, frame = _build("there", CELLS)

    _same_mesh(here, there)
    assert sorted(there.ids().tolist()) == [5, 9, 11, 12]
    assert frame.equals(here_frame)
    assert manifest["source"]["labels"] == "node://o2/cells"
    assert manifest["source"]["built_on"] == "node:o2"
    assert manifest["spacing"]["scale"] == pytest.approx(here_manifest["spacing"]["scale"])
    assert manifest["lods"][-1]["triangles"] == here_manifest["lods"][-1]["triangles"]


def test_the_object_table_is_sent_to_the_node_and_joined_here(tmp_path, node_process):
    """With the stack and mask both on the node, the objects are measured
    there; a surface build then sizes the cells from that table (sent along)
    and the voxel columns are joined into the surface's own table here."""
    from plexora.server.models.project import Project
    from plexora.server.volume import objects

    image = _write_stack(tmp_path / "v.ome.tif")
    mask = _write_mask(tmp_path / "mask.tif")
    _local("here", image, mask)
    objects.measure_project_objects(Project.load("here"), units="voxel")
    here, _m, here_frame = _build("here", CELLS)

    node = node_process(f"image:img={image}", f"segmentation:cells={mask}")
    register("o2", node)
    _node_status(node, "cells")
    _on_node("there", image, "o2", mask_id="cells")
    objects.measure_project_objects(Project.load("there"), units="voxel")
    there, _manifest, frame = _build("there", CELLS)

    _same_mesh(here, there)
    assert "voxel_count" in frame.columns
    assert frame.sort("object")["voxel_count"].to_list() == \
        here_frame.sort("object")["voxel_count"].to_list()


# -- isosurfaces ------------------------------------------------------------------


def test_an_isosurface_of_a_node_image_is_built_there_and_matches_local(tmp_path,
                                                                       node_process):
    image = _write_stack(tmp_path / "v.ome.tif")
    _local("here", image)
    here, here_manifest, _f = _build("here", ISO)

    node = node_process(f"image:img={image}")
    register("o2", node)
    _on_node("there", image, "o2")
    there, manifest, _frame = _build("there", ISO)

    _same_mesh(here, there)
    assert manifest["source"]["image"] == "node://o2/img"
    assert manifest["build"]["plan"] == here_manifest["build"]["plan"]


def test_a_preview_reads_a_node_image_through_the_node(tmp_path, node_process, license_issuer):
    import plexora
    from plexora.server.models import data_model

    license_issuer.install(license_issuer.issue(ent=["surfaces"]))
    image = _write_stack(tmp_path / "v.ome.tif")
    _local("here", image)
    node = node_process(f"image:img={image}")
    register("o2", node)
    _on_node("there", image, "o2")
    client = plexora.app.test_client()
    answers = {}
    for name in ("here", "there"):
        data_model.load_datasource(name, reload=True)
        response = client.get(f"/volume/{name}/surfaces/preview?channel=CD31&iso=500")
        assert response.status_code == 200, response.get_json()
        answers[name] = response
    assert answers["there"].data == answers["here"].data
    suggest = client.get("/volume/there/surfaces/suggest?channel=CD31")
    assert suggest.status_code == 200, suggest.get_json()


# -- after the build ----------------------------------------------------------------


def test_contact_areas_read_a_node_mask(tmp_path, node_process, license_issuer):
    import plexora
    from plexora.server.models.project import Project
    from plexora.server.volume import objects

    license_issuer.install(license_issuer.issue(ent=["surfaces"]))
    image = _write_stack(tmp_path / "v.ome.tif")
    mask = _write_mask(tmp_path / "mask.tif")
    node = node_process(f"image:img={image}", f"segmentation:cells={mask}")
    register("o2", node)
    _node_status(node, "cells")
    _on_node("there", image, "o2", mask_id="cells")
    objects.measure_project_objects(Project.load("there"), units="voxel")
    from plexora.server.surface import store

    answer = store.create(Project.find("there"), dict(CELLS))
    assert _settle("there", answer["layer"])["status"] == "ready"
    client = plexora.app.test_client()
    body = client.get(f"/volume/there/surfaces/{answer['layer']}/neighbours"
                      f"?object=9&within=10").get_json()
    near = {row["object"]: row for row in body["neighbours"]}
    assert 11 in near and near[11]["contact_area"] > 0, body
    assert body["contact"]


def test_a_cancelled_node_build_stops_the_node_job(tmp_path, node_process, monkeypatch):
    """Cancelling the layer cancels the node's job, which says so."""
    from plexora.server.models.project import Project
    from plexora.server.providers import node as node_provider
    from plexora.server.surface import store

    image = _write_stack(tmp_path / "v.ome.tif")
    mask = _write_mask(tmp_path / "mask.tif")
    node = node_process(f"segmentation:cells={mask}")
    register("o2", node)
    _node_status(node, "cells")
    from plexora import nodes
    from plexora.datasource import register_image_datasource

    register_image_datasource("there", image)
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    real = node_provider.build_surface_on_node

    def cancelled_at_once(binding, body, **kwargs):
        kwargs["cancelled"] = lambda: True
        return real(binding, body, **kwargs)

    monkeypatch.setattr(node_provider, "build_surface_on_node", cancelled_at_once)
    answer = store.create(Project.find("there"), dict(CELLS))
    job = _settle("there", answer["layer"])
    assert job["status"] == "failed"
