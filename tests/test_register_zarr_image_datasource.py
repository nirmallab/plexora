"""An OME-Zarr image registered and served through the real runtime path.

The sibling of test_register_image_datasource.py, and deliberately end to end:
the reader's own tests prove the metadata parse, but what matters here is that
nothing between `register_image_datasource` and an encoded tile had to learn
about zarr. `read_tile`, `_zarr_level`, `quantization_window_of` and the tile
route are all untouched by OME-Zarr support -- if any of them had needed a
branch, these tests are where that would show up as a crash rather than as a
design decision.
"""

import numpy as np
import pytest

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.utils import ome_zarr
from tests.helpers import use_data_root
from tests.ngff_fixtures import write_ngff, write_spatialdata_like


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    use_data_root(monkeypatch, directory)
    return directory


def test_register_records_the_zarr_kind_and_geometry(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(3, 512, 512), levels=2,
                       labels=["DNA", "CD3", "CD8"])

    entry = datasource.register_image_datasource(
        name="zarr_sample", image=image, data_dir=data_dir)

    assert entry["image_kind"] == "ome_zarr"
    assert entry["width"] == 512 and entry["height"] == 512
    assert entry["num_channels"] == 3
    assert entry["maxLevel"] == 2
    # The virtual tile grid, same as the TIFF pyramid branch.
    assert entry["tileWidth"] == 1024 and entry["tileHeight"] == 1024
    assert entry["dataset"] is None
    # omero labels are the image's own channel names, tier 2 of the same
    # ladder OME-XML names sit on.
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3", "CD8"]
    # No coarse levels were missing, so nothing was derived.
    assert "imagePyramid" not in entry
    assert not (data_dir / "zarr_sample" / ome_zarr.EXTENSION_NAME).exists()


def test_generic_channel_names_without_omero(tmp_path, data_dir):
    image = write_ngff(tmp_path / "bare.ome.zarr", shape=(2, 128, 128), levels=1)

    entry = datasource.register_image_datasource(
        name="bare", image=image, data_dir=data_dir)

    assert [c["name"] for c in entry["imageData"]] == ["Channel 1", "Channel 2"]


def test_channel_keys_carry_the_index_the_tile_route_parses(tmp_path, data_dir):
    """`_parse_channel` reads the trailing "_<N>" off a channel key to get the
    channel index; a key without one is read as the label mask."""
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 128, 128), levels=1)

    entry = datasource.register_image_datasource(
        name="keys", image=image, data_dir=data_dir)

    sources = [c["src"] for c in entry["imageData"]]
    assert sources == ["/generated/data/keys/slide_0/", "/generated/data/keys/slide_1/"]
    assert data_model._parse_channel("slide_1") == (1, False)


def test_load_and_serve_tiles(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 600, 600), levels=2)
    datasource.register_image_datasource(name="served", image=image, data_dir=data_dir)

    data_model.load_datasource("served", reload=True)

    # The pyramid is group-shaped, which is what keeps read_tile's
    # isinstance(pyramid, zarr.Array) branch on the right side.
    assert len(data_model.channels) == 2
    assert data_model.zarray.shape[0] == 2
    assert data_model.datasource is None

    tile = data_model.read_tile(data_model.channels, 1, 0, "0_0", 1024, 1024)
    assert tile.shape == (600, 600)
    assert tile.dtype == np.dtype("uint16")

    encoded, mimetype = data_model.encode_tile("served", "slide_1", 1, "0_0.png", "default")
    assert encoded and mimetype == "image/webp"

    # The window scan reads full-resolution rows in slabs off the same level.
    low, high = data_model.quantization_window_of(data_model.channels, 0)
    assert low == 0.0 and high > 1.0


def test_channel_stats_and_gmm_work_without_a_feature_table(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 256, 256), levels=1,
                       labels=["DNA", "CD3"])
    datasource.register_image_datasource(name="stats", image=image, data_dir=data_dir)
    data_model.load_datasource("stats", reload=True)

    stats = data_model.get_image_channel_stats("DNA", "stats")
    assert "image_min" in stats and "image_histogram" in stats
    assert data_model.get_channel_gmm("DNA", "stats")


def test_scale_bar_metadata_reaches_the_viewer(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(1, 128, 128), levels=1,
                       unit="micrometer", scale=0.65)
    datasource.register_image_datasource(name="scaled", image=image, data_dir=data_dir)
    data_model.load_datasource("scaled", reload=True)

    metadata = data_model.get_ome_metadata("scaled")
    assert metadata["physical_size_x"] == pytest.approx(0.65)
    assert metadata["physical_size_x_unit"] == "µm"


def test_thumbnail_is_produced(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 300, 300), levels=1)
    datasource.register_image_datasource(name="thumb", image=image, data_dir=data_dir)

    assert data_model.generate_thumbnail("thumb") is not None


# -- stores that arrive without a pyramid --------------------------------


def test_a_pyramid_less_store_gets_one_derived(tmp_path, data_dir):
    """A single-level 3000px store is legal OME-Zarr and unusable as-is: every
    zoomed-out tile would decode the full-resolution plane."""
    image = write_ngff(tmp_path / "flat.ome.zarr", shape=(2, 3000, 2500), levels=1)

    entry = datasource.register_image_datasource(
        name="flat", image=image, data_dir=data_dir)

    derived = data_dir / "flat" / ome_zarr.EXTENSION_NAME
    assert derived.is_dir()
    assert entry["imagePyramid"] == str(derived)
    assert entry["imagePyramidKey"]
    assert entry["maxLevel"] == 3
    # Level 0 is never duplicated into the derived store.
    assert sorted(p.name for p in derived.iterdir() if p.is_dir()) == ["1", "2"]


def test_derived_levels_serve_coarse_tiles(tmp_path, data_dir):
    image = write_ngff(tmp_path / "flat.ome.zarr", shape=(2, 3000, 2500), levels=1)
    datasource.register_image_datasource(name="coarse", image=image, data_dir=data_dir)

    data_model.load_datasource("coarse", reload=True)

    assert len(data_model.channels) == 3
    tile = data_model.read_tile(data_model.channels, 0, 2, "0_0", 1024, 1024)
    assert tile.shape == (750, 625)
    encoded, _ = data_model.encode_tile("coarse", "flat_0", 2, "0_0.png", "default")
    assert encoded


def test_reopening_serves_the_recorded_pyramid(tmp_path, data_dir):
    """The derived levels are found through the project's record, not by
    guessing at a filename beside the source -- the source may be read-only."""
    image = write_ngff(tmp_path / "flat.ome.zarr", shape=(1, 3000, 3000), levels=1)
    datasource.register_image_datasource(name="reopen", image=image, data_dir=data_dir)

    project = Project.load("reopen", data_dir)
    assert project.image.pyramid

    data_model.load_datasource("reopen", reload=True)
    assert len(data_model.channels) == 3


def test_deleted_derived_levels_are_rebuilt_on_open(tmp_path, data_dir):
    """They live under the project directory, which people clear out. Opening
    without them would leave the project claiming a `maxLevel` its pyramid no
    longer reaches, and every zoomed-out tile would be a 500."""
    import shutil

    image = write_ngff(tmp_path / "flat.ome.zarr", shape=(1, 3000, 3000), levels=1)
    datasource.register_image_datasource(name="wiped", image=image, data_dir=data_dir)
    derived = data_dir / "wiped" / ome_zarr.EXTENSION_NAME
    shutil.rmtree(derived)

    data_model.load_datasource("wiped", reload=True)

    assert derived.is_dir()
    assert len(data_model.channels) == 3
    assert data_model.read_tile(
        data_model.channels, 0, 2, "0_0", 1024, 1024).shape == (750, 750)


def test_spatialdata_shaped_store_resolves_to_its_image(tmp_path, data_dir):
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(2, 256, 256),
                                   levels=1, labels=["DNA", "CD3"])

    entry = datasource.register_image_datasource(
        name="element", image=store, data_dir=data_dir)

    # The recorded src is the resolved element, not the store root.
    assert entry["channelFile"] == str(store / "images" / "morphology")
    assert entry["image_kind"] == "ome_zarr"
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3"]


def test_explicit_channel_names_still_win(tmp_path, data_dir):
    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 128, 128), levels=1,
                       labels=["DNA", "CD3"])

    entry = datasource.register_image_datasource(
        name="renamed", image=image, channel_names=["A", "B"], data_dir=data_dir)

    assert [c["name"] for c in entry["imageData"]] == ["A", "B"]


def test_copy_brings_the_whole_store(tmp_path, data_dir):
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(1, 128, 128),
                                   levels=1)

    entry = datasource.register_image_datasource(
        name="copied", image=store, copy=True, data_dir=data_dir)

    copied = data_dir / "copied" / "sample.zarr"
    assert copied.is_dir()
    # Copied first, resolved after -- the store arrives whole, and the recorded
    # image is the element inside the copy.
    assert entry["channelFile"] == str(copied / "images" / "morphology")


# -- the wider registration entry points ---------------------------------


def test_csv_registration_accepts_a_zarr_image(tmp_path, data_dir):
    import polars as pl

    image = write_ngff(tmp_path / "slide.ome.zarr", shape=(2, 256, 256), levels=1)
    features = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": [1, 2, 3],
        "X_centroid": [1.0, 2.0, 3.0],
        "Y_centroid": [1.0, 2.0, 3.0],
        "DNA": [0.1, 0.2, 0.3],
        "CD3": [0.4, 0.5, 0.6],
    }).write_csv(features)

    entry = datasource.register_datasource(
        name="tabled", image=image, features=features, data_dir=data_dir)

    assert entry["image_kind"] == "ome_zarr"
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3"]
