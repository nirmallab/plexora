"""Leica LIF/LOF/XLEF through `plexora/server/utils/formats/lif.py`, over a fake
`liffile`: images with dims/sizes/coords in metres, `.frame(**indices)` and a
tile scan whose stage positions are in metres.
"""

import sys

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatSupportMissing, FormatUnreadable
from tests.formats_fakes import (FakeLifImage, colour_pattern, install_lif, lif_key, plane,
                                 tilescan, touch)

H, W = 14, 18


def _coords(n_x=W, n_y=H, step_x=1.3e-6, step_y=None, z=None, channels=None):
    coords = {"X": np.arange(n_x) * step_x, "Y": np.arange(n_y) * (step_y or step_x)}
    if z is not None:
        coords["Z"] = np.asarray(z)
    if channels is not None:
        coords["C"] = np.asarray(channels)
    return coords


def _stack(**kwargs):
    kw = dict(dims=("T", "M", "Z", "C", "Y", "X"), sizes=dict(T=2, M=1, Z=4, C=3, Y=H, X=W),
              coords=_coords(z=[0, 2e-6, 4e-6, 6e-6], channels=["DAPI", "FITC", "TRITC"]))
    kw.update(kwargs)
    return FakeLifImage("Series 1", kw["dims"], kw["sizes"], coords=kw["coords"],
                        tilescan=kw.get("tilescan"), dtype=kw.get("dtype", np.uint32))


def _open(monkeypatch, tmp_path, *images, name="a.lif"):
    install_lif(monkeypatch, list(images))
    return touch(tmp_path, name)


def test_shape_channels_names_and_scale(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    reader = formats.open_reader(path)
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (3, 4, 2)
        assert reader.channel_names == ["DAPI", "FITC", "TRITC"]
        assert reader.pixel_size == (pytest.approx(1.3), pytest.approx(1.3))     # metres -> µm
        assert reader.z_step == pytest.approx(2.0)
        assert reader.format_name == "Leica LIF"
        assert reader.images == ["Series 1"] and reader.note() == ""
    finally:
        reader.close()
    assert sys.modules["liffile"].LifFile.instances[-1].closed
    assert sys.modules["liffile"].LifFile.instances[-1].locked is True


def test_pixel_size_has_its_own_y_and_falls_back_to_x(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(coords=_coords(step_x=1e-6, step_y=2e-6)))
    assert formats.pixel_size(path) == (pytest.approx(1.0), pytest.approx(2.0))
    other = touch(tmp_path, "b.lif")
    install_lif(monkeypatch, [_stack(coords={"X": np.arange(W) * 1e-6})])
    assert formats.pixel_size(other) == (pytest.approx(1.0), pytest.approx(1.0))


def test_a_file_without_coordinates_states_no_scale(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(coords={}))
    description = formats.describe(path)
    assert description.pixel_size is None and description.z_step is None
    assert formats.physical_metadata(path) == {}
    assert description.channel_names == ["Channel 1", "Channel 2", "Channel 3"]
    other = touch(tmp_path, "b.lif")
    install_lif(monkeypatch, [_stack(coords=None)])
    assert formats.describe(other).pixel_size is None


def test_a_single_plane_or_a_single_z_value_has_no_step(monkeypatch, tmp_path):
    image = _stack(dims=("C", "Y", "X"), sizes=dict(C=2, Y=H, X=W),
                   coords=_coords(z=[0, 2e-6], channels=["A", "B"]))
    path = _open(monkeypatch, tmp_path, image)
    assert formats.describe(path).z_step is None
    other = touch(tmp_path, "b.lif")
    install_lif(monkeypatch, [_stack(coords=_coords(z=[0.0]))])
    assert formats.describe(other).z_step is None


@pytest.mark.parametrize("names, expected", [
    (["Cy5", "Cy5", ""], ["Cy5", "Cy5 (2)", "Channel 3"]),
    (["A", "B"], ["A", "B", "Channel 3"]),
])
def test_duplicate_empty_or_missing_names_are_numbered(monkeypatch, tmp_path, names, expected):
    path = _open(monkeypatch, tmp_path, _stack(coords=_coords(channels=names)))
    assert formats.channel_names(path) == expected


def test_reads_map_channel_z_t_and_m(monkeypatch, tmp_path):
    image = _stack()
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    for t in range(2):
        for c in range(3):
            for z in range(4):
                got = reader.read(0, c, 2, 9, 3, 15, z=z, t=t)
                np.testing.assert_array_equal(got, plane(lif_key(0, c, z, t), H, W)[2:9, 3:15])
    assert image.frame_calls[-1] == {"M": 0, "C": 2, "Z": 3, "T": 1}


def test_edges(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    reader = formats.open_reader(path)
    full = plane(lif_key(0, 1, 0, 0), H, W)
    np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), full)
    np.testing.assert_array_equal(reader.read(0, 1, H - 1, H, W - 1, W), full[-1:, -1:])


def test_other_dimensions_are_read_at_index_zero(monkeypatch, tmp_path):
    image = _stack(dims=("L", "C", "Y", "X"), sizes=dict(L=2, C=2, Y=H, X=W),
                   coords=_coords(channels=["A", "B"]))
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(0, 1, 0, H, 0, W)
    assert image.frame_calls[-1] == {"L": 0, "C": 1}
    np.testing.assert_array_equal(got, plane(lif_key(0, 1, 0, 0), H, W))


def test_images_are_listed_filtered_and_named(monkeypatch, tmp_path):
    a = _stack()
    nothing = FakeLifImage("Folder", (), {})
    b = FakeLifImage("", ("C", "Y", "X"), dict(C=1, Y=H, X=W))
    c = FakeLifImage(None, ("C", "Y", "X"), dict(C=1, Y=H, X=W))
    path = _open(monkeypatch, tmp_path, nothing, a, b, c)
    description = formats.describe(path)
    assert description.images == ["Series 1", "Image 2", "Image 3"]
    assert description.channels == 3                    # the first image with Y and X is shown
    assert formats.note(path) == ('This Leica LIF file holds 3 images; Plexora opens "Series 1" '
                                  'and not the other 2.')


def test_two_images_say_the_other_one(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack(), _stack())
    assert formats.note(path).endswith('and not the other one.')


@pytest.mark.parametrize("suffix, name", [(".lif", "Leica LIF"), (".lof", "Leica LOF"),
                                          (".xlef", "Leica XLEF"), (".LIF", "Leica LIF")])
def test_the_format_name_follows_the_suffix(monkeypatch, tmp_path, suffix, name):
    path = _open(monkeypatch, tmp_path, _stack(), name="a" + suffix)
    assert formats.describe(path).format_name == name


def test_no_image_is_an_error_and_closes_the_file(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, FakeLifImage("Folder", (), {}))
    with pytest.raises(FormatUnreadable, match="holds no image"):
        formats.open_reader(path)
    assert sys.modules["liffile"].LifFile.instances[-1].closed
    empty = _open(monkeypatch, tmp_path, name="b.lif")
    with pytest.raises(FormatUnreadable):
        formats.open_reader(empty)


# -- tile scans --------------------------------------------------------------

TH, TW = 4, 6
PIXEL_UM = 0.5


def _tiles(positions, **flags):
    return FakeLifImage(
        "Tiles", ("M", "Y", "X"), dict(M=len(positions), Y=TH, X=TW),
        coords=_coords(n_x=TW, n_y=TH, step_x=PIXEL_UM * 1e-6),
        tilescan=tilescan(positions, PIXEL_UM, **flags))


def _paint(offsets, height, width, channel=0):
    """The mosaic the placement means: tile m's frame at (y, x), later over earlier."""
    out = np.zeros((height, width), dtype=np.uint32)
    for m, (y, x) in enumerate(offsets):
        out[y:y + TH, x:x + TW] = plane(lif_key(m, channel, 0, 0), TH, TW)
    return out


def _check_mosaic(monkeypatch, tmp_path, image, offsets, height, width):
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    assert reader.levels == [(height, width)]
    want = _paint(offsets, height, width)
    np.testing.assert_array_equal(reader.read(0, 0, 0, height, 0, width), want)
    # a window across tile boundaries, away from the origin
    np.testing.assert_array_equal(reader.read(0, 0, 2, height - 1, 3, width - 2),
                                  want[2:height - 1, 3:width - 2])
    return reader


def test_tiles_are_placed_by_stage_position_and_overlap_later_over_earlier(monkeypatch, tmp_path):
    # px (x, y): tile 1 is 4 right of tile 0 (2 columns overlap), tile 2 is 3 below (1 row)
    image = _tiles([(0, 0), (4, 0), (0, 3)])
    _check_mosaic(monkeypatch, tmp_path, image, [(0, 0), (0, 4), (3, 0)], 7, 10)
    # where tile 1 overlaps tile 0 the later tile shows
    reader = formats.open_reader(touch(tmp_path, "again.lif"))
    got = reader.read(0, 0, 0, 1, 4, 6)
    np.testing.assert_array_equal(got, plane(lif_key(1, 0, 0, 0), TH, TW)[0:1, 0:2])
    # and a corner nothing covers is empty
    assert not reader.read(0, 0, 4, 7, 6, 10).any()


def test_flip_x_mirrors_the_stage_x(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (4, 0), (0, 3)], flip_x=True)
    _check_mosaic(monkeypatch, tmp_path, image, [(0, 4), (0, 0), (3, 4)], 7, 10)


def test_flip_y_mirrors_the_stage_y(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (4, 0), (0, 3)], flip_y=True)
    _check_mosaic(monkeypatch, tmp_path, image, [(3, 0), (3, 4), (0, 0)], 7, 10)


def test_swap_xy_exchanges_the_stage_axes(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (4, 0), (0, 3)], swap_xy=True)
    # stage (x, y) = (0,0), (4,0), (0,3) read as (y, x): tile 1 is 4 down, tile 2 is 3 right
    _check_mosaic(monkeypatch, tmp_path, image, [(0, 0), (4, 0), (0, 3)], 8, 9)


def test_tiles_not_on_a_grid_are_rounded_to_the_nearest_pixel(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (3.6, 0.4)])
    _check_mosaic(monkeypatch, tmp_path, image, [(0, 0), (0, 4)], 4, 10)


def test_a_read_touches_only_the_tiles_it_covers(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (8, 0), (0, 8)])
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    reader.read(0, 0, 0, 3, 0, 5)
    assert [call["M"] for call in image.frame_calls] == [0]


def test_a_mosaic_channel_z_t_reach_the_frames(monkeypatch, tmp_path):
    image = FakeLifImage("Tiles", ("T", "M", "Z", "C", "Y", "X"),
                         dict(T=2, M=2, Z=3, C=2, Y=TH, X=TW),
                         coords=_coords(n_x=TW, n_y=TH, step_x=PIXEL_UM * 1e-6),
                         tilescan=tilescan([(0, 0), (4, 0)], PIXEL_UM))
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    got = reader.read(0, 1, 0, TH, 0, 10, z=2, t=1)
    want = np.zeros((TH, 10), dtype=np.uint32)
    want[:, 0:6] = plane(lif_key(0, 1, 2, 1), TH, TW)
    want[:, 4:10] = plane(lif_key(1, 1, 2, 1), TH, TW)
    np.testing.assert_array_equal(got, want)


def test_a_tile_scan_without_positions_shows_the_first_tile(monkeypatch, tmp_path):
    image = _tiles([(0, 0), (0, 0), (0, 0)])               # every position the same: none stated
    path = _open(monkeypatch, tmp_path, image)
    reader = formats.open_reader(path)
    assert reader.levels == [(TH, TW)]
    np.testing.assert_array_equal(reader.read(0, 0, 0, TH, 0, TW), plane(lif_key(0, 0, 0, 0), TH, TW))


@pytest.mark.parametrize("make", ["none", "short", "no_pixel_size"])
def test_tiles_that_cannot_be_placed_show_the_first_tile(monkeypatch, tmp_path, make):
    image = _tiles([(0, 0), (4, 0), (0, 3)])
    if make == "none":
        image.tilescan = None
    elif make == "short":
        image.tilescan = tilescan([(0, 0), (4, 0)], PIXEL_UM)       # 2 positions for 3 tiles
    else:
        image.coords = {}
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    assert reader.levels == [(TH, TW)]
    np.testing.assert_array_equal(reader.read(0, 0, 0, TH, 0, TW), plane(lif_key(0, 0, 0, 0), TH, TW))


def test_a_single_tile_is_not_a_mosaic(monkeypatch, tmp_path):
    image = _tiles([(5, 5)])
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    assert reader.levels == [(TH, TW)]


# -- colour ------------------------------------------------------------------


def test_a_colour_image_is_rgb_and_brightfield(monkeypatch, tmp_path):
    image = FakeLifImage("HE", ("Y", "X", "S"), dict(Y=H, X=W, S=3), dtype=np.uint8,
                         coords=_coords(step_x=0.5e-6))
    path = _open(monkeypatch, tmp_path, image)
    description = formats.describe(path)
    assert description.rgb and description.channels == 3
    assert description.channel_names == ["Red", "Green", "Blue"]
    assert formats.detect_image_type(path).verdict == "brightfield"
    reader = formats.open_reader(path)
    block = reader.read_rgb(0, 2, 9, 3, 15)
    assert block.dtype == np.uint8 and block.shape == (7, 12, 3)
    np.testing.assert_array_equal(block, colour_pattern(H, W)[2:9, 3:15])
    np.testing.assert_array_equal(reader.read(0, 2, 2, 9, 3, 15), colour_pattern(H, W)[2:9, 3:15, 2])
    assert all("C" not in call for call in image.frame_calls)


def test_a_colour_image_of_sixteen_bits_is_brought_to_eight(monkeypatch, tmp_path):
    image = FakeLifImage("HE", ("C", "Y", "X", "S"), dict(C=1, Y=H, X=W, S=3), dtype=np.uint16)
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    block = reader.read_rgb(0, 0, H, 0, W)
    assert block.dtype == np.uint8
    np.testing.assert_array_equal(block, colour_pattern(H, W))


def test_a_colour_tile_scan_is_placed_too(monkeypatch, tmp_path):
    image = FakeLifImage("HE", ("M", "Y", "X", "S"), dict(M=2, Y=TH, X=TW, S=3), dtype=np.uint8,
                         coords=_coords(n_x=TW, n_y=TH, step_x=PIXEL_UM * 1e-6),
                         tilescan=tilescan([(0, 0), (4, 0)], PIXEL_UM))
    reader = formats.open_reader(_open(monkeypatch, tmp_path, image))
    assert reader.levels == [(TH, 10)]
    got = reader.read_rgb(0, 0, TH, 0, 10)
    want = np.zeros((TH, 10, 3), dtype=np.uint8)
    want[:, 0:6] = colour_pattern(TH, TW, shift=0)
    want[:, 4:10] = colour_pattern(TH, TW, shift=31)
    np.testing.assert_array_equal(got, want)


def test_pyramid_of_a_stack_at_its_middle_plane(monkeypatch, tmp_path):
    path = _open(monkeypatch, tmp_path, _stack())
    pyramid = formats.open_image(path)
    assert pyramid[0].shape == (3, H, W) and (pyramid.z, pyramid.t) == (2, 0)
    np.testing.assert_array_equal(pyramid[0][2, 1:5, 2:7], plane(lif_key(0, 2, 2, 0), H, W)[1:5, 2:7])
    assert formats.physical_metadata(pyramid)["physical_size_z"] == pytest.approx(2.0)


def test_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "liffile", None)
    path = touch(tmp_path, "a.lif")
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(path)
    assert "liffile" in info.value.INSTALL and "pip install" in str(info.value)
    assert "Leica LIF" in str(info.value)
