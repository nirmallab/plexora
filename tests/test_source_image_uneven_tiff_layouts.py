"""The agent's and Figure Builder's reader sees the image the viewer serves.

`SourceImage` is what agent renders, QC checks, autogate crops and figure
exports read pixels through, and every caller addresses level `n` as the image
at `1 / 2**n`. Two TIFF layouts the viewer reads through a halving chain
(`tiff_series.needs_region_reader` -> `tiff_region.open_image`) must read the
same way here:

- a pyramid that steps by 4 (a Leica SCN, `bfconvert -pyramid-scale 4`): its
  levels are the halving chain's, so level 1 is half size, not a quarter;
- channels stored as one page's interleaved samples (a multispectral export):
  channel `c` is plane `c`, not a slice across the image's columns.
"""

import numpy as np
import pytest

from plexora.server.providers.local import image_geometry
from plexora.server.utils import source_image
from tests.test_legacy_tiff_layouts import _panel, _register, _smooth, _write_by_four, app  # noqa: F401


@pytest.fixture(autouse=True)
def _close_shelf():
    yield
    source_image.close_readers()


def test_a_pyramid_stepping_by_four_reads_on_the_viewers_halving_chain(app):  # noqa: F811
    client, source = app
    data = _smooth()
    path = source / "byfour_reader.ome.tif"
    _write_by_four(path, data)
    _, _, entry = _register(client, path, "byfour_reader")
    shapes = [tuple(shape) for shape in image_geometry(str(path))["level_shapes"]]

    reader = source_image.SourceImage("byfour_reader")
    try:
        # The levels the viewer recorded (maxLevel) are the levels read here.
        assert reader.levels == entry["maxLevel"] == len(shapes)
        assert [tuple(reader.level_shape(i)) for i in range(reader.levels)] == shapes
        # Level 1 is the image at 1/2: a box of level 0 halved reads the 2x2
        # mean of level 0 there.
        pixels, clipped = reader.read(1, 1, (0, 0, 512, 512))
        assert clipped == (0, 0, 512, 512)
        want = data[1, :1024, :1024].astype(np.float64).reshape(512, 2, 512, 2).mean(axis=(1, 3))
        assert pixels.shape == want.shape
        assert np.corrcoef(pixels.astype(np.float64).ravel(), want.ravel())[0, 1] > 0.99
    finally:
        reader.close()


def test_four_interleaved_samples_read_as_four_channel_planes(app):  # noqa: F811
    client, source = app
    data = _panel(channels=4)
    import tifffile

    path = source / "multispectral_reader.tif"
    tifffile.imwrite(path, np.moveaxis(data, 0, -1).copy(), photometric="minisblack",
                     planarconfig="contig", metadata=None, extrasamples=None)
    _register(client, path, "msi_reader")

    reader = source_image.SourceImage("msi_reader")
    try:
        assert tuple(reader.level_shape(0)) == (600, 700)
        for channel in range(4):
            pixels, clipped = reader.read(channel, 0, (0, 0, 100, 100))
            assert clipped == (0, 0, 100, 100)
            assert np.array_equal(pixels, data[channel, :100, :100]), channel
    finally:
        reader.close()
