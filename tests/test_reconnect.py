"""Opening a project again, after everything that carried it has gone away.

The session ends, the tunnel drops, the node exits, the laptop sleeps. What
survives is on the primary: the project record, its bindings, its ROIs and its
figures. What does not is every address and every token -- both are new next
time, by construction.

So "reconnect" cannot mean "repair the entry". It means the same names line up
again: a node re-registered under the name the project points at, serving the
same files under the same resource ids, because both ends derived those ids
from the same paths rather than exchanging them. Nothing is reconciled; nothing
has to be.

The rest of this file is the honest half -- what happens when they do NOT line
up. A missing mask must not cost somebody their figures, and a project must
never be quietly repointed at a different image.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest
import tifffile

from tests.helpers import ALL_CONFIRMED, csv_spec, project
from tests.node_harness import node_process  # noqa: F401 - fixture


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _table_file(directory):
    pl.DataFrame({
        "CellID": [1, 2, 3],
        "X_centroid": [1.0, 2.0, 3.0],
        "Y_centroid": [1.0, 2.0, 3.0],
        "CD3": [0.5, 1.5, 2.5],
    }).write_csv(directory / "cells.csv")
    return directory / "cells.csv"


def _image_file(directory, size=256, channels=2):
    path = directory / f"image_{size}_{channels}.ome.tif"
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))
    return path


def _mask_file(directory):
    from plexora.server.utils import segmentation_pyramid

    labels = np.zeros((256, 256), dtype=np.uint32)
    labels[40:60, 40:60] = 1
    flat = directory / "mask.tif"
    tifffile.imwrite(flat, labels)
    # A str comes back, and the caller wants a Path to delete it by.
    return Path(segmentation_pyramid.pyramidize_segmentation_mask(
        flat, directory / "mask_pyramid.ome.tif", overwrite=True, outline=False))


def _project_with_local_table(tmp_path, name="demo", segmentation=None):
    path = _table_file(tmp_path)
    record = project(
        name,
        dataset=csv_spec(path, cell_id="CellID", x="X_centroid", y="Y_centroid",
                         markers=("CD3",),
                         metadata=("CellID", "X_centroid", "Y_centroid")),
        segmentation=segmentation,
        confirmed=ALL_CONFIRMED, src=str(_image_file(tmp_path)))
    record.save()
    return record, path


# -- the same names line up again ------------------------------------------


def test_a_node_that_comes_back_on_a_new_port_needs_no_reconfiguration(
        client, tmp_path, node_process):
    """Both the port and the token are new every session, and the project is
    not touched. What makes it work is that the NAME is the same, and that the
    resource id was derived from the file's own path at both ends."""
    from plexora.nodes import resource_id_for
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    table = _table_file(tmp_path)
    manifest = tmp_path / "manifest.json"
    resource_id = resource_id_for(table)

    first = node_process(dynamic=True, manifest=manifest, node_id="connect-hpc-local")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": first.endpoint, "token": first.token,
        "role": "client"})
    _project_with_local_table(tmp_path, "remote-table")
    client.post("/nodes/laptop/resources",
                json={"kind": "table", "path": str(table)})
    attached = client.post("/project/remote-table/resources/table",
                           json={"node": "laptop", "resource_id": resource_id})
    assert attached.status_code == 200, attached.get_json()
    binding = Project.load("remote-table").resource("table")

    # The session ends.
    first.stop()

    # A new one: a different port, a different token, the same name and the
    # same manifest -- which is exactly what `plexora connect` produces.
    second = node_process(dynamic=True, manifest=manifest,
                          node_id="connect-hpc-local")
    assert second.port != first.port and second.token != first.token
    assert [r["id"] for r in second.get("/node/v1/hello")["resources"]] == [resource_id]
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": second.endpoint, "token": second.token,
        "role": "client"})

    # Nothing about the project changed, and it reads again.
    assert Project.load("remote-table").resource("table") == binding
    data_model.load_datasource("remote-table", reload=True)
    assert data_model.get_datasource_df().height == 3


# -- when they do not ------------------------------------------------------


def test_the_banner_names_the_command_that_brings_a_managed_node_back(
        client, tmp_path, node_process):
    """A node a saved connection set up has its address rewritten every
    session, so "check the address in Settings" is advice that cannot work --
    the entry is not wrong, the tunnel is gone."""
    from plexora.nodes import resource_id_for
    from plexora.server.models import data_model

    table = _table_file(tmp_path)
    node = node_process(dynamic=True)
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token,
        "role": "client", "managed_by": "connect:study"})
    _project_with_local_table(tmp_path, "remote-table")
    client.post("/nodes/laptop/resources",
                json={"kind": "table", "path": str(table)})
    client.post("/project/remote-table/resources/table",
                json={"node": "laptop", "resource_id": resource_id_for(table)})

    node.stop()
    data_model.load_datasource("remote-table", reload=True)
    answer = client.get("/resource_status?datasource=remote-table").get_json()

    assert "table" in answer["unavailable"]
    assert "plexora connect study" in answer["reconnect"]
    # And it says where to run it: this server cannot, and that is the whole
    # reason the message exists rather than a button.
    assert "computer you started it from" in answer["reconnect"]


def test_a_node_registered_by_hand_is_not_told_to_reconnect(
        client, tmp_path, node_process):
    """Its address is a thing somebody typed and can fix, so Settings is the
    right advice there and naming a command they never ran is not."""
    from plexora.nodes import resource_id_for
    from plexora.server.models import data_model

    table = _table_file(tmp_path)
    node = node_process(dynamic=True)
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    _project_with_local_table(tmp_path, "remote-table")
    client.post("/nodes/hpc/resources", json={"kind": "table", "path": str(table)})
    client.post("/project/remote-table/resources/table",
                json={"node": "hpc", "resource_id": resource_id_for(table)})

    node.stop()
    data_model.load_datasource("remote-table", reload=True)

    answer = client.get("/resource_status?datasource=remote-table").get_json()
    assert answer["unavailable"] and answer["reconnect"] is None


# -- a local file that moved -----------------------------------------------


def test_a_mask_that_moved_costs_the_mask_and_nothing_else(client, tmp_path):
    """It used to cost the whole project: a raw 500 on open, which takes the
    ROIs and the figures with it. A file that moved is the same shape of
    problem as a laptop that closed its lid -- one layer gone, everything else
    still there, and the fix is one field on the Edit page."""
    from plexora.server.models import data_model

    # A mask this project was already serving -- ready, not one still being
    # converted. The pending case is a different situation with a different
    # story (nothing was ever servable), and it is not what a file that moved
    # after the fact looks like.
    mask = _mask_file(tmp_path)
    _project_with_local_table(tmp_path, "moved-mask", segmentation=mask)
    mask.unlink()

    data_model.load_datasource("moved-mask", reload=True)

    # The image is untouched and the project is open.
    assert data_model.get_current_channels() is not None
    answer = client.get("/resource_status?datasource=moved-mask").get_json()
    assert "segmentation" in answer["unavailable"]
    # And it says what to do, which is the one thing a path error can say.
    assert "Edit page" in answer["unavailable"]["segmentation"]


def test_a_mask_whose_source_moved_keeps_the_pyramid_it_derived(tmp_path):
    """The derived pyramid IS the mask, so tidying up the input it came from
    must not cost anybody their cell layer.

    And nothing may rewrite the entry to "no mask" behind their back: that is
    an answer about their own cells, and it is not one they gave.
    """
    from plexora.server.models import data_model

    pyramid = _mask_file(tmp_path)
    entry = {
        "segmentation": str(pyramid),
        "segmentationSource": str(tmp_path / "gone.tif"),
        # Deliberately stale, so the "the source changed" path is the one taken.
        "segmentationSourceKey": "not-the-current-fingerprint",
        "segmentationMode": "filled",
    }

    _changed, pending = data_model.refresh_segmentation_mapping(entry, "demo")

    assert entry["segmentation"] == str(pyramid)
    assert entry.get("segmentation_status") != "pending"
    # And no conversion is queued against a file that is not there.
    assert pending is None


def test_an_image_that_moved_is_still_loud(tmp_path):
    """Deliberately not degraded. A project whose image has gone has nothing to
    draw and no coordinate space to put anything in, so opening it onto an
    empty viewer would be a worse answer than saying so."""
    from plexora.server.models import data_model

    record, _table = _project_with_local_table(tmp_path, "moved-image")
    Path(record.image.src).unlink()

    with pytest.raises(Exception):
        data_model.load_datasource("moved-image", reload=True)


# -- which image a project is on -------------------------------------------


def test_an_image_may_move_between_machines(tmp_path, node_process):
    """The whole point of a binding: the same file, reached another way."""
    from plexora.nodes import attach_image
    from plexora.server.models.project import Project
    from tests.node_harness import register

    image = _image_file(tmp_path, size=512, channels=3)
    node = node_process(f"image:slide={image}")
    register("hpc", node)

    # The geometry the project was built on, which is what the guard compares
    # against -- `image_spec` derives the channel count from the names.
    project("movable", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED,
            src=str(image), width=512, height=512).save()
    attached = attach_image("movable", node="hpc", resource_id="slide",
                            channel_names=["A", "B", "C"])

    assert attached.resource("image").node == "hpc"
    assert attached.image.width == 512


def test_an_image_may_not_be_swapped_for_a_different_one(tmp_path, node_process):
    """Every ROI outline, figure panel and cell coordinate this project holds
    is in the image's pixel space. An image of another size would leave all of
    it rendering perfectly and meaning something else -- with nothing anywhere
    in a position to notice."""
    from plexora.nodes import attach_image
    from tests.node_harness import register

    node = node_process(f"image:other={_image_file(tmp_path, size=512, channels=3)}")
    register("hpc", node)
    project("fixed", channels=("A", "B"), confirmed=ALL_CONFIRMED,
            src=str(_image_file(tmp_path, size=256, channels=2)),
            width=256, height=256).save()

    with pytest.raises(ValueError) as raised:
        attach_image("fixed", node="hpc", resource_id="other",
                     channel_names=["A", "B", "C"])

    message = str(raised.value)
    assert "256x256" in message and "512x512" in message
    # And it says what to do instead, because there is something to do.
    assert "Import a new project" in message
