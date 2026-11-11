"""DeltaVision (.dv / .r3d) files, read from synthetic files written to the
layout in the docstring of `plexora/server/utils/formats/dv.py`.

Every plane carries its own identity (`formats_fixtures.dv_value`), so a read
that takes the wrong plane order, byte order or pixel type cannot pass.
"""

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, dv
from tests.formats_fixtures import DV_MODES, dv_plane, write_dv

H, W = 24, 32


def _open(path):
    return formats.open_reader(path)


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("mode", [6, 1, 2, 0])
@pytest.mark.parametrize("sequence", [0, 1, 2])
def test_every_plane_is_read_for_every_order_type_and_endianness(
        tmp_path, endian, mode, sequence):
    path = write_dv(tmp_path / "a.dv", height=H, width=W, depth=3, waves=2, times=2,
                    sequence=sequence, mode=mode, endian=endian)
    reader = _open(path)
    try:
        assert reader.dtype == np.dtype(DV_MODES[mode])
        assert reader.levels == [(H, W)]
        assert (reader.z_count, reader.channels, reader.t_count) == (3, 2, 2)
        for z in range(3):
            for w in range(2):
                for t in range(2):
                    got = reader.read(0, w, 0, H, 0, W, z=z, t=t)
                    want = dv_plane(z, w, t, H, W, DV_MODES[mode])
                    assert got.dtype == np.dtype(DV_MODES[mode])
                    assert got.shape == (H, W)
                    np.testing.assert_array_equal(got, want, err_msg=f"z={z} w={w} t={t}")
    finally:
        reader.close()


def test_a_region_equals_the_slice_of_the_plane(tmp_path):
    path = write_dv(tmp_path / "r.dv", height=H, width=W, sequence=2, endian=">")
    reader = _open(path)
    try:
        full = dv_plane(1, 1, 1, H, W, "u2")
        got = reader.read(0, 1, 5, 17, 3, 29, z=1, t=1)
        np.testing.assert_array_equal(got, full[5:17, 3:29])
        # a single row and a single column
        np.testing.assert_array_equal(reader.read(0, 1, 7, 8, 0, W, z=1, t=1), full[7:8, :])
        np.testing.assert_array_equal(reader.read(0, 1, 0, H, 9, 10, z=1, t=1), full[:, 9:10])
    finally:
        reader.close()


def test_channel_names_pixel_size_and_z_step(tmp_path):
    path = write_dv(tmp_path / "m.dv", height=H, width=W, depth=4, waves=2, times=1,
                    wavelengths=(523, 608, 0, 0, 0), pixel_size=(0.0645, 0.0650, 0.2))
    description = formats.describe(path)
    assert description.channel_names == ["523 nm", "608 nm"]
    assert description.pixel_size == (pytest.approx(0.0645), pytest.approx(0.0650))
    assert description.z_step == pytest.approx(0.2)
    assert description.z_count == 4 and description.t_count == 1
    assert description.channels == 2
    assert description.format_name == "DeltaVision"
    meta = formats.physical_metadata(path)
    assert meta["physical_size_x"] == pytest.approx(0.0645)
    assert meta["physical_size_y"] == pytest.approx(0.0650)
    assert meta["physical_size_z"] == pytest.approx(0.2)
    assert meta["physical_size_x_unit"] == "µm"


def test_a_single_plane_states_no_z_step(tmp_path):
    path = write_dv(tmp_path / "flat.dv", depth=1, waves=1, times=1,
                    pixel_size=(0.1, 0.1, 0.5), wavelengths=(0, 0, 0, 0, 0))
    description = formats.describe(path)
    assert description.z_step is None
    assert "physical_size_z" not in formats.physical_metadata(path)


def test_a_file_that_names_no_wavelength_numbers_its_channels(tmp_path):
    path = write_dv(tmp_path / "w.dv", depth=1, waves=2, times=1, wavelengths=(0, 0, 0, 0, 0))
    assert formats.channel_names(path) == ["Channel 1", "Channel 2"]


def test_a_missing_pixel_size_is_none(tmp_path):
    path = write_dv(tmp_path / "np.dv", pixel_size=(0.0, 0.0, 0.0))
    assert formats.pixel_size(path) is None
    assert formats.physical_metadata(path) == {}


def test_a_header_that_undercounts_reads_as_a_z_stack(tmp_path):
    # 2 wavelengths stated, 5 planes written: 5 is not a multiple of 2
    path = write_dv(tmp_path / "abort.dv", depth=1, waves=2, times=1, sections=5)
    # five planes' worth of bytes, so give the file them
    data = path.read_bytes()
    path.write_bytes(data + dv_plane(0, 0, 0, H, W, "u2").tobytes() * 3)
    description = formats.describe(path)
    assert (description.channels, description.z_count, description.t_count) == (1, 5, 1)


def test_a_truncated_file_reads_zeros_past_its_end(tmp_path):
    # drop the whole last plane and half of the one before it, on a row boundary
    row = W * 2
    plane = H * row
    path = write_dv(tmp_path / "cut.dv", height=H, width=W, depth=3, waves=1, times=1,
                    wavelengths=(500, 0, 0, 0, 0), cut_bytes=plane + (H // 2) * row)
    reader = _open(path)
    try:
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W, z=0),
                                      dv_plane(0, 0, 0, H, W, "u2"))
        half = reader.read(0, 0, 0, H, 0, W, z=1)
        want = dv_plane(1, 0, 0, H, W, "u2")
        np.testing.assert_array_equal(half[:H // 2], want[:H // 2])
        assert not half[H // 2:].any()
        last = reader.read(0, 0, 0, H, 0, W, z=2)
        assert last.shape == (H, W) and not last.any()
    finally:
        reader.close()


def test_a_file_cut_in_the_middle_of_a_row_still_reads(tmp_path):
    """The documented behaviour is zeros past the end and no exception; a cut
    that does not fall on a row boundary is the ordinary way to be truncated."""
    path = write_dv(tmp_path / "cutrow.dv", height=H, width=W, depth=1, waves=1, times=1,
                    wavelengths=(500, 0, 0, 0, 0), cut_bytes=W * 2 * 3 + 10)
    reader = _open(path)
    try:
        got = reader.read(0, 0, 0, H, 0, W)
        want = dv_plane(0, 0, 0, H, W, "u2")
        assert got.shape == (H, W)
        np.testing.assert_array_equal(got[:H - 4], want[:H - 4])
        assert not got[H - 3:].any()
    finally:
        reader.close()


def test_a_file_without_the_mark_or_a_header_is_refused(tmp_path):
    short = tmp_path / "short.dv"
    short.write_bytes(b"\x00" * 100)
    with pytest.raises(FormatUnreadable):
        dv.reader(str(short))
    blank = tmp_path / "blank.dv"
    blank.write_bytes(b"\x00" * 2048)
    with pytest.raises(FormatUnreadable):
        dv.reader(str(blank))


def test_a_pixel_type_that_is_not_read_is_refused(tmp_path):
    path = write_dv(tmp_path / "t.dv")
    data = bytearray(path.read_bytes())
    data[12:16] = (99).to_bytes(4, "little")
    path.write_bytes(bytes(data))
    with pytest.raises(FormatUnreadable):
        dv.reader(str(path))


def test_r3d_is_the_same_format_and_handles_says_so(tmp_path):
    path = write_dv(tmp_path / "x.r3d")
    assert formats.handles(path)
    assert formats.format_name(path) == "DeltaVision"
    assert not formats.handles(tmp_path / "nothing.dv")


def test_the_pyramid_opens_on_the_middle_plane_and_moves_through_z(tmp_path):
    path = write_dv(tmp_path / "p.dv", height=H, width=W, depth=5, waves=2, times=1,
                    sequence=1)
    pyramid = formats.open_image(path)
    try:
        assert pyramid.z == 2 and pyramid.t == 0
        assert pyramid.level_shapes == [[H, W]]
        for channel in range(2):
            np.testing.assert_array_equal(
                pyramid["0"][channel], dv_plane(2, channel, 0, H, W, "u2"))
        other = pyramid.plane(4, 0)
        np.testing.assert_array_equal(other["0"][1], dv_plane(4, 1, 0, H, W, "u2"))
        assert formats.physical_metadata(pyramid)["physical_size_z"] == pytest.approx(0.2)
    finally:
        pyramid.close()


def test_detect_image_type_says_fluorescence(tmp_path):
    path = write_dv(tmp_path / "f.dv")
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "fluorescence"
    assert verdict.confidence == "high"
    assert not formats.is_rgb(path)
