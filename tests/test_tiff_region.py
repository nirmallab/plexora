"""`tiff_region.read_region` against tifffile's own decode of the same page.

The reader streams remote TIFFs tile by tile, so it has to agree with
`page.asarray()` pixel for pixel on every layout a TIFF can take: tiled and
striped, interleaved and planar, compressed with and without a predictor, JPEG
with shared tables, and edge tiles that run past the image.
"""

import numpy as np
import pytest
import tifffile as tf

from plexora.server.utils import tiff_region
from tests.brightfield_fixtures import (write_planar_fluorescence,
                                        write_planar_rgb_tiff,
                                        write_rgb_ome_tiff)

WINDOWS = [(0, 50, 0, 60), (37, 211, 45, 333), (100, 10_000, 200, 10_000),
           (0, 10_000, 0, 10_000), (500, 512, 630, 640)]


def _check_page(page, fetch=None):
    whole = page.asarray()
    samples = int(page.samplesperpixel)
    planar = int(page.planarconfig) == 2 and samples > 1
    for y0, y1, x0, x1 in WINDOWS:
        got = tiff_region.read_region(page, y0, y1, x0, x1, fetch=fetch)
        if planar:
            expected = np.moveaxis(whole, 0, -1)[y0:y1, x0:x1]
        else:
            expected = whole[y0:y1, x0:x1]
        assert got.shape == expected.shape, (y0, y1, x0, x1)
        assert np.array_equal(got, expected), (y0, y1, x0, x1)
        if planar:
            for s in range(samples):
                one = tiff_region.read_region(page, y0, y1, x0, x1, sample=s, fetch=fetch)
                assert np.array_equal(one, whole[s, y0:y1, x0:x1])


def _pages(path):
    tiff = tf.TiffFile(str(path), is_ome=False)
    pages = []
    for level in tiff.series[0].levels:
        pages += [page for page in level.pages]
    return tiff, pages


@pytest.mark.parametrize("writer", [
    lambda p: write_rgb_ome_tiff(p, pyramid=2),
    write_planar_rgb_tiff,
    write_planar_fluorescence,
])
def test_fixture_layouts(tmp_path, writer):
    path = tmp_path / "image.tif"
    writer(path)
    tiff, pages = _pages(path)
    try:
        for page in pages:
            _check_page(page)
    finally:
        tiff.close()


@pytest.mark.parametrize("options", [
    dict(tile=(96, 96), compression="zlib", predictor=True),
    dict(tile=(96, 96), compression="lzw"),
    dict(rowsperstrip=37, compression="zlib"),
    dict(rowsperstrip=37),
    dict(tile=(64, 128)),
])
def test_compressed_grids(tmp_path, options):
    rng = np.random.default_rng(3)
    data = rng.integers(0, 60000, size=(512, 640), dtype=np.uint16)
    path = tmp_path / "grid.tif"
    tf.imwrite(path, data, **options)
    tiff, pages = _pages(path)
    try:
        _check_page(pages[0])
    finally:
        tiff.close()


def test_jpeg_tiles_with_shared_tables(tmp_path):
    pytest.importorskip("imagecodecs")
    yy, xx = np.mgrid[0:512, 0:640]
    data = np.stack([(yy % 256), (xx % 256), ((yy + xx) % 256)], axis=-1).astype(np.uint8)
    path = tmp_path / "jpeg.tif"
    tf.imwrite(path, data, tile=(96, 96), compression="jpeg", photometric="rgb")
    tiff, pages = _pages(path)
    try:
        _check_page(pages[0])
    finally:
        tiff.close()


def test_a_positionless_fetch_gives_the_same_pixels(tmp_path):
    path = tmp_path / "image.tif"
    write_planar_fluorescence(path)
    raw = path.read_bytes()

    def fetch(spans):
        return [raw[o:o + n] for o, n in spans]

    tiff, pages = _pages(path)
    try:
        for page in pages:
            _check_page(page, fetch=fetch)
    finally:
        tiff.close()


def test_only_the_touched_tiles_are_fetched(tmp_path):
    data = np.arange(512 * 640, dtype=np.uint32).reshape(512, 640).astype(np.uint16)
    path = tmp_path / "tiles.tif"
    tf.imwrite(path, data, tile=(128, 128))
    raw = path.read_bytes()
    asked = []

    def fetch(spans):
        asked.extend(spans)
        return [raw[o:o + n] for o, n in spans]

    tiff, pages = _pages(path)
    try:
        tiff_region.read_region(pages[0], 130, 250, 10, 120, fetch=fetch)
    finally:
        tiff.close()
    assert len(asked) == 1
