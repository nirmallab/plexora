"""A 3-D mask on a data node, read plane by plane like a local one.

A label volume -- one id per cell across every plane it spans -- is what a
z-stack segmented in 3-D produces, and the viewer's own machine has stored and
served one for a while (volume/labels.py). A data node did not: it pushed every
mask through the 2-D pyramid, so a (Z, Y, X) TIFF or an Imaris mask came out as
its middle plane, and the node's tile and region routes took no plane at all.

Now the node stores a 3-D mask as a label volume by the same rule
(`volume_labels.resolve_label_volume`), says so in its resource status, and
serves the plane a `view` names; the primary forwards the view, reads blocks
through `NodeLabelVolume`, and reports `ndim: 3` -- so every assertion here is
one the local test (tests/test_volume_labels.py) makes against a local mask.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pytest
import tifffile

from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.test_ims_masks import write_ims

DEPTH, SIZE = 6, 64


def _labels():
    volume = np.zeros((DEPTH, SIZE, SIZE), dtype=np.uint16)
    volume[1:4, 10:20, 10:20] = 5
    volume[3:6, 30:40, 30:40] = 9
    volume[0:2, 40:60, 5:25] = 12
    return volume


def _decode(payload):
    from PIL import Image

    rgba = np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA")).astype(np.uint32)
    return rgba[..., 0] + (rgba[..., 1] << 8) + (rgba[..., 2] << 16)


def _get_bytes(node, path):
    return node.get(path, raw=True)


def _post_array(node, path, body):
    from plexora.server.providers import wire

    array, _meta = wire.unpack_array(node.post(path, body, raw=True))
    return array


def _status(node, resource_id, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        status = node.get(f"/node/v1/resources/{resource_id}/status").get("resource") or {}
        if status.get("state") == "ready" or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


@pytest.fixture(params=["tif", "ims"])
def mask_file(request, tmp_path):
    """The same 3-D mask as a TIFF stack and as an Imaris file (float32, the
    way Imaris's converter stores 32-bit ids)."""
    labels = _labels()
    if request.param == "tif":
        path = tmp_path / "mask.tif"
        tifffile.imwrite(path, labels)
    else:
        path = write_ims(tmp_path / "mask.ims", [("cells", labels.astype(np.float32))])
    return path, labels


# -- the node --------------------------------------------------------------------


def test_the_node_stores_a_3d_mask_as_a_label_volume(mask_file, node_process):
    path, labels = mask_file
    node = node_process(f"segmentation:cells={path}")

    status = _status(node, "cells")
    assert status["state"] == "ready", status
    assert status["mask_mode"] == "filled"
    facts = status["label_volume"]
    assert (facts["depth"], facts["height"], facts["width"]) == (DEPTH, SIZE, SIZE)
    assert facts["frames"] == 1 and facts["level_shapes"][0] == [SIZE, SIZE]


def test_each_plane_is_served_by_its_view(mask_file, node_process):
    path, labels = mask_file
    node = node_process(f"segmentation:cells={path}")
    _status(node, "cells")

    tiles = {}
    for z in (0, 2, 4):
        ids = _decode(_get_bytes(node, f"/node/v1/seg/cells/tile/0/0_0.png?view=z{z}"))
        assert np.array_equal(ids[:SIZE, :SIZE], labels[z]), z
        tiles[z] = ids
    # The node's tile cache is keyed by the view: a second plane is not the
    # first plane's tile handed back again.
    assert not np.array_equal(tiles[0], tiles[4])
    # No view is the middle plane, as a local mask's default is.
    ids = _decode(_get_bytes(node, "/node/v1/seg/cells/tile/0/0_0.png"))
    assert np.array_equal(ids[:SIZE, :SIZE], labels[DEPTH // 2])


def test_regions_and_blocks_read_the_planes_asked_for(mask_file, node_process):
    path, labels = mask_file
    node = node_process(f"segmentation:cells={path}")
    _status(node, "cells")

    region = _post_array(node, "/node/v1/seg/cells/region",
                         {"level": 0, "box": [0, 0, SIZE, SIZE], "z": 1})
    assert np.array_equal(region, labels[1])
    block = _post_array(node, "/node/v1/seg/cells/block",
                        {"level": 0, "z0": 1, "z1": 4, "rows": [5, 45], "cols": [0, 50]})
    assert np.array_equal(block, labels[1:5, 5:45, 0:50])


def test_a_2d_mask_on_a_node_is_unchanged(tmp_path, node_process):
    flat = np.zeros((SIZE, SIZE), np.uint32)
    flat[10:30, 10:30] = 4
    tifffile.imwrite(tmp_path / "flat.tif", flat)
    node = node_process(f"segmentation:flat={tmp_path / 'flat.tif'}")

    status = _status(node, "flat")
    assert "label_volume" not in status
    a = _get_bytes(node, "/node/v1/seg/flat/tile/0/0_0.png?view=z1")
    b = _get_bytes(node, "/node/v1/seg/flat/tile/0/0_0.png?view=z4")
    assert a == b and np.array_equal(_decode(a)[:SIZE, :SIZE], flat)


def test_a_3d_mask_shared_while_the_node_runs_converts_too(mask_file, node_process):
    from plexora import nodes

    path, labels = mask_file
    node = node_process(dynamic=True)
    register("o2", node)
    shared = nodes.share_path("o2", "segmentation", str(path))
    resource_id = shared.rsplit("/", 1)[-1] if isinstance(shared, str) else shared["id"]

    status = _status(node, resource_id)
    assert status["state"] == "ready", status
    assert status["label_volume"]["depth"] == DEPTH
    ids = _decode(_get_bytes(node, f"/node/v1/seg/{resource_id}/tile/0/0_0.png?view=z3"))
    assert np.array_equal(ids[:SIZE, :SIZE], labels[3])


# -- the primary -----------------------------------------------------------------


@pytest.fixture
def project_on_a_node(tmp_path, mask_file, node_process):
    """A local z-stack whose 3-D mask is held by a data node."""
    from plexora import nodes
    from plexora.datasource import register_datasource
    from tests import volume_fixtures as vf

    path, labels = mask_file
    image, _ = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=2,
                                   depth=DEPTH, height=SIZE, width=SIZE,
                                   names=["DNA", "CD3"])
    table = tmp_path / "cells.csv"
    table.write_text("CellID,X,Y,DNA,CD3\n5,15,15,10,1\n9,35,35,12,3\n", encoding="utf-8")
    entry = register_datasource("v3", image, table, x="X", y="Y", id_column="CellID")
    node = node_process(f"segmentation:cells={path}")
    register("o2", node)
    _status(node, "cells")
    nodes.attach_segmentation("v3", node="o2", resource_id="cells")
    return entry, labels


def test_the_primary_reports_a_node_mask_as_3d(project_on_a_node):
    import plexora

    client = plexora.app.test_client()
    info = client.get("/volume/v3/info").get_json()
    assert info.get("segmentation") == {"ndim": 3}, info


def test_the_primary_forwards_the_plane_to_the_node(project_on_a_node):
    import plexora
    from plexora.server.models.project import Project

    _entry, labels = project_on_a_node
    client = plexora.app.test_client()
    src = Project.find("v3").to_entry()["imageData"][0]["src"]
    key = src.rstrip("/").rsplit("/", 1)[-1]
    for z in (0, 2, 5):
        response = client.get(f"/generated/data/v3/{key}/0/0_0.png?view=z{z}")
        assert response.status_code == 200, response.data[:200]
        assert np.array_equal(_decode(response.data)[:SIZE, :SIZE], labels[z]), z


def test_the_dataset_api_reads_a_node_mask_in_depth(project_on_a_node):
    from plexora import api

    _entry, labels = project_on_a_node
    volume = api.project_data("v3").segmentation.label_volume()
    assert volume is not None
    assert (volume.depth, volume.height, volume.width) == (DEPTH, SIZE, SIZE)
    block = volume.read(0, 0, DEPTH - 1, slice(0, SIZE), slice(0, SIZE))
    assert np.array_equal(block, labels)
    # A plane, through the 2-D face every label reader takes.
    plane = np.asarray(volume.plane(4)["0"][0:SIZE, 0:SIZE])
    assert np.array_equal(plane, labels[4])


# -- measured where the pixels are -----------------------------------------------

#: A stack big enough for a node to open (its default reading needs a level of
#: at least 200 px; see tests/test_volume_backend.py's `volume_node`).
BIG, BIG_DEPTH = 256, 4


def _stack_and_mask(tmp_path):
    from tests import volume_fixtures as vf

    image, _ = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=2,
                                   depth=BIG_DEPTH, height=BIG, width=BIG, levels=2,
                                   names=["DNA", "CD3"])
    labels = np.zeros((BIG_DEPTH, BIG, BIG), dtype=np.uint16)
    labels[0:3, 20:60, 20:60] = 3
    labels[1:4, 100:150, 80:140] = 8
    labels[2:4, 180:240, 170:230] = 21
    mask = tmp_path / "mask.tif"
    tifffile.imwrite(mask, labels, photometric="minisblack")
    return image, mask


def _on_a_node(name, image, node_name):
    """`image` registered here (which records its depth reading), then moved
    onto the node -- the ordinary way a project comes to read it remotely."""
    from plexora import nodes
    from plexora.datasource import register_image_datasource

    register_image_datasource(name, image)
    nodes.attach_image(name, node=node_name, resource_id="img",
                       channel_names=["DNA", "CD3"])


def test_objects_are_measured_on_the_node_as_they_are_locally(tmp_path, node_process):
    """A z-stack and its 3-D mask both on a node: the objects are measured on
    the node (volume/objects.measure_columns, run there) and the table that
    comes back is the table a local measurement of the same files writes."""
    import polars as pl

    from plexora import nodes
    from plexora.datasource import register_image_datasource
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import attach_segmentation
    from plexora.server.volume import objects

    image, mask = _stack_and_mask(tmp_path)
    register_image_datasource("here", image)
    attach_segmentation("here", str(mask))
    _wait_for_local_mask("here")
    local = objects.measure_project_objects(Project.load("here"), units="voxel",
                                            attach=False)

    node = node_process(f"image:img={image}", f"segmentation:cells={mask}")
    register("o2", node)
    _status(node, "cells")
    _on_a_node("there", image, "o2")
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    steps = []
    remote = objects.measure_project_objects(
        Project.load("there"), units="voxel", attach=False,
        progress=lambda done, total: steps.append((done, total)))

    assert remote["objects"] == local["objects"] == 3
    assert pl.read_parquet(remote["table"]).equals(pl.read_parquet(local["table"]))
    assert steps and steps[-1][0] == steps[-1][1]


def test_a_mask_on_another_machine_than_the_image_is_said(tmp_path, node_process):
    from plexora import nodes
    from plexora.server.models.project import Project
    from plexora.server.volume import objects

    image, mask = _stack_and_mask(tmp_path)
    images = node_process(f"image:img={image}")
    masks = node_process(f"segmentation:cells={mask}")
    register("images", images)
    register("masks", masks)
    _status(masks, "cells")
    _on_a_node("split", image, "images")
    nodes.attach_segmentation("split", node="masks", resource_id="cells")

    with pytest.raises(objects.ObjectsUnavailable, match="machine that holds both"):
        objects.measure_project_objects(Project.load("split"), units="voxel", attach=False)


def _wait_for_local_mask(name, timeout=60):
    from plexora.server.models import data_model

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = data_model.get_segmentation_job_status(name)
        if status.get("status") in ("ready", "error"):
            assert status["status"] == "ready", status
            return
        time.sleep(0.1)
    raise AssertionError(f"mask job for {name} did not finish")
