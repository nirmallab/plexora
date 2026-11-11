"""KFBio KFB, read from synthetic files written to the layout in the docstring
of `plexora/server/utils/formats/kfb.py` (`formats_fixtures_more.write_kfb`).

Every 8 x 8 block of every level has its own colour (`kfb_rgb`), and the
tiles are JPEGs (quality 100, no chroma subsampling, blocks aligned with the
JPEG's own 8 x 8 grid), so a decoded pixel is within a few levels of what was
written while a tile from the wrong place, level or offset is far off.
Unscanned glass is exactly 255. The fixture's tile side is 128, not the
scanners' 256, so a reader must take it from the header.
"""

import random
import threading

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, kfb
from tests.formats_fixtures_more import (KFB_PICTURES, KfbSlide, kfb_expected, kfb_level_shapes,
                                         write_kfb)

#: the most a decoded JPEG sample may differ from the colour written
TOLERANCE = 4


def _close_to(got, want, what=""):
    assert got.shape == want.shape, f"{what}: shape {got.shape} != {want.shape}"
    diff = np.abs(got.astype(np.int16) - want.astype(np.int16))
    bad = np.argwhere(diff > TOLERANCE)
    assert not len(bad), (f"{what}: {len(bad)} samples off by up to {diff.max()}, first at "
                          f"{tuple(bad[0])}: {got[tuple(bad[0])]} != {want[tuple(bad[0])]}")


@pytest.fixture(scope="module")
def slide_file(tmp_path_factory):
    slide = KfbSlide()
    path = write_kfb(tmp_path_factory.mktemp("kfb") / "scan.kfb", slide)
    return path, slide


def _open(path, index=0):
    return formats.open_reader(path, index)


# -- levels ----------------------------------------------------------------------


def test_levels_are_finest_first_halved_and_rounded_down(slide_file):
    path, slide = slide_file
    reader = _open(path)
    try:
        assert reader.levels == kfb_level_shapes(slide)
        assert reader.levels[0] == (300, 1300)
        assert reader.levels[1] == (150, 650)
        assert reader.levels[3] == (37, 162)
        assert reader.rgb and reader.channels == 3 and reader.dtype == np.dtype(np.uint8)
    finally:
        reader.close()


def test_every_level_reads_its_own_tiles(slide_file):
    path, slide = slide_file
    reader = _open(path)
    try:
        for level, (h, w) in enumerate(reader.levels):
            got = reader.read_rgb(level, 0, h, 0, w)
            assert got.dtype == np.uint8
            _close_to(got, kfb_expected(slide, level), f"level {level}")
    finally:
        reader.close()


def test_the_staircase_below_the_short_strip_is_white_glass(slide_file):
    """Strip two's bottom is 7 rows short of the image: nothing covers those
    rows, and they read 255 exactly, while strip one reaches the bottom."""
    path, slide = slide_file
    reader = _open(path)
    try:
        span = slide.strip_tiles * slide.tile         # 1024
        below = reader.read_rgb(0, 293, 300, span, 1300)
        assert (below == 255).all()
        _close_to(reader.read_rgb(0, 286, 293, span, 1300),
                  kfb_expected(slide, 0)[286:293, span:1300], "above the step")
        _close_to(reader.read_rgb(0, 293, 300, 0, span),
                  kfb_expected(slide, 0)[293:300, :span], "strip one to the bottom")
        # a rectangle across the step
        _close_to(reader.read_rgb(0, 280, 300, 1000, 1050),
                  kfb_expected(slide, 0)[280:300, 1000:1050], "across the step")
    finally:
        reader.close()


@pytest.mark.parametrize("level", [0, 1, 2])
def test_regions_across_tile_boundaries_and_at_the_edges(slide_file, level):
    path, slide = slide_file
    want = kfb_expected(slide, level)
    h, w = want.shape[:2]
    reader = _open(path)
    try:
        cases = [(120, 140, 120, 140), (0, h, w - 3, w), (h - 1, h, 0, w), (h - 1, h, w - 1, w),
                 (0, 1, 0, 1), (100, 160, 250, 270)]
        rng = random.Random(level)
        for _ in range(15):
            y0, x0 = rng.randrange(h), rng.randrange(w)
            cases.append((y0, rng.randint(y0 + 1, h), x0, rng.randint(x0 + 1, w)))
        for y0, y1, x0, x1 in cases:
            if y1 > h or x1 > w:
                continue
            _close_to(reader.read_rgb(level, y0, y1, x0, x1), want[y0:y1, x0:x1],
                      f"level {level} [{y0}:{y1}, {x0}:{x1}]")
    finally:
        reader.close()


def test_read_is_one_sample_of_read_rgb(slide_file):
    path, _slide = slide_file
    reader = _open(path)
    try:
        block = reader.read_rgb(1, 40, 140, 100, 300)
        for channel in range(3):
            np.testing.assert_array_equal(reader.read(1, channel, 40, 140, 100, 300),
                                          block[..., channel])
    finally:
        reader.close()


@pytest.mark.parametrize("order", ["raster", "shuffled"])
def test_the_order_of_the_index_records_does_not_matter(tmp_path, slide_file, order):
    _path, slide = slide_file
    other = KfbSlide(record_order=order)
    path = write_kfb(tmp_path / f"{order}.kfb", other)
    reader = _open(path)
    try:
        assert reader.levels == kfb_level_shapes(slide)
        for level in (0, 1):
            h, w = reader.levels[level]
            _close_to(reader.read_rgb(level, 0, h, 0, w), kfb_expected(slide, level),
                      f"{order} level {level}")
    finally:
        reader.close()


def test_a_second_one_by_one_level_at_the_bottom_is_skipped(tmp_path):
    slide = KfbSlide(height=512, width=512, tile=128, drops=(0,), duplicate_bottom=True)
    path = write_kfb(tmp_path / "sq.kfb", slide)
    reader = _open(path)
    try:
        assert reader.levels[-1] == (1, 1)
        assert reader.levels.count((1, 1)) == 1
        assert all(h >= 1 and w >= 1 for h, w in reader.levels)
        assert reader.levels == kfb_level_shapes(slide)
        _close_to(reader.read_rgb(len(reader.levels) - 1, 0, 1, 0, 1),
                  kfb_expected(slide, len(reader.levels) - 1), "the 1 x 1 level")
    finally:
        reader.close()


# -- what the header says ---------------------------------------------------------


def test_pixel_size_and_the_description(slide_file):
    path, slide = slide_file
    description = formats.describe(path)
    assert description.format_name == "KFBio KFB"
    assert description.rgb is True and description.channels == 3
    assert description.dtype == "uint8"
    assert (description.height, description.width) == (300, 1300)
    assert description.pixel_size == (pytest.approx(0.486, abs=1e-6),) * 2
    assert description.level_shapes == [list(shape) for shape in kfb_level_shapes(slide)]
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "brightfield" and verdict.confidence == "high"
    assert formats.physical_metadata(path)["physical_size_x"] == pytest.approx(0.486, abs=1e-6)


def test_the_macro_label_and_thumbnail(slide_file):
    path, _slide = slide_file
    reader = kfb.reader(str(path))
    try:
        for name, ((h, w), colour) in KFB_PICTURES.items():
            picture = reader.associated(name)
            assert picture is not None, name
            assert picture.shape == (h, w, 3) and picture.dtype == np.uint8, name
            _close_to(picture, np.full((h, w, 3), colour, np.uint8), name)
    finally:
        reader.close()


def test_a_file_without_pictures_says_so(tmp_path):
    path = write_kfb(tmp_path / "bare.kfb", KfbSlide(pictures=False))
    reader = kfb.reader(str(path))
    try:
        for name in KFB_PICTURES:
            assert reader.associated(name) is None, name
        h, w = reader.levels[0]
        _close_to(reader.read_rgb(0, 0, h, 0, w), kfb_expected(KfbSlide(), 0), "level 0")
    finally:
        reader.close()


def test_there_is_one_image(slide_file):
    path, _slide = slide_file
    assert len(formats.describe(path).images) == 1
    with pytest.raises(FormatUnreadable):
        _open(path, 1)
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path), 1)


# -- damaged and foreign files ---------------------------------------------------


@pytest.mark.parametrize("cut", [64, 5000, 200_000], ids=["record", "index", "tiles"])
def test_a_cut_file_is_refused_not_crashed(tmp_path, cut):
    """The tile index is the last thing in the file: cut, it is incomplete."""
    path = write_kfb(tmp_path / "cut.kfb", KfbSlide(), cut_bytes=cut)
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path))
    with pytest.raises(FormatUnreadable):
        formats.describe(path)


@pytest.mark.parametrize("content", [b"", b"\x00" * 96, b"KFB?" * 100])
def test_what_is_not_a_kfb_is_refused(tmp_path, content):
    path = tmp_path / "x.kfb"
    path.write_bytes(content)
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path))


def test_a_compression_other_than_jpeg_is_refused(tmp_path):
    path = write_kfb(tmp_path / "lzw.kfb", KfbSlide(compression=b"LZW\x00"))
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path))


def test_an_index_offset_past_the_file_is_refused(tmp_path):
    path = write_kfb(tmp_path / "far.kfb", KfbSlide())
    data = bytearray(path.read_bytes())
    data[0x44:0x4C] = (len(data) + 10_000).to_bytes(8, "little")
    path.write_bytes(bytes(data))
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path))


def test_a_corrupt_tile_reads_as_fill_or_is_refused_not_a_decoder_error(tmp_path):
    """Tile record 5's JPEG is garbage after its first 20 bytes (its
    start-of-image marker intact). The tiles around it read as written; it
    reads as fill or FormatUnreadable -- not a JPEG decoder's own exception."""
    import struct

    slide = KfbSlide()
    path = write_kfb(tmp_path / "bad.kfb", slide)
    data = bytearray(path.read_bytes())
    index_at = struct.unpack_from("<Q", data, 0x44)[0]
    record = index_at + 64 * 5
    x, y, tw, th = struct.unpack_from("<4I", data, record + 4)
    size, offset = struct.unpack_from("<Iq", data, record + 32)
    start = index_at + offset
    data[start + 20:start + size] = b"\x11" * (size - 20)
    path.write_bytes(bytes(data))
    reader = kfb.reader(str(path))
    try:
        want = kfb_expected(slide, 0)
        _close_to(reader.read_rgb(0, 0, 100, 0, 100), want[:100, :100], "a good tile")
        try:
            got = reader.read_rgb(0, y, y + th, x, x + tw)
        except FormatUnreadable:
            return
        assert got.shape == (th, tw, 3)
    finally:
        reader.close()


def test_a_header_counting_more_records_than_the_index_holds_is_refused(tmp_path):
    path = write_kfb(tmp_path / "many.kfb", KfbSlide())
    data = bytearray(path.read_bytes())
    count = int.from_bytes(data[0x10:0x14], "little")
    data[0x10:0x14] = (count + 50).to_bytes(4, "little")
    path.write_bytes(bytes(data))
    with pytest.raises(FormatUnreadable):
        kfb.reader(str(path))


# -- threads and the app -----------------------------------------------------------


def test_concurrent_reads_of_one_reader_agree(slide_file):
    path, slide = slide_file
    expected = [kfb_expected(slide, level) for level in range(3)]
    reader = _open(path)
    failures = []
    try:
        def work(seed):
            rng = random.Random(seed)
            for _ in range(20):
                level = rng.randrange(3)
                h, w = expected[level].shape[:2]
                y0, x0 = rng.randrange(h), rng.randrange(w)
                y1, x1 = min(h, y0 + rng.randint(1, 200)), min(w, x0 + rng.randint(1, 300))
                got = reader.read_rgb(level, y0, y1, x0, x1).astype(np.int16)
                if np.abs(got - expected[level][y0:y1, x0:x1]).max() > TOLERANCE:
                    failures.append((level, y0, y1, x0, x1))

        threads = [threading.Thread(target=work, args=(seed,)) for seed in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        reader.close()
    assert not failures, failures[:5]


def test_through_the_package_functions(slide_file):
    path, slide = slide_file
    assert formats.handles(path)
    assert formats.format_name(path) == "KFBio KFB"
    assert formats.is_rgb(path)
    geometry = formats.geometry_of(path)
    assert (geometry["height"], geometry["width"]) == (300, 1300)
    pyramid = formats.open_image(path)
    try:
        assert pyramid.is_color
        assert pyramid.level_shapes[0] == [300, 1300]
        want = kfb_expected(slide, 0)[100:180, 1000:1100]
        for channel in range(3):
            block = np.asarray(pyramid["0"][channel, 100:180, 1000:1100])
            assert block.shape == want.shape[:2]
            assert np.abs(block.astype(np.int16) - want[..., channel]).max() <= TOLERANCE
    finally:
        pyramid.close()
