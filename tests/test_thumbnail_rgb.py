"""A brightfield slide's card on the Samples page.

A Visium HD run's H&E is an interleaved RGB pyramid -- `(y, x, 3)` at every
level -- and the thumbnail reader took each level's shape as `(channel, y,
x)`. Every level then failed the ">= 200 pixels" test on its "width" of 3, the
read fell through to FULL resolution, and `array[0]` -- one row of pixels --
became the picture: a 56-byte strip where the tissue should have been.
"""

import numpy as np
import tifffile

from plexora.server.models import data_model


def _pyramid(path, levels):
    """An interleaved RGB pyramid in SubIFDs, the layout Plexora's tiler writes."""
    with tifffile.TiffWriter(path) as tiff:
        tiff.write(levels[0], subifds=len(levels) - 1, photometric="rgb",
                   tile=(128, 128))
        for level in levels[1:]:
            tiff.write(level, subfiletype=1, photometric="rgb", tile=(128, 128))


def _levels(height, width, count):
    rng = np.random.default_rng(3)
    return [rng.integers(0, 255, size=(height >> i, width >> i, 3), dtype=np.uint8)
            for i in range(count)]


def test_an_interleaved_slide_is_read_from_a_coarse_level_in_colour(tmp_path):
    path = tmp_path / "he.ome.tif"
    _pyramid(path, _levels(1600, 1200, 4))            # 1600, 800, 400, 200

    array = data_model._local_thumbnail_plane(str(path), rgb=True)

    # The smallest level with both SPATIAL sides >= 200: 200 x 150 fails on
    # the width, so 400 x 300 -- never the full-resolution 1600 x 1200.
    assert array.shape == (400, 300, 3)


def test_an_interleaved_slide_without_colour_is_one_plane_not_one_row(tmp_path):
    path = tmp_path / "he.ome.tif"
    _pyramid(path, _levels(1600, 1200, 4))

    array = data_model._local_thumbnail_plane(str(path), rgb=False)

    assert array.shape == (400, 300)


def test_a_colour_array_makes_a_colour_card():
    rgb = np.zeros((40, 30, 3), dtype=np.uint8)
    rgb[..., 0] = 200

    image = data_model._thumbnail_image(rgb)

    assert image.mode == "RGB"
    assert image.size == (30, 40)
    assert image.getpixel((5, 5)) == (200, 0, 0)       # eight bits, untouched


def test_a_plane_still_makes_a_stretched_grey_card():
    plane = np.linspace(0, 4000, 40 * 30).reshape(40, 30).astype(np.uint16)

    image = data_model._thumbnail_image(plane)

    assert image.mode == "L"
    assert image.getextrema() == (0, 255)
