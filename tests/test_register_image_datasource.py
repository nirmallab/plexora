"""End-to-end check that an image-only ("quick view") datasource -- no
feature table, no segmentation -- can be registered and loaded through the
real runtime path. A null `dataset` block is
the explicit, first-class "no feature data" state (datasource.py no longer
writes a synthetic stub CSV): load_datasource() must leave data_model.datasource
as None instead of loading anything, and every direct consumer of the
feature table/ball tree must tolerate that (return an empty/graceful result)
rather than crashing. The marker-less image channels must also not break
get_channel_gmm/get_image_channel_stats (both already tolerate image
channels with no matching feature column). Mirrors
test_optional_segmentation.py's pattern.
"""

import numpy as np
import tifffile
from PIL import Image

from plexora import datasource
from plexora.server.models import data_model


def _write_image(path, size=256, channels=3):
    rng = np.random.default_rng(0)
    tifffile.imwrite(path, rng.integers(1, 255, size=(channels, size, size), dtype=np.uint8))


def _write_png(path, size=64):
    Image.fromarray(np.zeros((size, size, 3), dtype=np.uint8)).save(path)


def test_register_and_load_image_datasource(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path)

    entry = datasource.register_image_datasource(
        name="quick_view_sample",
        image=image_path,
        data_dir=data_dir,
    )

    assert entry["image_kind"] == "ome_tiff"
    assert entry["segmentation"] is None
    # No dataset block IS the image-only state -- there is no separate flag
    # that could disagree with it.
    assert entry["dataset"] is None
    assert [c["name"] for c in entry["imageData"]] == ["Channel 1", "Channel 2", "Channel 3"]

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)

    data_model.load_datasource("quick_view_sample", reload=True)

    # No feature file exists on disk for this datasource -- load_datasource
    # must not attempt to read one.
    assert data_model.datasource is None
    assert data_model.ball_tree is None
    assert data_model.channels is not None
    assert data_model.seg is None

    # Every direct consumer of the feature table/ball tree must return a
    # graceful empty result instead of crashing on the missing table.
    cells = data_model.get_all_cells("quick_view_sample", ["id", "X", "Y"], int)
    assert len(cells) == 0
    assert data_model.query_for_closest_cell(0, 0, "quick_view_sample") == {}

    # get_datasource_description is numeric-feature-column stats only now
    # (image-channel stats moved to get_image_channel_stats) -- empty here
    # since there's no feature table to describe.
    description = data_model.get_datasource_description("quick_view_sample")
    assert description == {}

    # The per-channel numeric endpoints that compute straight from pixel
    # data (not feature-table columns) -- must not crash on a datasource
    # with zero real markers.
    stats = data_model.get_image_channel_stats("Channel 1", "quick_view_sample")
    assert "image_min" in stats
    assert "image_histogram" in stats

    gmm = data_model.get_channel_gmm("Channel 1", "quick_view_sample")
    assert gmm


def test_register_and_load_image_datasource_above_downsample_threshold(tmp_path, monkeypatch):
    """load_datasource's block_reduce downsample path (data_model.py, only
    entered when the smallest usable pyramid level is still >400px in a
    dimension) used to crash with AttributeError('Array' object has no
    attribute 'strides') because it ran on a lazy zarr.Array instead of a
    real numpy array. A quick-view image is registered with no pyramid at
    all (register_image_datasource never writes one), so its one and only
    level is exactly what gets block_reduce'd here whenever it's above the
    threshold -- this is the common case for quick view, not an edge case.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path, size=512, channels=2)

    datasource.register_image_datasource(name="big_sample", image=image_path, data_dir=data_dir)

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)

    data_model.load_datasource("big_sample", reload=True)

    assert data_model.zarray.shape[1] <= 400
    assert data_model.zarray.shape[2] <= 400


def test_register_image_datasource_explicit_channel_names(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path, channels=2)

    entry = datasource.register_image_datasource(
        name="named_sample",
        image=image_path,
        channel_names=["DAPI", "CD3"],
        data_dir=data_dir,
    )

    assert [c["name"] for c in entry["imageData"]] == ["DAPI", "CD3"]


def test_register_image_datasource_channel_count_mismatch(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path, channels=2)

    try:
        datasource.register_image_datasource(
            name="bad_sample",
            image=image_path,
            channel_names=["OnlyOne"],
            data_dir=data_dir,
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_dedupe_dataset_name():
    assert datasource._dedupe_dataset_name("foo", []) == "foo"
    assert datasource._dedupe_dataset_name("foo", ["foo"]) == "foo_2"
    assert datasource._dedupe_dataset_name("foo", ["foo", "foo_2"]) == "foo_3"


def test_derive_dataset_name_from_path():
    assert datasource._derive_dataset_name_from_path("/a/b/sample.ome.tif") == "sample"
    assert datasource._derive_dataset_name_from_path("C:\\a\\b\\sample.tiff") == "sample"
    assert datasource._derive_dataset_name_from_path("/a/b/photo.PNG") == "photo"


def test_sniff_quick_view_kind(tmp_path):
    tif_path = tmp_path / "img.tif"
    _write_image(tif_path, channels=1)
    assert datasource._sniff_quick_view_kind(str(tif_path)) == "ome_tiff"

    png_path = tmp_path / "img.png"
    _write_png(png_path)
    assert datasource._sniff_quick_view_kind(str(png_path)) == "rgb"

    try:
        datasource._sniff_quick_view_kind("/a/b/whatever.csv")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_register_rgb_datasource(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    png_path = tmp_path / "photo.png"
    _write_png(png_path, size=64)

    entry = datasource.register_rgb_datasource(
        name="rgb_sample",
        image=png_path,
        data_dir=data_dir,
    )

    assert entry["image_kind"] == "rgb"
    assert entry["imageData"] == []
    assert entry["num_channels"] == 0
    assert entry["width"] == 64
    assert entry["height"] == 64
    assert entry["segmentation"] is None
    assert entry["channelFile"] == str(png_path)

def test_tile_requests_do_not_reload_the_datasource(tmp_path, monkeypatch):
    """An image-only datasource has datasource=None (no feature table) and
    seg=None (no segmentation). load_datasource()'s early return used to
    require `datasource is not None`, and generate_zarr_png()'s guard treated
    `seg is None` as "not loaded" -- so between them every single tile request
    re-ran the whole load: reopening the OME-TIFF, re-parsing the OME-XML,
    re-materializing the overview, wiping the derived caches and bumping
    load_generation. Because data_routes keys its encoded-tile LRU on
    load_generation, that also pinned the tile cache at a 0% hit rate forever.

    Loadedness is now tracked explicitly by _loaded_source, so serving tiles
    must leave load_generation untouched.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path)

    datasource.register_image_datasource(
        name="tile_cache_sample", image=image_path, data_dir=data_dir
    )

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)

    data_model.load_datasource("tile_cache_sample", reload=True)
    assert data_model.datasource is None and data_model.seg is None
    generation_after_load = data_model.load_generation

    for tile in ("0_0.png", "0_0.png", "0_0.png"):
        encoded, mimetype = data_model.encode_tile(
            "tile_cache_sample", "tile_cache_sample_0", "0", tile, "webp"
        )
        assert encoded and mimetype == "image/webp"

    assert data_model.load_generation == generation_after_load
