"""Zeiss CZI through `plexora/server/utils/formats/czi.py`, over a fake `czifile`
(`tests/formats_fakes.py`) that behaves as the real package does on real files:
a scene's array holds every level, a region is a level-0 stage rectangle, and a
rectangle nothing was scanned at raises ValueError.
"""

import sys

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatSupportMissing, FormatUnreadable
from plexora.server.utils.formats.base import Reader
from tests.formats_fakes import (FakeCziImage, colour_pattern, install_czi, plane, tcz, touch)

DIMS = ("T", "C", "Z", "Y", "X", "S")
BBOX = (-247, 598, 48, 40)          # x, y on the stage, negative x
SIZES = dict(T=2, C=3, Z=4, Y=40, X=48, S=1)
LEVELS = [(40, 48), (20, 24), (10, 12)]


def _stack(**kwargs):
    kw = dict(bbox=BBOX, mpp=(0.325, 0.4), channels={"DAPI": 0, "CD3": 1, "CD8": 2},
              coord_scales={"Z": 3.5e-07}, coord_units={"Z": "meter"})
    kw.update(kwargs)
    return FakeCziImage(DIMS, SIZES, LEVELS, **kw)


def _open(monkeypatch, tmp_path, *scenes, name="a.czi"):
    install_czi(monkeypatch, list(scenes))
    return touch(tmp_path, name)


def test_shape_levels_channels_names_and_scale(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    reader = formats.open_reader(path)
    try:
        assert reader.levels == LEVELS
        assert (reader.channels, reader.z_count, reader.t_count) == (3, 4, 2)
        assert reader.dtype == np.dtype(np.uint32)
        assert not reader.rgb
        assert reader.channel_names == ["DAPI", "CD3", "CD8"]
        assert reader.pixel_size == (0.325, 0.4)
        assert reader.z_step == pytest.approx(0.35)
        assert reader.images == ["a"] and reader.note() == ""
    finally:
        reader.close()


def test_the_file_is_opened_unsqueezed_locked_and_closed(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    reader = formats.open_reader(path)
    handle = sys.modules["czifile"].CziFile.instances[-1]
    assert handle.squeeze is False and handle.locked is True and handle.maxcache
    reader.close()
    assert handle.closed


def test_pixel_size_falls_back_to_x_and_is_absent_when_unstated(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(mpp=(0.5, 0)))
    assert formats.pixel_size(path) == (0.5, 0.5)
    other = touch(tmp_path, "b.czi")
    install_czi(monkeypatch, [_stack(mpp=None)])
    assert formats.pixel_size(other) is None
    assert formats.physical_metadata(other) == {}


@pytest.mark.parametrize("scale, unit, expected", [
    (3.5e-07, "meter", 0.35),
    (3.5e-07, "m", 0.35),
    (-3.5e-07, "metre", 0.35),
    (350.0, "nm", 0.35),
    (0.35, "µm", 0.35),
    (3.5e-07, "", 0.35),            # no unit: metres, by its size
    (2.5, "", 2.5),                 # no unit: micrometres, by its size
    (0, "meter", None),
])
def test_z_step_units(monkeypatch, tmp_path, scale, unit, expected):
    path = _open(monkeypatch, tmp_path, _stack(coord_scales={"Z": scale}, coord_units={"Z": unit}))
    got = formats.describe(path).z_step
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_no_z_scale_or_a_single_plane_has_no_step(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(coord_scales={}, coord_units={}))
    assert formats.describe(path).z_step is None
    flat = FakeCziImage(("C", "Y", "X", "S"), dict(C=2, Y=10, X=12, S=1), [(10, 12)],
                        bbox=(0, 0, 12, 10), coord_scales={"Z": 3.5e-07}, coord_units={"Z": "meter"})
    other = touch(tmp_path, "flat.czi")
    install_czi(monkeypatch, [flat])
    assert formats.describe(other).z_step is None
    assert formats.describe(other).z_count == 1


def test_names_empty_or_absent_are_numbered(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(channels={"": 0, "CD3": 1}))
    assert formats.channel_names(path) == ["Channel 1", "CD3", "Channel 3"]
    other = touch(tmp_path, "none.czi")
    install_czi(monkeypatch, [_stack(channels={})])
    assert formats.channel_names(other) == ["Channel 1", "Channel 2", "Channel 3"]


def test_level_zero_region_maps_channel_z_t_and_stage_offset(monkeypatch, tmp_path):
    image = _stack()
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    got = reader.read(0, 2, 5, 12, 7, 20, z=3, t=1)
    np.testing.assert_array_equal(got, plane(tcz(1, 2, 3), 40, 48)[5:12, 7:20])
    level, roi, selection = image.calls[-1]
    assert level == 0
    assert roi == (BBOX[0] + 7, BBOX[1] + 5, 13, 7)
    assert selection == {"T": 1, "C": 2, "Z": 3}
    # every plane is its own
    for c in range(3):
        for z in range(4):
            np.testing.assert_array_equal(reader.read(0, c, 0, 4, 0, 4, z=z, t=0),
                                          plane(tcz(0, c, z), 40, 48)[:4, :4])


def test_edges_of_level_zero(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    reader = formats.open_reader(path)
    full = plane(tcz(0, 0, 0), 40, 48)
    np.testing.assert_array_equal(reader.read(0, 0, 0, 40, 0, 48), full)
    np.testing.assert_array_equal(reader.read(0, 0, 39, 40, 47, 48), full[39:40, 47:48])
    np.testing.assert_array_equal(reader.read(0, 0, 0, 1, 0, 1), full[:1, :1])


def test_coarser_level_region_is_asked_in_level_zero_stage_pixels(monkeypatch, tmp_path):
    image = _stack()
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    got = reader.read(2, 1, 2, 8, 3, 9, z=2, t=1)
    # level 2 is 4x smaller: a 6x6 window is a 24x24 roi at (4*x, 4*y)
    assert image.calls[-1][:2] == (2, (BBOX[0] + 12, BBOX[1] + 8, 24, 24))
    want = plane(tcz(1, 1, 2) + 2 * 100_000, 10, 12)[2:8, 3:9]
    np.testing.assert_array_equal(got, want)
    # the whole of the coarsest level, edge to edge
    whole = reader.read(2, 0, 0, 10, 0, 12)
    np.testing.assert_array_equal(whole, plane(tcz(0, 0, 0) + 200_000, 10, 12))
    reader.close()


NON_EXACT = [(41, 53), (21, 27)]


def _odd(**kwargs):
    kw = dict(bbox=(100, 200, 53, 41), channels={"A": 0})
    kw.update(kwargs)
    return FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=41, X=53, S=1), NON_EXACT, **kw)


def test_non_halving_level_is_read_through_rounded_roi_and_trimmed(monkeypatch, tmp_path):
    image = _odd()
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    assert reader.levels == NON_EXACT
    got = reader.read(1, 0, 4, 10, 3, 9)
    # 41/21 and 53/27 are not 2: the roi is rounded to level-0 pixels
    assert image.calls[-1][:2] == (1, (100 + 6, 200 + 8, 12, 12))
    assert got.shape == (6, 6)
    np.testing.assert_array_equal(got, plane(0 + 100_000, 21, 27)[4:10, 3:9])
    # the level's last row and column are reachable and exactly sized
    whole = reader.read(1, 0, 0, 21, 0, 27)
    np.testing.assert_array_equal(whole, plane(100_000, 21, 27))


def test_non_halving_level_when_czifile_returns_one_pixel_more(monkeypatch, tmp_path):
    image = _odd()
    image.grow = 1
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(1, 0, 4, 10, 3, 9)
    assert got.shape == (6, 6)
    np.testing.assert_array_equal(got, plane(100_000, 21, 27)[4:10, 3:9])


def test_a_level_region_czifile_returns_one_pixel_short_of_is_padded(monkeypatch, tmp_path):
    image = _stack()
    image.shrink = 1
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(1, 0, 4, 10, 3, 9)            # a 6x6 window; the file gives 5x5
    assert got.shape == (6, 6) and got.dtype == np.dtype(np.uint32)
    want = plane(tcz(0, 0, 0) + 100_000, 20, 24)[4:10, 3:9]
    np.testing.assert_array_equal(got[:5, :5], want[:5, :5])
    assert not got[5, :].any() and not got[:, 5].any()       # the missing edge is the fill


def test_a_region_nothing_was_scanned_at_reads_as_fill(monkeypatch, tmp_path):
    image = _stack(holes=[(BBOX[0] + 8, BBOX[1] + 8, 16, 16)])
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(0, 1, 10, 20, 10, 20)           # inside the hole
    assert got.shape == (10, 10) and got.dtype == np.dtype(np.uint32)
    assert not got.any()
    # a region reaching outside the hole is read
    np.testing.assert_array_equal(reader.read(0, 1, 0, 20, 0, 20),
                                  plane(tcz(0, 1, 0), 40, 48)[:20, :20])


def test_unscanned_region_of_a_coarse_level_is_fill_too(monkeypatch, tmp_path):
    image = _stack(holes=[(BBOX[0] + 8, BBOX[1] + 8, 16, 16)])
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(1, 0, 5, 9, 5, 9)               # level 1 -> stage 10..18
    assert got.shape == (4, 4) and not got.any()


def test_other_czi_errors_are_not_swallowed(monkeypatch, tmp_path):
    image = _stack()
    image.error = "corrupt subblock"
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    with pytest.raises(ValueError, match="corrupt"):
        reader.read(0, 0, 0, 4, 0, 4)


def test_extra_axes_are_read_at_their_first_index(monkeypatch, tmp_path):
    dims = ("B", "T", "C", "Y", "X", "S")
    image = FakeCziImage(dims, dict(B=2, T=1, C=2, Y=10, X=12, S=1), [(10, 12)],
                         bbox=(0, 0, 12, 10), channels={"A": 0, "B": 1})
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(0, 1, 0, 10, 0, 12)
    assert image.calls[-1][2] == {"B": 0, "T": 0, "C": 1}
    np.testing.assert_array_equal(got, plane(tcz(0, 1, 0), 10, 12))


def test_an_image_without_levels_is_its_own_only_level(monkeypatch, tmp_path):
    image = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=1), [(10, 12)],
                         bbox=(0, 0, 12, 10), levels=False)
    assert not hasattr(image, "levels")
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    assert reader.levels == [(10, 12)]
    np.testing.assert_array_equal(reader.read(0, 0, 2, 5, 3, 6), plane(0, 10, 12)[2:5, 3:6])


def test_scenes_are_listed_and_the_first_is_shown(monkeypatch, tmp_path):
    first = _stack()
    second = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=1), [(10, 12)],
                          bbox=(0, 0, 12, 10))
    third = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=1), [(10, 12)],
                         bbox=(0, 0, 12, 10))
    path = _open(monkeypatch, tmp_path, first, second, third)
    description = formats.describe(path)
    assert description.images == ["Scene 1", "Scene 2", "Scene 3"]
    assert description.image_index == 0
    assert (description.height, description.width, description.channels) == (40, 48, 3)
    assert formats.note(path) == ('This Zeiss CZI file holds 3 images; Plexora opens "Scene 1" '
                                  'and not the other 2.')


def test_two_scenes_say_the_other_one(monkeypatch, tmp_path):
    a, b = _stack(), _stack()
    path = _open(monkeypatch, tmp_path, a, b)
    assert formats.note(path).endswith('and not the other one.')


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_colour_scan_is_rgb_and_brightfield(monkeypatch, tmp_path, dtype):
    image = FakeCziImage(("Y", "X", "S"), dict(Y=20, X=24, S=3), [(20, 24), (10, 12)],
                         bbox=(-5, -7, 24, 20), dtype=dtype, mpp=(0.25, 0.25))
    path = _open(monkeypatch, tmp_path, image)
    description = formats.describe(path)
    assert description.rgb and description.channels == 3
    assert description.channel_names == ["Red", "Green", "Blue"]
    detection = formats.detect_image_type(path)
    assert detection.verdict == "brightfield" and detection.confidence == "high"
    assert formats.is_rgb(path)
    reader = formats.open_reader(path)
    block = reader.read_rgb(0, 3, 9, 4, 15)
    assert block.dtype == np.uint8 and block.shape == (6, 11, 3)
    np.testing.assert_array_equal(block, colour_pattern(20, 24)[3:9, 4:15])
    for sample in range(3):
        np.testing.assert_array_equal(reader.read(0, sample, 3, 9, 4, 15),
                                      colour_pattern(20, 24)[3:9, 4:15, sample])
    # a coarser level is asked for in level-0 stage pixels
    coarse = reader.read_rgb(1, 1, 4, 2, 6)
    assert image.calls[-1][:2] == (1, (-5 + 4, -7 + 2, 8, 6))
    np.testing.assert_array_equal(coarse, colour_pattern(10, 12, level=1)[1:4, 2:6])


def test_colour_region_nothing_was_scanned_at_is_white(monkeypatch, tmp_path):
    image = FakeCziImage(("Y", "X", "S"), dict(Y=20, X=24, S=3), [(20, 24)],
                         bbox=(0, 0, 24, 20), holes=[(4, 4, 10, 10)])
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    block = reader.read_rgb(0, 5, 10, 5, 10)
    assert block.shape == (5, 5, 3) and (block == 255).all()
    assert (reader.read(0, 2, 5, 10, 5, 10) == 255).all()


def test_colour_with_a_single_channel_axis_is_still_colour(monkeypatch, tmp_path):
    image = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=3), [(10, 12)],
                         bbox=(0, 0, 12, 10), dtype=np.uint8)
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    assert reader.rgb and reader.channels == 3
    np.testing.assert_array_equal(reader.read_rgb(0, 0, 10, 0, 12), colour_pattern(10, 12))


def test_a_scene_with_no_x_or_y_cannot_be_read(monkeypatch, tmp_path):
    image = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=1), [(10, 12)],
                         bbox=(0, 0, 12, 10))
    install_czi(monkeypatch, [image])
    image.dims = ("C", "Y", "S")                 # no X axis
    path = touch(tmp_path, "noxy.czi")
    with pytest.raises(FormatUnreadable, match="no Y and X"):
        formats.open_reader(path)
    assert sys.modules["czifile"].CziFile.instances[-1].closed


def test_a_file_without_scenes_holds_no_image(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path)
    with pytest.raises(FormatUnreadable, match="holds no image"):
        formats.open_reader(path)
    assert sys.modules["czifile"].CziFile.instances[-1].closed


def test_pyramid_at_the_middle_plane_with_its_numbers(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    pyramid = formats.open_image(path)
    try:
        assert pyramid.level_shapes == [[40, 48]]
        assert pyramid[0].shape == (3, 40, 48)
        assert (pyramid.z, pyramid.t) == (2, 0)
        assert pyramid.channel_names == ["DAPI", "CD3", "CD8"]
        assert pyramid.mpp == (0.325, 0.4) and pyramid.z_count == 4 and pyramid.t_count == 2
        np.testing.assert_array_equal(pyramid[0][1, 3:7, 4:9], plane(tcz(0, 1, 2), 40, 48)[3:7, 4:9])
        other = pyramid.plane(z=3, t=1)
        np.testing.assert_array_equal(other[0][2, 0:3, 0:3], plane(tcz(1, 2, 3), 40, 48)[:3, :3])
        assert formats.physical_metadata(pyramid) == {
            "physical_size_x": 0.325, "physical_size_x_unit": "µm",
            "physical_size_y": 0.4, "physical_size_y_unit": "µm",
            "physical_size_z": pytest.approx(0.35), "physical_size_z_unit": "µm"}
    finally:
        pyramid.close()


class _Stub(Reader):
    format_name = "Stub"
    dtype = np.dtype(np.uint16)

    def __init__(self):
        super().__init__()
        self.levels = [(2048, 2560), (1024, 1280)]
        self.channels = 1
        self.channel_names = ["A"]
        self.asked = []

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0):
        self.asked.append((level, y0, y1, x0, x1))
        return np.full((y1 - y0, x1 - x0), 7, dtype=np.uint16)


def test_a_files_own_levels_are_read_where_they_exist():
    from plexora.server.utils.formats import base

    stub = _Stub()
    pyramid = base.build(stub, "x.czi")
    assert pyramid.level_shapes == [[2048, 2560], [1024, 1280], [512, 640]]
    block = pyramid[2][0, 0:512, 0:640]
    assert block.shape == (512, 640)
    assert stub.asked[-1][0] == 1                       # the native level 1, not level 0
    assert stub.asked[-1][1:] == (0, 1024, 0, 1280)


def test_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "czifile", None)
    path = touch(tmp_path, "a.czi")
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(path)
    assert "czifile" in info.value.INSTALL
    assert "pip install" in str(info.value) and "Zeiss CZI" in str(info.value)
