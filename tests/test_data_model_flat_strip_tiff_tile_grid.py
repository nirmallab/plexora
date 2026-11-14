"""The tile grid recorded for a flat multiplex TIFF written in strips.

A single-level TIFF no larger than `image_pyramid.FLAT_IMAGE_MAX_SIDE` a side
is registered as it is (no pyramidized copy), on the promise that its
whole-image view "decodes at most sixteen tiles per channel"
(server/utils/image_pyramid.py). `convertOmeTiff` records the grid the viewer
asks tiles on (`tileWidth`/`tileHeight`), and `read_tile` slices by it. When
the file is written in compressed strips -- PIL, MATLAB, libtiff tools,
tifffile with compression -- the reader's chunk is one strip, a few rows by
the full width; recording that strip as the tile turns the home view of one
channel into hundreds of thin tile requests and encodes. The grid must be a
real tile, as the OME-Zarr branch's virtual 1024 grid is.
"""

import math

import numpy as np
import pytest
import tifffile

from plexora.server.models import data_model
from plexora.server.utils import image_pyramid


def _write_strip_tiff(path, height, width, rowsperstrip, compression):
    rng = np.random.default_rng(3)
    data = rng.integers(0, 50, size=(2, height, width), dtype=np.uint16)
    tifffile.imwrite(str(path), data, photometric="minisblack",
                     compression=compression, rowsperstrip=rowsperstrip,
                     metadata={"axes": "CYX"})


@pytest.mark.parametrize("height,width,rowsperstrip,compression", [
    (3000, 3000, 8, "zlib"),
    (2500, 3000, 10, "lzw"),
])
def test_flat_strip_tiff_records_a_real_tile_not_one_strip(
        tmp_path, height, width, rowsperstrip, compression):
    """A flat strip-written TIFF under the pyramid threshold registers on a
    tile grid of at most sixteen tiles per channel, not one tile per strip."""
    path = tmp_path / f"roi_{compression}.tif"
    _write_strip_tiff(path, height, width, rowsperstrip, compression)
    # The premise: such a file is left as it is, so nothing replaces its grid.
    assert image_pyramid.pyramid_gaps((2, height, width), 1, np.uint16) == []

    info = data_model.convertOmeTiff(str(path), dataDirectory=str(tmp_path))

    assert info["maxLevel"] == 1 and "imageCopy" not in info
    assert (info["height"], info["width"]) == (height, width)
    tiles = (math.ceil(info["height"] / info["tileHeight"])
             * math.ceil(info["width"] / info["tileWidth"]))
    assert info["tileHeight"] >= 256, (
        f"recorded tile is {info['tileWidth']} x {info['tileHeight']} "
        f"(one strip), {tiles} tile requests per channel for the whole image")
    assert tiles <= 16, f"{tiles} tiles per channel at the whole-image view"
