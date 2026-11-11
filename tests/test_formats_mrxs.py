"""3DHISTECH MIRAX (`.mrxs`) fluorescence slides, read from synthetic slides
written to the layout in the docstring of
`plexora/server/utils/formats/mrxs.py` (`formats_fixtures_more.write_mrxs`).

The fixture slide: 3 x 2 photos of 128 x 96 (2 x 2 camera images of 64 x 48
each), overlapping by about 16 pixels with positions off the nominal grid by
up to 8 (some negative), four zoom levels (one camera image a stored image
at level 0; one photo at level 1; 2 x 2 and 4 x 4 photos side by side at
levels 2 and 3), three filters whose storing channel numbers are not their
order, the two data files. Each photo has its own 16 x 16 blocks
(`mrxs_photo`), so a photo pasted at the wrong place, under instead of over
its neighbour, at the wrong scale, or a filter taken from the wrong JPEG
component cannot pass; the stored JPEGs are three raw components, and a
decoder's YCbCr-to-RGB conversion moves them by ~200.
"""

import random
import threading

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, mrxs
from tests.formats_fixtures_more import MrxsSlide, mrxs_expected, write_mrxs

#: the most a raw JPEG component may differ from what was written (quality 100)
TOLERANCE = 2


def _close_to(got, want, what="", tolerance=TOLERANCE):
    assert got.shape == want.shape, f"{what}: shape {got.shape} != {want.shape}"
    diff = np.abs(got.astype(np.int16) - want.astype(np.int16))
    bad = np.argwhere(diff > tolerance)
    assert not len(bad), (f"{what}: {len(bad)} pixels off by up to {diff.max()}, first at "
                          f"{tuple(bad[0])}: {got[tuple(bad[0])]} != {want[tuple(bad[0])]}")


@pytest.fixture(scope="module")
def slide_file(tmp_path_factory):
    slide = MrxsSlide()
    path = write_mrxs(tmp_path_factory.mktemp("mrxs"), slide)
    return path, slide


def _open(path, index=0):
    return formats.open_reader(path, index)


# -- which slides are this reader's ---------------------------------------------


def test_a_fluorescence_slide_is_claimed_and_a_brightfield_one_is_left(tmp_path, slide_file):
    path, _slide = slide_file
    assert mrxs.claims(str(path))
    assert formats.handles(path)
    bright = write_mrxs(tmp_path / "bf", MrxsSlide(slide_type="SLIDE_TYPE_BRIGHTFIELD",
                                                  filters=(("Default", 0),)))
    assert not mrxs.claims(str(bright))
    assert not formats.handles(bright)


@pytest.mark.parametrize("change", [
    dict(image_format="PNG"),
    dict(data_in_filter=("FilterLevel_0", "FilterLevel_1", "FilterLevel_0")),
], ids=["not-jpeg", "a-filter-stored-elsewhere"])
def test_a_fluorescence_slide_laid_out_otherwise_is_not_claimed(tmp_path, change):
    path = write_mrxs(tmp_path, MrxsSlide(**change))
    assert not mrxs.claims(str(path))
    assert not formats.handles(path)


def test_a_level_that_does_not_halve_is_refused_or_read_at_its_own_scale(tmp_path):
    """IMAGE_CONCAT_FACTOR 2 at level 2: a quarter of level 1, not a half.
    `claims` names halving levels as a condition; a slide it claims anyway
    must be read at the scale its levels state."""
    slide = MrxsSlide(concat=(0, 1, 2), levels=3)
    path = write_mrxs(tmp_path, slide)
    if not mrxs.claims(str(path)):
        return
    reader = _open(path)
    try:
        for level in range(3):
            want = mrxs_expected(slide, level)
            h, w = reader.levels[level]
            assert (h, w) == want.shape[1:], f"level {level}"
            _close_to(reader.read(level, 0, 0, h, 0, w), want[0], f"level {level}")
    finally:
        reader.close()


def test_a_slide_without_its_folder_is_nobodys(tmp_path, slide_file):
    lonely = tmp_path / "lonely.mrxs"
    lonely.write_bytes(b"x" * 100)
    assert not mrxs.claims(str(lonely))
    assert not formats.handles(lonely)
    with pytest.raises(FormatUnreadable):
        mrxs.reader(str(lonely))


# -- the stitched levels ---------------------------------------------------------


def test_levels_are_the_photos_bounding_box_halved(slide_file):
    path, slide = slide_file
    reader = _open(path)
    try:
        full = mrxs_expected(slide, 0).shape[1:]
        # x from -32 (photo (0, 1)) to 208 + 128; y from 1000 to 1088 + 96
        assert full == (184, 368)
        assert reader.levels == [(full[0] >> k, full[1] >> k) for k in range(slide.levels)]
        assert reader.channels == 3 and not reader.rgb
        assert reader.dtype == np.dtype(np.uint8)
    finally:
        reader.close()


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_every_level_is_the_photos_pasted_at_their_positions(slide_file, level):
    path, slide = slide_file
    want = mrxs_expected(slide, level)
    reader = _open(path)
    try:
        h, w = reader.levels[level]
        assert (h, w) == want.shape[1:]
        for channel in range(3):
            _close_to(reader.read(level, channel, 0, h, 0, w), want[channel],
                      f"level {level} channel {channel}")
    finally:
        reader.close()


def test_filters_are_named_in_order_and_read_from_their_storing_component(slide_file):
    path, slide = slide_file
    assert formats.channel_names(path) == ["Rhodamine", "FITC", "UV"]
    reader = _open(path)
    try:
        assert reader.channel_names == ["Rhodamine", "FITC", "UV"]
        want = mrxs_expected(slide, 1)
        h, w = reader.levels[1]
        # Rhodamine is stored in component 2, FITC in 0, UV in 1
        for channel in range(3):
            got = reader.read(1, channel, 0, h, 0, w)
            _close_to(got, want[channel], f"channel {channel}")
            for other in set(range(3)) - {channel}:
                assert np.abs(got.astype(int) - want[other]).max() > 20
    finally:
        reader.close()


def test_regions_across_photo_seams_and_overlaps(slide_file):
    path, slide = slide_file
    reader = _open(path)
    try:
        for level in (0, 1):
            want = mrxs_expected(slide, level)
            h, w = reader.levels[level]
            rng = random.Random(level)
            cases = [(0, 1, 0, 1), (h - 1, h, w - 1, w), (0, h, w // 3 - 5, w // 3 + 5),
                     (h // 2 - 7, h // 2 + 7, 0, w)]
            for _ in range(20):
                y0, x0 = rng.randrange(h), rng.randrange(w)
                cases.append((y0, rng.randint(y0 + 1, h), x0, rng.randint(x0 + 1, w)))
            for y0, y1, x0, x1 in cases:
                channel = rng.randrange(3)
                _close_to(reader.read(level, channel, y0, y1, x0, x1),
                          want[channel, y0:y1, x0:x1],
                          f"level {level} c{channel} [{y0}:{y1}, {x0}:{x1}]")
    finally:
        reader.close()


def test_the_bounding_box_corners_no_photo_covers_read_zero(slide_file):
    path, slide = slide_file
    want = mrxs_expected(slide, 0)
    assert (want[:, :8, :8] == 0).all()              # the fixture leaves a corner uncovered
    reader = _open(path)
    try:
        for channel in range(3):
            assert not reader.read(0, channel, 0, 8, 0, 8).any()
    finally:
        reader.close()


def test_pixel_size_is_level_zeros(slide_file):
    path, _slide = slide_file
    description = formats.describe(path)
    # the registry calls it "3DHISTECH MRXS", the reader "3DHISTECH MIRAX"
    assert description.format_name in ("3DHISTECH MRXS", "3DHISTECH MIRAX")
    assert description.pixel_size == (pytest.approx(0.3225), pytest.approx(0.3226))
    assert description.channels == 3 and not description.rgb
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "fluorescence" and verdict.confidence == "high"


# -- where the photos are ------------------------------------------------------------


def test_an_unscanned_photo_is_left_out_at_every_level(tmp_path):
    """Photo (1, 1), in the middle of the bottom row, was not scanned: no
    level-0 images of it, flag 0 and (0, 0) in the position buffer, blank in
    the coarser stored images that hold it with its neighbours."""
    slide = MrxsSlide(unscanned=frozenset({(1, 1)}))
    path = write_mrxs(tmp_path, slide)
    reader = _open(path)
    try:
        for level in range(slide.levels):
            want = mrxs_expected(slide, level)
            h, w = reader.levels[level]
            assert (h, w) == want.shape[1:], f"level {level}"
            for channel in range(3):
                _close_to(reader.read(level, channel, 0, h, 0, w), want[channel],
                          f"level {level} channel {channel}")
    finally:
        reader.close()


def test_a_slide_without_a_position_buffer_lies_on_the_nominal_grid(tmp_path):
    slide = MrxsSlide(position_buffer="none")
    path = write_mrxs(tmp_path, slide)
    assert mrxs.claims(str(path))
    reader = _open(path)
    try:
        for level in (0, 2):
            want = mrxs_expected(slide, level, slide.nominal())
            h, w = reader.levels[level]
            assert (h, w) == want.shape[1:]
            _close_to(reader.read(level, 1, 0, h, 0, w), want[1], f"level {level}")
    finally:
        reader.close()


def test_a_version_2_2_slide_reads_its_compressed_positions(tmp_path):
    slide = MrxsSlide(position_buffer="deflate", version="2.2")
    path = write_mrxs(tmp_path, slide)
    reader = _open(path)
    try:
        for level in (0, 1):
            want = mrxs_expected(slide, level, MrxsSlide().placed())
            h, w = reader.levels[level]
            assert (h, w) == want.shape[1:]
            _close_to(reader.read(level, 0, 0, h, 0, w), want[0], f"level {level}")
    finally:
        reader.close()


def test_one_photo_per_stored_image_at_level_0_of_a_slide_divided_once(tmp_path):
    """CameraImageDivisionsPerSide 1: a photo is one camera image."""
    slide = MrxsSlide(div=1, dw=128, dh=96, photos_x=4, photos_y=2, levels=3)
    path = write_mrxs(tmp_path, slide)
    reader = _open(path)
    try:
        for level in range(3):
            want = mrxs_expected(slide, level)
            h, w = reader.levels[level]
            assert (h, w) == want.shape[1:], f"level {level}"
            _close_to(reader.read(level, 2, 0, h, 0, w), want[2], f"level {level}")
    finally:
        reader.close()


# -- brightfield, one image, damage ---------------------------------------------------


def test_a_brightfield_slide_still_opens_as_a_colour_picture(tmp_path):
    slide = MrxsSlide(slide_type="SLIDE_TYPE_BRIGHTFIELD", filters=(("Default", 0),))
    path = write_mrxs(tmp_path, slide)
    reader = mrxs.reader(str(path))
    try:
        assert reader.rgb
        want = np.moveaxis(mrxs_expected(slide, 0), 0, -1)
        h, w = reader.levels[0]
        assert (h, w) == want.shape[:2]
        _close_to(reader.read_rgb(0, 0, h, 0, w), want, "brightfield RGB", tolerance=4)
    finally:
        reader.close()


def test_there_is_one_image(slide_file):
    path, _slide = slide_file
    assert len(formats.describe(path).images) == 1
    with pytest.raises(FormatUnreadable):
        _open(path, 1)
    with pytest.raises(FormatUnreadable):
        mrxs.reader(str(path), 1)


@pytest.mark.parametrize("keep", [20, 41, 45, 200],
                         ids=["in-the-slide-id", "in-the-root-offsets", "header-only", "in-pages"])
def test_a_cut_index_is_refused_not_crashed(tmp_path, keep):
    """Index.dat: 5-byte version, 32-byte slide id, two int32 root offsets
    (45 bytes), the root tables, the page chains. Cut anywhere, the slide is
    one this reader cannot serve -- not a struct.error."""
    path = write_mrxs(tmp_path, MrxsSlide())
    index = tmp_path / "slide" / "Index.dat"
    index.write_bytes(index.read_bytes()[:keep])
    with pytest.raises(FormatUnreadable):
        mrxs.reader(str(path))


@pytest.mark.parametrize("keep", ["header", "half"])
def test_images_a_cut_data_file_lost_read_as_zeros_or_are_refused(tmp_path, keep):
    """Data0000.dat (level 0) cut to its 296-byte header, or to half. Opening
    may refuse the slide; if it opens, a level-0 read is what was written
    where the file still holds it and zeros or FormatUnreadable where it does
    not -- not a JPEG decoder's own exception -- and level 1, in the other
    data file, reads as written."""
    slide = MrxsSlide()
    path = write_mrxs(tmp_path, slide)
    data = tmp_path / "slide" / "Data0000.dat"
    whole = data.read_bytes()
    data.write_bytes(whole[:300] if keep == "header" else whole[:len(whole) // 2])
    try:
        reader = _open(path)
    except FormatUnreadable:
        return
    try:
        h, w = reader.levels[1]
        _close_to(reader.read(1, 0, 0, h, 0, w), mrxs_expected(slide, 1)[0], "level 1")
        h, w = reader.levels[0]
        try:
            got = reader.read(0, 0, 0, h, 0, w)
        except FormatUnreadable:
            return
        assert got.shape == (h, w)
        want = mrxs_expected(slide, 0)[0]
        assert ((np.abs(got.astype(int) - want) <= TOLERANCE) | (got == 0)).all()
    finally:
        reader.close()


def test_a_missing_index_is_refused(tmp_path):
    path = write_mrxs(tmp_path, MrxsSlide())
    (tmp_path / "slide" / "Index.dat").unlink()
    with pytest.raises(FormatUnreadable):
        mrxs.reader(str(path))


def test_file_names_are_matched_without_case(tmp_path):
    """Slides written on Windows are read on case-sensitive file systems."""
    slide = MrxsSlide()
    path = write_mrxs(tmp_path, slide)
    folder = tmp_path / "slide"
    (folder / "Data0000.dat").rename(folder / "DATA0000.DAT")
    (folder / "Index.dat").rename(folder / "index.dat")
    reader = mrxs.reader(str(path))
    try:
        h, w = reader.levels[0]
        _close_to(reader.read(0, 0, 0, h, 0, w), mrxs_expected(slide, 0)[0], "level 0")
    finally:
        reader.close()


# -- threads and the app -----------------------------------------------------------


def test_concurrent_reads_of_one_reader_agree(slide_file):
    path, slide = slide_file
    expected = [mrxs_expected(slide, level) for level in range(slide.levels)]
    reader = _open(path)
    failures = []
    try:
        def work(seed):
            rng = random.Random(seed)
            for _ in range(25):
                level, channel = rng.randrange(slide.levels), rng.randrange(3)
                h, w = expected[level].shape[1:]
                y0, x0 = rng.randrange(h), rng.randrange(w)
                y1, x1 = rng.randint(y0 + 1, h), rng.randint(x0 + 1, w)
                got = reader.read(level, channel, y0, y1, x0, x1).astype(np.int16)
                if np.abs(got - expected[level][channel, y0:y1, x0:x1]).max() > TOLERANCE:
                    failures.append((level, channel, y0, y1, x0, x1))

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
    assert formats.format_name(path) == "3DHISTECH MRXS"
    geometry = formats.geometry_of(path)
    assert (geometry["height"], geometry["width"], geometry["num_channels"]) == (184, 368, 3)
    pyramid = formats.open_image(path)
    try:
        assert not pyramid.is_color
        assert pyramid.channel_names == ["Rhodamine", "FITC", "UV"]
        assert pyramid.level_shapes[0] == [184, 368]
        _close_to(np.asarray(pyramid["0"][1]), mrxs_expected(slide, 0)[1], "pyramid level 0")
    finally:
        pyramid.close()
