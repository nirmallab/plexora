"""End-to-end check that an image-only ("quick view") datasource -- no
feature table, no segmentation -- can be registered and loaded through the
real runtime path. The synthetic 1-row stub feature table written by
register_image_datasource must be enough to satisfy load_datasource's
CsvAdapter/ball-tree assumptions, and the marker-less image channels must
not break get_datasource_description/get_channel_gmm (both already tolerate
image channels with no matching feature column -- this test proves it holds
for a genuinely empty feature set too, not just a partially-matching one).
Mirrors test_optional_segmentation.py's pattern.
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
    assert [c["name"] for c in entry["imageData"]] == ["Channel 1", "Channel 2", "Channel 3"]
    assert entry["featureData"][0]["xCoordinate"] == "X"
    assert entry["featureData"][0]["yCoordinate"] == "Y"
    assert entry["featureData"][0]["idField"] == "id"

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)

    data_model.load_datasource("quick_view_sample", reload=True)

    assert data_model.datasource.height == 1
    assert data_model.channels is not None
    assert data_model.seg is None

    # numericData.js's fetchCells() always sends [idField, xCoordinate,
    # yCoordinate] to this endpoint -- idField must resolve to a real
    # column (CsvAdapter's own synthesized positional 'id'), or this 500s
    # with a ColumnNotFoundError on an empty column name.
    cells = data_model.get_all_cells("quick_view_sample", ["id", "X", "Y"], int)
    assert len(cells) == 1 * 3

    # The two per-channel numeric endpoints that compute straight from pixel
    # data (not feature-table columns) -- must not crash on a datasource
    # with zero real markers.
    description = data_model.get_datasource_description("quick_view_sample")
    assert "image_min" in description["Channel 1"]
    assert "image_histogram" in description["Channel 1"]

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
