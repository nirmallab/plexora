"""An array served as a pyramid, against the same array written to a file.

Two things are being pinned. The first is the duck-type: everything that reads
a pyramid does so through `group[str(level)]`, `len(group)` and a level indexed
`[channel, rows, cols]`, and deliberately tells a pyramid from a single plane by
the ABSENCE of `.shape` -- so a memory pyramid that grew one would silently be
read as a one-level image.

The second is the tiles. `read_tile` is pure over whatever pyramid it is handed,
so a tile of an array served from memory and a tile of the same array written to
an OME-TIFF and served locally have to be the same bytes. If they ever are not,
the two paths are not two transports for one picture.
"""

import numpy as np
import pytest

from plexora.server.models import data_model
from plexora.server.utils import memory_pyramid, ome_zarr


def _image(channels=3, height=1200, width=1500):
    rng = np.random.default_rng(11)
    return (rng.random((channels, height, width)) * 4000).astype("uint16")


def _mask(height=1200, width=1500):
    labels = np.zeros((height, width), dtype="uint32")
    labels[100:300, 100:400] = 7
    labels[600:900, 800:1200] = 4123
    return labels


# -- the duck-type -------------------------------------------------------


def test_a_memory_pyramid_is_shaped_like_the_group_a_tiff_produces():
    pyramid = memory_pyramid.image_pyramid(_image())

    assert len(pyramid) > 1
    assert pyramid["0"].shape == (3, 1200, 1500)
    assert data_model._zarr_level(pyramid, 1).shape[-2:] == (600, 750)
    # The absent attribute IS the contract -- see the module docstring.
    assert not hasattr(pyramid, "shape")


def test_the_levels_halve_all_the_way_down_to_one_tile():
    pyramid = memory_pyramid.image_pyramid(_image(height=4000, width=5000))
    shapes = pyramid.level_shapes

    assert ome_zarr.dyadic_prefix(shapes) == len(shapes)
    assert max(shapes[-1]) <= ome_zarr.EXTENSION_TARGET


def test_a_two_dimensional_image_is_read_as_one_channel():
    """A single-channel image is routinely handed over without the axis, and
    read as (rows, cols) it would be an image with 1200 channels."""
    pyramid = memory_pyramid.image_pyramid(_image(channels=1)[0])

    assert pyramid["0"].shape == (1, 1200, 1500)


def test_an_array_of_the_wrong_rank_says_so():
    with pytest.raises(ValueError):
        memory_pyramid.image_pyramid(np.zeros((2, 3, 4, 5), dtype="uint16"))


def test_level_zero_is_the_callers_own_array_not_a_copy():
    array = _image(height=300, width=300)
    assert memory_pyramid.image_pyramid(array)["0"] is array


# -- masks ---------------------------------------------------------------


def test_label_levels_are_subsampled_rather_than_averaged():
    """The mean of cell 41 and cell 43 is cell 42. Every level has to hold ids
    that exist."""
    pyramid = memory_pyramid.label_pyramid(_mask())
    present = set(np.unique(np.asarray(pyramid["1"])))

    assert present <= {0, 7, 4123}
    assert np.array_equal(np.asarray(pyramid["1"]),
                          np.asarray(pyramid["0"])[::2, ::2])


def test_a_label_pyramid_costs_no_memory_beyond_the_mask():
    mask = _mask()
    pyramid = memory_pyramid.label_pyramid(mask)

    assert len(pyramid) > 1
    assert memory_pyramid.level_bytes(pyramid) == mask.nbytes


def test_a_mask_with_a_leading_singleton_axis_is_flattened():
    pyramid = memory_pyramid.label_pyramid(_mask()[np.newaxis, ...])
    assert pyramid["0"].ndim == 2


# -- the same picture as the file ----------------------------------------


def _tiff(array, path):
    import tifffile

    tifffile.imwrite(path, array, tile=(1024, 1024), photometric="minisblack",
                     metadata={"axes": "CYX"})
    return path


@pytest.mark.parametrize("tile", ["0_0", "1_0", "0_1"])
def test_a_tile_from_memory_is_the_tile_from_the_file(tmp_path, tile):
    from plexora.server.providers.local import LocalImageProvider

    array = _image()
    from_file, _overview, _metadata = LocalImageProvider(
        str(_tiff(array, tmp_path / "image.ome.tif"))).open()
    from_memory = memory_pyramid.image_pyramid(array)

    assert np.array_equal(
        data_model.read_tile(from_memory, 1, 0, tile, 1024, 1024),
        data_model.read_tile(from_file, 1, 0, tile, 1024, 1024))


def test_the_quantization_window_is_read_off_the_full_resolution_plane():
    """Not off the overview -- see `quantization_window_of`. The ceiling has to
    be the real maximum, which pooling would have lowered."""
    array = _image()
    array[2, 700, 900] = 60000
    pyramid = memory_pyramid.image_pyramid(array)

    assert data_model.quantization_window_of(pyramid, 2) == (0.0, 60000.0)


def test_the_geometry_describes_the_finest_level():
    geometry = ome_zarr.geometry(memory_pyramid.image_pyramid(_image()))

    assert geometry["width"] == 1500
    assert geometry["height"] == 1200
    assert geometry["num_channels"] == 3
    assert geometry["levels"] == len(geometry["level_shapes"])
