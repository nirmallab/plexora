"""Olympus VSI + ETS, read from synthetic files written to the layout in the
docstring of `plexora/server/utils/formats/vsi.py`.

`formats_fixtures.vsi_value` gives every (level, channel, z, y, x) its own
number, so a read from the wrong tile, level, channel or plane cannot pass.
"""

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable
from tests.formats_fixtures import VsiStack, vsi_expected, vsi_value, write_vsi


def _reader(path):
    return formats.open_reader(path)


def _full(stack, level, c=0, z=0):
    h = -(-stack.height // (1 << level))
    w = -(-stack.width // (1 << level))
    return vsi_expected(stack, level, c, z, 0, h, 0, w)


# -- the pyramid ----------------------------------------------------------


def test_a_two_level_pyramid_with_a_ragged_edge(tmp_path):
    stack = VsiStack(2, height=601, width=1001, tile=256, dims=[(4, 2)])
    reader = _reader(write_vsi(tmp_path, stacks=[stack]))
    try:
        # ceil-halved, not floor-halved: 601 -> 301, 1001 -> 501
        assert reader.levels == [(601, 1001), (301, 501)]
        assert reader.channels == 2 and reader.z_count == 1 and reader.t_count == 1
        assert reader.dtype == np.dtype(np.uint16)
        assert not reader.rgb
        for level in (0, 1):
            for c in (0, 1):
                full = _full(stack, level, c)
                got = reader.read(level, c, 0, full.shape[0], 0, full.shape[1])
                assert got.shape == full.shape and got.dtype == np.uint16
                np.testing.assert_array_equal(got, full)
    finally:
        reader.close()


def test_reads_at_the_right_and_bottom_edge_have_the_right_shape_and_values(tmp_path):
    stack = VsiStack(2, height=601, width=1001, tile=256, dims=[(4, 2)])
    reader = _reader(write_vsi(tmp_path, stacks=[stack]))
    try:
        # the last tile column (768..1000) and row (512..600) are partial
        got = reader.read(0, 1, 590, 601, 990, 1001)
        assert got.shape == (11, 11)
        np.testing.assert_array_equal(got, vsi_expected(stack, 0, 1, 0, 590, 601, 990, 1001))
        # a rectangle straddling the corner of four tiles
        got = reader.read(0, 0, 250, 262, 250, 262)
        np.testing.assert_array_equal(got, vsi_expected(stack, 0, 0, 0, 250, 262, 250, 262))
        # level 1, bottom-right corner
        got = reader.read(1, 1, 290, 301, 490, 501)
        assert got.shape == (11, 11)
        np.testing.assert_array_equal(got, vsi_expected(stack, 1, 1, 0, 290, 301, 490, 501))
        # one pixel
        assert int(reader.read(0, 0, 600, 601, 1000, 1001)[0, 0]) == int(
            vsi_value(0, 0, 0, np.array([600]), np.array([1000]))[0, 0])
    finally:
        reader.close()


def test_the_format_pyramid_maps_dyadic_levels_to_native_ones(tmp_path):
    stack = VsiStack(2, height=1500, width=2100, tile=512, dims=[(4, 1)], channel_names=("Only",))
    path = write_vsi(tmp_path, stacks=[stack])
    pyramid = formats.open_image(path)
    try:
        shapes = pyramid.level_shapes
        # halving chain to one tile: 1500x2100, 750x1050, 375x525
        assert shapes == [[1500, 2100], [750, 1050], [375, 525]]
        np.testing.assert_array_equal(pyramid["0"][0, 1400:1500, 2000:2100],
                                      vsi_expected(stack, 0, 0, 0, 1400, 1500, 2000, 2100))
        # the native level 1 is exactly the second dyadic level
        np.testing.assert_array_equal(pyramid["1"][0, 700:750, 1000:1050],
                                      vsi_expected(stack, 1, 0, 0, 700, 750, 1000, 1050))
    finally:
        pyramid.close()


# -- what the tree says ---------------------------------------------------


def test_channel_names_pixel_size_and_the_description(tmp_path):
    stack = VsiStack(2, channel_names=("DAPI", "Cy5"), pixel_size=(0.325, 0.33))
    path = write_vsi(tmp_path, stacks=[stack])
    description = formats.describe(path)
    assert description.format_name == "Olympus VSI"
    assert description.channel_names == ["DAPI", "Cy5"]
    assert description.pixel_size == (pytest.approx(0.325), pytest.approx(0.33))
    assert (description.height, description.width) == (601, 1001)
    assert description.level_shapes == [[601, 1001], [301, 501]]
    assert description.channels == 2 and not description.rgb
    assert formats.channel_names(path) == ["DAPI", "Cy5"]
    assert formats.physical_metadata(path)["physical_size_x"] == pytest.approx(0.325)


def test_repeated_or_missing_channel_names_are_made_distinct(tmp_path):
    stack = VsiStack(2, channel_names=("Cy5", "Cy5"))
    assert formats.channel_names(write_vsi(tmp_path / "a", stacks=[stack])) == [
        "Cy5", "Cy5 (2)"]
    plain = VsiStack(2, dims=[])
    assert formats.channel_names(write_vsi(tmp_path / "b", stacks=[plain])) == ["Channel 1"]


def test_a_stack_with_no_pixel_size_states_none(tmp_path):
    path = write_vsi(tmp_path, stacks=[VsiStack(2, pixel_size=None)])
    assert formats.pixel_size(path) is None
    assert formats.physical_metadata(path) == {}


# -- missing tiles ----------------------------------------------------------


def test_a_missing_tile_reads_as_the_background(tmp_path):
    stack = VsiStack(2, height=601, width=1001, tile=256, background=77,
                     missing={(0, 1, 2)})
    reader = _reader(write_vsi(tmp_path, stacks=[stack]))
    try:
        got = reader.read(0, 0, 0, 601, 0, 1001)
        want = _full(stack, 0, 0).copy()
        want[256:512, 512:768] = 77
        np.testing.assert_array_equal(got, want)
        # a region wholly inside the missing tile
        assert (reader.read(0, 1, 300, 310, 520, 530) == 77).all()
        # the neighbouring tile of the other channel is present
        np.testing.assert_array_equal(reader.read(0, 1, 300, 310, 770, 780),
                                      vsi_expected(stack, 0, 1, 0, 300, 310, 770, 780))
    finally:
        reader.close()


# -- which stack is shown ---------------------------------------------------


@pytest.mark.parametrize("skipped", ["Overview", "Label"])
def test_an_overview_or_label_stack_is_skipped_in_favour_of_the_scan(tmp_path, skipped):
    small = VsiStack(1, name=skipped, height=100, width=120, tile=64, dims=[(4, 1)],
                     channel_names=("junk",), levels=1, pyramid=False)
    scan = VsiStack(2, name="Region 1", height=300, width=400, tile=128, dims=[(4, 2)],
                    channel_names=("DAPI", "FITC"))
    path = write_vsi(tmp_path, stacks=[small, scan])
    description = formats.describe(path)
    assert description.images == ["Region 1"]
    assert (description.height, description.width) == (300, 400)
    assert description.channel_names == ["DAPI", "FITC"]
    assert description.note == ""


def test_two_scans_list_both_and_the_first_is_opened(tmp_path):
    overview = VsiStack(1, name="Overview", height=100, width=120, tile=64, dims=[(4, 1)],
                        channel_names=("junk",), levels=1, pyramid=False)
    first = VsiStack(2, name="Scan A", height=300, width=400, tile=128, dims=[(4, 2)],
                     channel_names=("A1", "A2"))
    second = VsiStack(3, name="Scan B", height=200, width=250, tile=128, dims=[(4, 3)],
                      channel_names=("B1", "B2", "B3"))
    path = write_vsi(tmp_path, stacks=[overview, first, second])
    description = formats.describe(path)
    assert description.images == ["Scan A", "Scan B"]
    assert description.image_index == 0
    assert (description.height, description.width) == (300, 400)
    assert description.channel_names == ["A1", "A2"]
    note = formats.note(path)
    assert "Scan A" in note and "Scan B" not in note
    assert "2 images" in note
    reader = _reader(path)
    try:
        np.testing.assert_array_equal(reader.read(0, 1, 0, 300, 0, 400), _full(first, 0, 1))
    finally:
        reader.close()


# -- z and channel dimensions -----------------------------------------------


@pytest.mark.parametrize("pyramid", [False, True])
@pytest.mark.parametrize("dims", [[(1, 3), (4, 2)], [(4, 2), (1, 3)]],
                         ids=["z-then-channel", "channel-then-z"])
def test_z_and_channel_dimensions_map_reads_in_either_order(tmp_path, dims, pyramid):
    stack = VsiStack(2, height=301, width=257, tile=128, dims=dims, pyramid=pyramid,
                     channel_names=("DAPI", "Cy5"), z_step=2.5)
    path = write_vsi(tmp_path, stacks=[stack])
    reader = _reader(path)
    try:
        assert reader.channels == 2 and reader.z_count == 3 and reader.t_count == 1
        assert reader.levels == ([(301, 257), (151, 129)] if pyramid else [(301, 257)])
        assert reader.channel_names == ["DAPI", "Cy5"]
        assert reader.z_step == pytest.approx(2.5)
        for z in range(3):
            for c in range(2):
                got = reader.read(0, c, 0, 301, 0, 257, z=z)
                np.testing.assert_array_equal(got, _full(stack, 0, c, z),
                                              err_msg=f"z={z} c={c}")
        if pyramid:
            got = reader.read(1, 1, 10, 151, 5, 129, z=2)
            np.testing.assert_array_equal(
                got, vsi_expected(stack, 1, 1, 2, 10, 151, 5, 129))
    finally:
        reader.close()
    description = formats.describe(path)
    assert description.z_count == 3 and description.z_step == pytest.approx(2.5)
    assert formats.physical_metadata(path)["physical_size_z"] == pytest.approx(2.5)


def test_the_default_plane_is_the_middle_of_the_stack(tmp_path):
    stack = VsiStack(2, height=200, width=210, tile=128, dims=[(4, 2), (1, 3)], pyramid=False)
    pyramid = formats.open_image(write_vsi(tmp_path, stacks=[stack]))
    try:
        assert pyramid.z == 1 and pyramid.z_count == 3
        np.testing.assert_array_equal(pyramid["0"][1], _full(stack, 0, 1, 1))
        np.testing.assert_array_equal(pyramid.plane(2, 0)["0"][0], _full(stack, 0, 0, 2))
    finally:
        pyramid.close()


# -- unreadable and colour --------------------------------------------------


def test_a_vsi_without_its_folder_is_refused_naming_the_folder(tmp_path):
    path = write_vsi(tmp_path, stem="lonely", stacks=[VsiStack(2)], with_pixels=False)
    with pytest.raises(FormatUnreadable, match="_lonely_"):
        formats.open_reader(path)
    with pytest.raises(FormatUnreadable, match="_lonely_"):
        formats.describe(path)


def test_an_rgb_stack_is_colour_and_a_brightfield_scan(tmp_path):
    stack = VsiStack(2, height=200, width=300, tile=128, dims=[], pixel_type=2, samples=3,
                     background=0xFFFFFF, levels=2, missing={(0, 1, 2)})
    path = write_vsi(tmp_path, stacks=[stack])
    description = formats.describe(path)
    assert description.rgb is True
    assert description.channels == 3
    assert description.dtype == "uint8"
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "brightfield" and verdict.confidence == "high"
    assert formats.is_rgb(path)

    ys, xs = np.arange(200), np.arange(300)
    want = np.stack([vsi_value(0, 0, 0, ys, xs, s) for s in range(3)], -1).astype(np.uint8)
    want[128:200, 256:300] = 255          # the missing tile reads as the (white) background
    reader = _reader(path)
    try:
        block = reader.read_rgb(0, 0, 200, 0, 300)
        assert block.shape == (200, 300, 3) and block.dtype == np.uint8
        np.testing.assert_array_equal(block, want)
        np.testing.assert_array_equal(reader.read_rgb(0, 100, 150, 120, 270),
                                      want[100:150, 120:270])
        # the channels view reads the same samples
        np.testing.assert_array_equal(reader.read(0, 1, 0, 128, 0, 128), want[:128, :128, 1])
    finally:
        reader.close()
    pyramid = formats.open_image(path)
    try:
        assert pyramid.is_color
    finally:
        pyramid.close()


def test_a_grey_stack_is_fluorescence(tmp_path):
    path = write_vsi(tmp_path, stacks=[VsiStack(2)])
    assert formats.detect_image_type(path).verdict == "fluorescence"
