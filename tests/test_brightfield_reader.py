"""The brightfield reader: the pyramid contract, the virtual halving chain,
and the pixels that come back out of it.

The contract clauses that look like trivia are the load-bearing ones, exactly
as they are for `NgffPyramid`: `data_model._zarr_level`, `read_tile`'s
`isinstance(pyramid, zarr.Array)` branches and `node/api.py`'s
`hasattr(pyramid, "shape")` test all mean "a single plane, not a pyramid" -- so
an `RgbPyramid` that grew a `.shape` would be served as one channel of
full-resolution pixels at every zoom level, silently.
"""

import numpy as np
import pytest
import tifffile as tf
import zarr

from plexora.server.utils import brightfield as bf
from plexora.server.utils import ome_zarr
from tests.brightfield_fixtures import (
    write_ambiguous_planar,
    write_interleaved_tiff,
    write_planar_rgb_tiff,
    write_rgb_ome_tiff,
    write_svs_like,
)


# -- the shape the rest of the codebase indexes --------------------------


def test_pyramid_is_not_a_plane(tmp_path):
    """Both tests that mean "single plane" have to say no."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif"))

    assert not hasattr(pyramid, "shape")
    assert not isinstance(pyramid, zarr.Array)
    assert "0" in pyramid
    assert pyramid["0"] is pyramid[0]


def test_levels_present_as_channel_y_x(tmp_path):
    """Every consumer indexes a level as [channel, rows, cols], which is what
    lets quantization, overviews and `build_extension` stay unaware that the
    samples were interleaved."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif",
                                             height=512, width=640))
    level = pyramid[0]

    assert level.shape == (3, 512, 640)
    assert level.dtype == np.uint8
    assert level[1, 0:8, 0:8].shape == (8, 8)
    assert np.asarray(level).shape == (3, 512, 640)


def test_rgb_accessor_returns_the_samples_together(tmp_path):
    """The one seam where colour leaves the module: what `read_tile` hands
    straight to the encoder."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    pyramid = bf.open_rgb(path)

    block = pyramid[0].rgb[0:16, 0:16]
    assert block.shape == (16, 16, 3)
    assert block.dtype == np.uint8

    with tf.TiffFile(path) as handle:
        source = handle.series[0].asarray()[0:16, 0:16, :3]
    assert np.array_equal(block, source)


def test_the_planes_are_the_samples(tmp_path):
    """[c] and .rgb[..., c] are the same pixels, which is what makes the
    fluorescence override an honest reading rather than a second pipeline."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif"))
    level = pyramid[0]

    for channel in range(3):
        assert np.array_equal(level[channel, 0:32, 0:32],
                              level.rgb[0:32, 0:32][..., channel])


def test_slicing_past_the_edge_returns_what_is_there(tmp_path):
    """The tile grid is ceil(size / 1024), so the last tile of every row asks
    for pixels that do not exist. A short answer, exactly as a numpy slice
    would give."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif",
                                             height=300, width=400))

    assert pyramid[0].rgb[200:1224, 300:1324].shape == (100, 100, 3)
    assert pyramid[0].rgb[500:1524, 0:1024].shape == (0, 400, 3)


def test_planar_source_reads_the_same_picture(tmp_path):
    """Separate-plane RGB goes through a different source class and has to
    arrive at identical pixels."""
    interleaved = bf.open_rgb(write_interleaved_tiff(tmp_path / "i.tif"))
    planar = bf.open_rgb(write_planar_rgb_tiff(tmp_path / "p.tif"))

    assert np.array_equal(interleaved[0].rgb[0:64, 0:64],
                          planar[0].rgb[0:64, 0:64])


# -- the virtual halving chain -------------------------------------------


def test_levels_are_the_chain_the_viewer_asks_for(tmp_path):
    """The client's tile source computes a level's size as `size >> level`, so
    the levels served are always halvings -- whatever the file holds."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif",
                                             height=4000, width=5000))

    shapes = pyramid.level_shapes
    assert shapes[0] == [4000, 5000]
    for previous, current in zip(shapes, shapes[1:]):
        assert current == [-(-previous[0] // 2), -(-previous[1] // 2)]
    # This one is flat, so the chain stops where a tile read would stop being
    # bounded -- and says so, which is what makes the top-up happen.
    assert bf.needs_extension(pyramid)


def test_a_quartering_pyramid_needs_no_conversion(tmp_path):
    """An Aperio slide steps by 4, so half the levels the viewer wants are not
    in the file. They are read from the nearest one that is -- which is what
    makes importing a whole-slide image write nothing at all."""
    pyramid = bf.open_rgb(write_svs_like(tmp_path / "slide.svs",
                                         height=1024, width=1280, levels=3))

    assert len(pyramid) == pyramid.base_levels
    assert not bf.needs_extension(pyramid)
    assert pyramid.level_shapes[0] == [1024, 1280]


def test_a_level_the_file_holds_is_passed_through_unchanged(tmp_path):
    """Where a native level lands exactly on a dyadic index, the pixels are the
    file's own -- no resample, no rounding."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=4096, width=4096,
                              pyramid=2)
    pyramid = bf.open_rgb(path)

    with tf.TiffFile(path) as handle:
        native = handle.series[0].levels[1].asarray()[0:32, 0:32, :3]
    assert np.array_equal(pyramid[1].rgb[0:32, 0:32], native)


def test_a_derived_level_is_a_box_average(tmp_path):
    """A level the file does not hold is downsampled in flight. Area-averaged,
    which is the same thing `build_extension` does for the levels it writes --
    so a tile from either path is the same picture."""
    path = write_interleaved_tiff(tmp_path / "colour.tif", height=2048, width=2048)
    pyramid = bf.open_rgb(path)

    with tf.TiffFile(path) as handle:
        full = handle.series[0].asarray()[0:64, 0:64, :3].astype(np.float32)
    expected = full.reshape(32, 2, 32, 2, 3).mean(axis=(1, 3))

    got = pyramid[1].rgb[0:32, 0:32].astype(np.float32)
    assert np.abs(got - expected).max() <= 1.0


# -- extension pyramids --------------------------------------------------


def test_a_flat_image_stops_short_and_is_topped_up(tmp_path):
    """A slide written with no pyramid at all cannot serve its coarse levels on
    the fly -- one zoomed-out tile would decode most of the image -- so the
    chain stops and `build_extension` writes the rest."""
    path = write_interleaved_tiff(tmp_path / "flat.tif", height=9000, width=9000)
    pyramid = bf.open_rgb(path)

    assert pyramid.base_levels < len(bf._dyadic_shapes(9000, 9000))
    assert bf.needs_extension(pyramid)

    store = bf.build_extension(pyramid, bf.extension_path(tmp_path))
    assert store is not None

    topped_up = bf.open_rgb(path, extension=store)
    assert len(topped_up) > topped_up.base_levels
    assert max(topped_up.level_shapes[-1]) <= ome_zarr.EXTENSION_TARGET
    # Still the same contract on the derived half.
    assert topped_up[len(topped_up) - 1].shape[0] == 3
    assert topped_up[len(topped_up) - 1].rgb[0:8, 0:8].shape == (8, 8, 3)


def test_derived_levels_are_numbered_by_absolute_index(tmp_path):
    """`build_extension` names its arrays by absolute level index so
    `open_rgb` can append them without renumbering anything."""
    path = write_interleaved_tiff(tmp_path / "flat.tif", height=9000, width=9000)
    pyramid = bf.open_rgb(path)
    store = bf.build_extension(pyramid, bf.extension_path(tmp_path))

    derived = zarr.open_group(str(store), mode="r")
    assert str(pyramid.base_levels) in derived
    assert str(pyramid.base_levels - 1) not in derived


# -- what the rest of the server asks for --------------------------------


def test_geometry_counts_planes_not_layers(tmp_path):
    """`num_channels` is what a node's geometry check compares against, so it
    has to be the number of planes the pyramid really has."""
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif",
                                             height=512, width=640))
    geometry = bf.geometry(pyramid)

    assert geometry["num_channels"] == 3
    assert (geometry["height"], geometry["width"]) == (512, 640)
    assert geometry["tile_width"] == geometry["tile_height"] == 1024
    assert geometry["levels"] == len(pyramid)


def test_overview_is_bounded_and_channel_first(tmp_path):
    pyramid = bf.open_rgb(write_rgb_ome_tiff(tmp_path / "he.ome.tif",
                                             height=4000, width=5000))
    overview = bf.overview_plane(pyramid)

    assert overview.shape[0] == 3
    assert max(overview.shape[1:]) <= 400
    assert overview.dtype == np.uint8


def test_pixel_size_comes_from_each_format(tmp_path):
    aperio = bf.physical_metadata(write_svs_like(tmp_path / "slide.svs"))
    assert aperio["physical_size_x"] == pytest.approx(0.2465)
    assert aperio["physical_size_x_unit"] == "µm"

    # No scale anywhere in this one, which is the state the scale bar already
    # hides itself for -- an empty dict, not a wrong number.
    assert bf.physical_metadata(
        write_interleaved_tiff(tmp_path / "colour.tif")) == {}


def test_rgb_region_falls_back_to_stacking_planes(tmp_path):
    """The one arrangement that reaches a brightfield tile through a plain zarr
    level: three minisblack planes served by a node, which has no project to
    have been told they are colour."""
    path = write_ambiguous_planar(tmp_path / "light.tif", light=True)
    with tf.TiffFile(path) as handle:
        level = zarr.open(handle.series[0].aszarr(), mode="r")
        block = bf.rgb_region(level, slice(0, 16), slice(0, 16))
        assert block.shape == (16, 16, 3)
        assert block.dtype == np.uint8
        assert np.array_equal(block[..., 1], np.asarray(level[1, 0:16, 0:16]))
