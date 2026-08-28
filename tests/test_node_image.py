"""A project whose image, and mask, live on another machine.

Same claim as `test_node_table.py` and the same method: a real second process,
and every answer compared against a local read of the same file rather than
against a hand-written expectation.

The bar for tiles is higher than "it works", and deliberately. The primary
forwards a node's tile bytes to the browser verbatim -- it does not decode and
re-encode them -- and that is only correct if the node produces the identical
bytes for the identical input. So the tile tests assert byte-equality, which is
the only assertion that would notice the quantization window drifting between
the two ends.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import tifffile

from tests.helpers import ALL_CONFIRMED, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture


CHANNELS = 3

#: PNG file signature. A label tile is always PNG, never WebP.
PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
SIZE = 512


def _image_file(directory, name="slide.ome.tif"):
    """A small multi-channel image with real structure in it.

    Not zeros: the quantization window is a max over full-resolution pixels and
    the GMM fit is over the log of the non-zero ones, so a blank image would
    make every one of these tests pass for the wrong reason.
    """
    rng = np.random.default_rng(7)
    data = np.zeros((CHANNELS, SIZE, SIZE), dtype=np.uint16)
    for index in range(CHANNELS):
        data[index] = rng.poisson(40 * (index + 1), (SIZE, SIZE)).astype(np.uint16)
        data[index, 100:160, 100:160] += 4000 * (index + 1)
    path = directory / name
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _local_project(tmp_path, name, path):
    """The same image, registered the ordinary way, as the comparison."""
    from plexora.datasource import register_image_datasource

    register_image_datasource(name, path)
    record = __import__("plexora.server.models.project", fromlist=["Project"]).Project.load(name)
    record.patch(confirmed=ALL_CONFIRMED).save()
    return record


@pytest.fixture
def node_image(tmp_path, node_process):
    """A project called `remote` whose image is served by a real node."""
    from plexora.nodes import attach_image

    path = _image_file(tmp_path)
    node = node_process(f"image:slide={path}")
    register("imgnode", node)

    # The geometry the project was built on, and it has to be the image's own:
    # where an image lives can change, which image it is cannot, and
    # `nodes._same_image` refuses a repoint that would silently move a project
    # into another image's pixel space.
    project("remote", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED,
            width=SIZE, height=SIZE).save()
    attached = attach_image("remote", node="imgnode", resource_id="slide",
                            channel_names=["A", "B", "C"])
    return node, attached, path


def test_attaching_an_image_records_its_geometry_centrally(node_image):
    _node, attached, _path = node_image

    # The viewer needs every one of these before it can ask for a tile, so they
    # are recorded on the primary rather than fetched per request.
    assert attached.image.width == SIZE
    assert attached.image.height == SIZE
    assert attached.image.num_channels == CHANNELS
    assert attached.image.tile_width and attached.image.tile_height
    assert attached.resource("image").node == "imgnode"
    # The names are the project's; the node knows only positions.
    assert attached.image.channel_names == ["A", "B", "C"]


def test_a_tile_is_byte_identical_to_a_local_read(node_image, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_image
    local = _local_project(tmp_path, "here", path)
    local_key = local.image.channels[0]["src"].rstrip("/").rsplit("/", 1)[-1]

    data_model.load_datasource("here", reload=True)
    local_bytes, local_type = data_model.encode_tile("here", local_key, 0, "0_0", "webp")

    data_model.load_datasource("remote", reload=True)
    remote_bytes, remote_type = data_model.encode_tile("remote", "slide_0", 0, "0_0", "webp")

    assert remote_type == local_type == "image/webp"
    # Byte-equal, not merely similar. The primary forwards these to the browser
    # untouched, so anything less would mean the two ends had drifted on the
    # quantization window and nothing would say so.
    assert remote_bytes == local_bytes


def test_an_hd_tile_is_byte_identical_too(node_image, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_image
    local = _local_project(tmp_path, "here", path)
    local_key = local.image.channels[1]["src"].rstrip("/").rsplit("/", 1)[-1]

    data_model.load_datasource("here", reload=True)
    local_bytes, _ = data_model.encode_tile("here", local_key, 0, "0_0", "hd")
    data_model.load_datasource("remote", reload=True)
    remote_bytes, _ = data_model.encode_tile("remote", "slide_1", 0, "0_0", "hd")

    assert remote_bytes == local_bytes


def test_channel_stats_match_a_local_read(node_image, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_image
    _local_project(tmp_path, "here", path)

    data_model.load_datasource("here", reload=True)
    local = data_model.get_image_channel_stats(
        data_model.real_channels("here")[0]["fullname"], "here")
    data_model.load_datasource("remote", reload=True)
    remote = data_model.get_image_channel_stats("A", "remote")

    assert remote["qmax"] == local["qmax"]
    assert remote["image_max"] == pytest.approx(local["image_max"])
    assert remote["vmin_hint"] == pytest.approx(local["vmin_hint"])
    assert len(remote["image_histogram"]) == len(local["image_histogram"])


def test_the_quantization_window_comes_from_full_resolution_pixels(node_image, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_image
    data_model.load_datasource("remote", reload=True)

    _qmin, qmax = data_model.get_channel_quantization_window("C", "remote")
    # The bright square is 4000*3 above a Poisson background: a ceiling derived
    # from the mean-pooled overview instead would sit far below it, which is
    # the failure that saturates whole channels to a solid colour.
    assert qmax >= 4000 * CHANNELS


def test_the_mini_map_overview_matches_a_local_read(node_image, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_image
    _local_project(tmp_path, "here", path)

    data_model.load_datasource("here", reload=True)
    local = data_model.generate_channel_overview(
        "here", data_model.real_channels("here")[0]["fullname"])
    data_model.load_datasource("remote", reload=True)
    remote = data_model.generate_channel_overview("remote", "A")

    assert remote == local


def test_an_unknown_channel_is_no_image_rather_than_an_error(node_image):
    from plexora.server.models import data_model

    _node, _attached, _path = node_image
    data_model.load_datasource("remote", reload=True)
    # The mini-map is decoration; a stale name costs a thumbnail, not a page.
    assert data_model.generate_channel_overview("remote", "no_such_channel") is None


def test_the_viewer_route_serves_a_node_tile(node_image):
    from plexora import app
    from plexora.server.models import data_model

    _node, _attached, _path = node_image
    data_model.load_datasource("remote", reload=True)

    client = app.test_client()
    response = client.get("/generated/data/remote/slide_0/0/0_0.png")

    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    # Cacheable, with the same contract a local tile gets: the browser is doing
    # the same job either way.
    assert response.headers["ETag"]
    assert "max-age" in response.headers["Cache-Control"]


# -- the mask --------------------------------------------------------------


def _mask_file(directory):
    """A label mask with a handful of cells, already a servable pyramid."""
    from plexora.server.utils import segmentation_pyramid

    labels = np.zeros((SIZE, SIZE), dtype=np.uint32)
    for index in range(1, 6):
        top = index * 40
        labels[top:top + 20, top:top + 20] = index
    flat = directory / "mask.tif"
    tifffile.imwrite(flat, labels)
    return segmentation_pyramid.pyramidize_segmentation_mask(
        flat, directory / "mask_pyramid.ome.tif", overwrite=True, outline=False)


def test_a_label_tile_from_a_node_carries_the_same_ids(tmp_path, node_process):
    from plexora.nodes import attach_image, attach_segmentation
    from plexora.server.models import data_model

    image = _image_file(tmp_path)
    mask = _mask_file(tmp_path)
    node = node_process(f"image:slide={image}", f"segmentation:mask={mask}")
    register("both", node)

    project("split", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED,
            width=SIZE, height=SIZE).save()
    attach_image("split", node="both", resource_id="slide",
                 channel_names=["A", "B", "C"])
    attach_segmentation("split", node="both", resource_id="mask")

    data_model.load_datasource("split", reload=True)
    encoded, mimetype = data_model.encode_tile("split", "mask", 0, "0_0", "webp")

    assert mimetype == "image/png"
    # PNG, never WebP: label tiles carry integer ids in their RGB bytes and a
    # browser's WebP decoder corrupts RGB wherever alpha is zero, which here is
    # every pixel.
    assert encoded[:8] == b"\x89PNG\r\n\x1a\n"

    from PIL import Image
    import io

    pixels = np.asarray(Image.open(io.BytesIO(encoded)))
    ids = (pixels[..., 0].astype(np.uint32)
           | (pixels[..., 1].astype(np.uint32) << 8)
           | (pixels[..., 2].astype(np.uint32) << 16))
    # The integer cell-id contract, end to end: what the mask says a pixel
    # belongs to has to be the number a value buffer is keyed on.
    assert set(np.unique(ids)) >= {0, 1, 2}


# -- reading pixels no tile API can express -------------------------------


def test_a_figure_panel_renders_from_a_node_image(node_image, tmp_path):
    """Figure Builder's export, against pixels on another machine.

    The render itself stays here: it is milliseconds of numpy over an
    already-screen-sized array, and doing it on the node would be a second
    implementation of the colour blending that could disagree about a figure.
    What crosses is the rectangle the panel covers, at the level it chose.
    """
    from plexora.plugins.figure_builder.server import render

    _node, _attached, path = node_image
    local = _local_project(tmp_path, "here", path)
    key = local.image.channels[0]["src"].rstrip("/").rsplit("/", 1)[-1]

    scene = {
        "viewport": {"x": 64, "y": 64, "w": 256, "h": 256},
        "channels": [{"key": key, "window": [0, 6000],
                      "color": {"r": 255, "g": 255, "b": 255}}],
    }
    with render.SourceImage("here") as source:
        expected, expected_meta = render.render_panel(source, scene, 128, 128)

    remote_scene = dict(scene)
    remote_scene["channels"] = [dict(scene["channels"][0], key="slide_0")]
    with render.SourceImage("remote") as source:
        assert source.levels >= 1
        actual, actual_meta = render.render_panel(source, remote_scene, 128, 128)

    assert actual.size == expected.size
    assert actual_meta["channels_rendered"] == expected_meta["channels_rendered"] == 1
    # The same pixels: the node sends raw source values, so the blend either
    # side of the wire is arithmetic over identical inputs.
    assert (np.asarray(actual) == np.asarray(expected)).all()


def test_quick_edit_reads_a_region_and_summarises_a_channel(node_image, tmp_path):
    from plexora.plugins.figure_builder.server import pixels

    _node, _attached, path = node_image
    local = _local_project(tmp_path, "here", path)
    key = local.image.channels[2]["src"].rstrip("/").rsplit("/", 1)[-1]

    here = pixels.channel_stats("here", key)
    there = pixels.channel_stats("remote", "slide_2")
    assert there["max"] == pytest.approx(here["max"])
    assert there["p999"] == pytest.approx(here["p999"])

    region = pixels.read_region("remote", "slide_2", (0, 0, 128, 128), (64, 64))
    assert region is not None


# -- more than two machines ------------------------------------------------


def test_three_resources_on_three_separate_nodes(tmp_path, node_process):
    """Nothing in the design counts to two.

    The manifest binds each resource independently, so "image on the cluster,
    mask on the workstation, table on the laptop" is three attachments rather
    than a topology anything has to know about. Worth pinning: a two-server
    assumption is the kind that hides in a helper and only shows up on the
    machine nobody tested on.
    """
    import polars as pl

    from plexora.nodes import (attach_image, attach_segmentation, attach_table,
                               register_node)
    from plexora.server.models import data_model
    from tests.helpers import csv_spec

    image = _image_file(tmp_path)
    mask = _mask_file(tmp_path)
    cells = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": [1, 2, 3, 4, 5],
        "X_centroid": [40.0, 80.0, 120.0, 160.0, 200.0],
        "Y_centroid": [40.0, 80.0, 120.0, 160.0, 200.0],
        "CD3": [1.0, 2.0, 3.0, 4.0, 5.0],
    }).write_csv(cells)

    image_node = node_process(f"image:slide={image}")
    mask_node = node_process(f"segmentation:mask={mask}")
    table_node = node_process(f"table:cells={cells}")
    for name, node in (("imgs", image_node), ("masks", mask_node),
                       ("tables", table_node)):
        register_node(name, node.endpoint, token=node.token, verify=True)

    record = project("spread", channels=("A", "B", "C"),
                     confirmed=ALL_CONFIRMED, width=SIZE, height=SIZE,
                     dataset=csv_spec(cells, cell_id="CellID", x="X_centroid",
                                      y="Y_centroid", markers=("CD3",),
                                      metadata=("CellID", "X_centroid",
                                                "Y_centroid")))
    record.save()
    attach_image("spread", node="imgs", resource_id="slide",
                 channel_names=["A", "B", "C"])
    attach_segmentation("spread", node="masks", resource_id="mask")
    attached = attach_table("spread", node="tables", resource_id="cells")

    assert sorted(attached.resources) == ["image", "segmentation", "table"]
    assert {b.node for b in attached.resources.values()} == {"imgs", "masks", "tables"}

    data_model.load_datasource("spread", reload=True)
    # One question answered by each of the three machines, in one project.
    channel, _ = data_model.encode_tile("spread", "slide_0", 0, "0_0", "webp")
    labels, _ = data_model.encode_tile("spread", "mask", 0, "0_0", "webp")
    values = data_model.get_all_cells("spread", ["CD3"], float)

    assert len(channel) > 0
    assert labels[:8] == PNG_MAGIC
    assert list(values) == [1.0, 2.0, 3.0, 4.0, 5.0]
    # And the spatial index, built here from the compact copy, answers without
    # touching any of them.
    assert data_model.query_for_closest_cell(80.0, 80.0, "spread")["CellID"] == 2


# -- a mask that is not ready to serve -------------------------------------


def _flat_mask(path):
    """What a segmentation pipeline actually writes: one full-resolution,
    untiled plane. No tile route can serve a zoomed-out level of it."""
    labels = np.zeros((SIZE, SIZE), dtype=np.uint32)
    labels[20:60, 20:60] = 3
    tifffile.imwrite(path, labels)
    return path


def test_a_flat_mask_is_converted_at_startup_and_served(tmp_path):
    """A node handed a raw pipeline mask prepares it itself.

    The alternative -- which this replaced -- was refusing to start with the
    conversion command printed, which made the ordinary case (a mask on the
    machine that produced it) a two-command dance with a filename to carry
    between them.
    """
    from plexora.server.node.app import create_node_app
    from plexora.server.utils import segmentation_pyramid as sp

    flat = _flat_mask(tmp_path / "mask.tif")
    app = create_node_app([f"segmentation:mask={flat}"], token="x",
                          log=lambda *a, **k: None)

    resource = app.config["PLEXORA_NODE_RESOURCES"].get("mask")
    # It serves the derived pyramid, not the file named on the command line.
    assert Path(resource.path) != flat
    assert Path(resource.path).parent == tmp_path, "written beside the mask"
    # A mask this small converts to one tiled level -- there is nothing to
    # downsample a 512 px image to under a 1024 px tile -- so what makes it
    # servable is that Plexora produced it, which is the rule
    # `refresh_segmentation_mapping` applies before adopting a derived file.
    assert sp.generated_mask_kind(resource.path) == sp.MODE_FILLED
    # And the provider went with it: a repoint that left the old provider in
    # place would serve tiles of the flat mask and look entirely fine.
    assert str(resource.provider.path) == resource.path


def test_a_second_start_adopts_the_pyramid_the_first_one_built(tmp_path):
    """Restarting a node must not re-convert. On a whole-slide mask that is
    minutes and gigabytes, every time, for a file that is already right
    there."""
    from plexora.server.node.app import create_node_app
    from plexora.server.utils import segmentation_pyramid as sp

    flat = _flat_mask(tmp_path / "mask.tif")
    first = create_node_app([f"segmentation:mask={flat}"], token="x",
                            log=lambda *a, **k: None)
    written = Path(first.config["PLEXORA_NODE_RESOURCES"].get("mask").path)
    stamped = written.stat().st_mtime_ns

    second = create_node_app([f"segmentation:mask={flat}"], token="x",
                             log=lambda *a, **k: None)
    adopted = Path(second.config["PLEXORA_NODE_RESOURCES"].get("mask").path)
    assert adopted == written
    assert adopted.stat().st_mtime_ns == stamped, "adopted, not rebuilt"
    assert sp.generated_mask_kind(adopted) == sp.MODE_FILLED


def test_a_mask_regenerated_after_its_pyramid_is_converted_again(tmp_path):
    """The other half of adoption: a stale pyramid must not be served.

    A node has no config.json and so no recorded fingerprint to compare
    against -- what it has is the two files' modification times, which is
    enough to notice that the mask was rewritten after the pyramid was built.
    """
    from plexora.server.node.app import create_node_app
    from plexora.server.utils import segmentation_pyramid as sp

    flat = _flat_mask(tmp_path / "mask.tif")
    first = create_node_app([f"segmentation:mask={flat}"], token="x",
                            log=lambda *a, **k: None)
    written = Path(first.config["PLEXORA_NODE_RESOURCES"].get("mask").path)

    # The pipeline ran again and wrote a different mask to the same path. Aged
    # by moving the pyramid into the past rather than the mask into the future:
    # a derived file stamped later than the moment it was written is not a
    # thing that happens, and building the test on one would leave the rebuilt
    # pyramid older than its own source.
    labels = np.zeros((SIZE, SIZE), dtype=np.uint32)
    labels[100:200, 100:200] = 7
    tifffile.imwrite(flat, labels)
    stale = flat.stat().st_mtime_ns - 10 ** 9
    os.utime(written, ns=(stale, stale))

    again = create_node_app([f"segmentation:mask={flat}"], token="x",
                            log=lambda *a, **k: None)
    rebuilt = Path(again.config["PLEXORA_NODE_RESOURCES"].get("mask").path)
    assert rebuilt == written
    assert rebuilt.stat().st_mtime_ns > stale, "rebuilt from the new mask"
    assert sp.generated_mask_kind(rebuilt) == sp.MODE_FILLED


def test_a_mask_in_a_read_only_directory_is_refused_with_a_destination(tmp_path,
                                                                      monkeypatch):
    """Where the old refusal still belongs.

    Converting writes a file often larger than the mask it came from, so when
    the mask's own directory will not take a write there is a real question
    about somebody's disk quota to answer, and nothing sensible to guess.
    """
    from plexora import paths
    from plexora.server.node.app import NodeStartupError, create_node_app

    flat = _flat_mask(tmp_path / "mask.tif")
    # Rather than chmod, which does not mean on Windows what it means on a
    # cluster filesystem. The question this asks is "what does the node do when
    # told it cannot write there", and that is the answer either way.
    monkeypatch.setattr(paths, "is_writable", lambda root: False)

    with pytest.raises(NodeStartupError) as raised:
        create_node_app([f"segmentation:mask={flat}"], token="x",
                        log=lambda *a, **k: None)

    message = str(raised.value)
    assert str(flat) in message
    assert str(tmp_path) in message, "names the directory that refused the write"
    # The fix, spelled out, on the machine the operator is already sitting at.
    assert "plexora node prepare" in message


def test_preparing_a_mask_ahead_of_time_needs_no_paths(tmp_path):
    """`prepare` then `serve`, with no filename carried between them -- both
    ends derive the same destination from the mask's own path."""
    from plexora.server.node.app import create_node_app, prepare_mask
    from plexora.server.utils import segmentation_pyramid as sp

    flat = _flat_mask(tmp_path / "mask.tif")
    written = prepare_mask(flat, log=lambda *a, **k: None)
    assert sp.generated_mask_kind(written) == sp.MODE_FILLED

    app = create_node_app([f"segmentation:mask={flat}"], token="x",
                          log=lambda *a, **k: None)
    assert app.config["PLEXORA_NODE_RESOURCES"].get("mask").path == str(written)


def test_an_outline_pyramid_is_served_and_reported_as_outlines(tmp_path):
    """An operator who chose outlines gets outlines -- and the primary is told
    so, because the two modes draw different pictures and neither errors."""
    from plexora.server.node.app import create_node_app, prepare_mask
    from plexora.server.utils import segmentation_pyramid as sp

    flat = _flat_mask(tmp_path / "mask.tif")
    written = prepare_mask(flat, outline=True, log=lambda *a, **k: None)

    app = create_node_app([f"segmentation:mask={flat}"], token="x",
                          log=lambda *a, **k: None)
    resource = app.config["PLEXORA_NODE_RESOURCES"].get("mask")
    assert resource.path == str(written), "adopted rather than converted to filled"
    assert resource.describe()["mask_mode"] == sp.MODE_OUTLINES
