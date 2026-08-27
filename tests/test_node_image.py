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

import numpy as np
import pytest
import tifffile

from tests.helpers import ALL_CONFIRMED, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture


CHANNELS = 3
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

    project("remote", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED).save()
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

    project("split", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED).save()
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
