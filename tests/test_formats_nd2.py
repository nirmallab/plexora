"""Nikon ND2 through `plexora/server/utils/formats/nd2file.py`, over a fake `nd2`
(`tests/formats_fakes.py`): `.sizes` in loop order, `read_frame(i)` over the
non-C/Y/X axes in that order, calibration flags and a 1 µm "uncalibrated" voxel.
"""

import sys

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatSupportMissing
from tests.formats_fakes import colour_pattern, install_nd2, nd2_key, plane, touch

H, W = 16, 20


def _open(monkeypatch, tmp_path, sizes, name="a.nd2", **kwargs):
    module = install_nd2(monkeypatch, sizes=sizes, **kwargs)
    return touch(tmp_path, name), module


def test_time_series_shape_names_scale_and_reads(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(T=3, C=2, Y=H, X=W), names=["DAPI", "GFP"],
                         voxel=(0.325, 0.33, 1.0))
    reader = formats.open_reader(path)
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (2, 1, 3)
        assert reader.channel_names == ["DAPI", "GFP"]
        assert reader.pixel_size == (0.325, 0.33)
        assert reader.z_step is None
        assert reader.images == ["a"] and reader.note() == ""
        for t in range(3):
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 2, 9, 3, 15, t=t),
                                              plane(nd2_key(t, c), H, W)[2:9, 3:15])
    finally:
        reader.close()
    assert module.made[0].closed


def test_edges(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(C=2, Y=H, X=W))
    reader = formats.open_reader(path)
    full = plane(nd2_key(0, 1), H, W)
    np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), full)
    np.testing.assert_array_equal(reader.read(0, 1, H - 1, H, W - 1, W), full[-1:, -1:])


@pytest.mark.parametrize("sizes, z, t, index", [
    # index = the sequence number over the non-C/Y/X axes, in .sizes order
    (dict(T=3, P=2, Z=4, C=2, Y=H, X=W), 1, 2, 17),          # (2, 0, 1) over (3, 2, 4)
    (dict(Z=3, T=2, P=2, C=2, Y=H, X=W), 2, 1, 10),          # (2, 1, 0) over (3, 2, 2)
    (dict(T=5, Z=4, C=2, Y=H, X=W), 3, 4, 19),               # (4, 3) over (5, 4)
    (dict(Z=4, C=2, Y=H, X=W), 3, 0, 3),
    (dict(C=2, Y=H, X=W), 0, 0, 0),
])
def test_the_frame_is_the_sequence_number_over_the_loop_axes(monkeypatch, tmp_path, sizes, z, t, index):
    path, module = _open(monkeypatch, tmp_path, sizes)
    reader = formats.open_reader(path)
    got = reader.read(0, 1, 0, H, 0, W, z=z, t=t)
    assert module.made[0].frames_read == [index]
    np.testing.assert_array_equal(got, plane(nd2_key(index, 1), H, W))


def test_multi_position_shows_position_zero_and_says_how_many(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(T=11, P=4, Z=5, C=2, Y=H, X=W))
    description = formats.describe(path)
    assert description.images == ["Position 1", "Position 2", "Position 3", "Position 4"]
    assert (description.z_count, description.t_count) == (5, 11)
    assert formats.note(path) == ('This Nikon ND2 file holds 4 images; Plexora opens '
                                  '"Position 1" and not the other 3.')
    reader = formats.open_reader(path)
    reader.read(0, 0, 0, H, 0, W, z=3, t=7)
    assert module.made[-1].frames_read == [7 * 20 + 0 * 5 + 3]      # position 0, never 1..3


def test_a_frame_is_decoded_once_for_many_reads(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(T=2, C=3, Y=H, X=W))
    reader = formats.open_reader(path)
    for c in range(3):
        reader.read(0, c, 0, 4, 0, 4, t=1)
        reader.read(0, c, 4, 8, 4, 8, t=1)
    assert module.made[0].frames_read == [1]


def test_an_image_without_a_channel_axis(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(Y=H, X=W), names=["only"])
    reader = formats.open_reader(path)
    assert reader.channels == 1 and reader.channel_names == ["only"]
    assert module.made[0].frames_read == []
    np.testing.assert_array_equal(reader.read(0, 0, 1, 5, 2, 7), plane(nd2_key(0, 0), H, W)[1:5, 2:7])
    assert module.made[0].frames_read == [0]


@pytest.mark.parametrize("names, expected", [
    (["DAPI", "", "DAPI"], ["DAPI", "Channel 2", "DAPI (3)"]),
    (["", "", ""], ["Channel 1", "Channel 2", "Channel 3"]),
    (["A", "B"], ["A", "B", "Channel 3"]),
    ([None, "B", "B"], ["Channel 1", "B", "B (3)"]),
])
def test_channel_names_empty_duplicate_or_missing_are_numbered(monkeypatch, tmp_path, names, expected):
    path, _ = _open(monkeypatch, tmp_path, dict(C=3, Y=H, X=W), names=names)
    assert formats.channel_names(path) == expected


def test_no_metadata_numbers_the_channels(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(C=2, Y=H, X=W), metadata=None)
    assert formats.channel_names(path) == ["Channel 1", "Channel 2"]
    assert formats.describe(path).pixel_size == (1.0, 1.0)      # no calibration flags: believed


def test_pixel_size_and_z_step_where_calibrated(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(Z=6, C=1, Y=H, X=W), voxel=(0.5, 0.0, 2.5))
    meta = formats.physical_metadata(path)
    assert meta == {"physical_size_x": 0.5, "physical_size_x_unit": "µm",
                    "physical_size_y": 0.5, "physical_size_y_unit": "µm",      # y falls back to x
                    "physical_size_z": 2.5, "physical_size_z_unit": "µm"}


def test_an_uncalibrated_file_states_no_scale(monkeypatch, tmp_path):
    # ND2 says 1 µm for what it never calibrated
    path, _ = _open(monkeypatch, tmp_path, dict(Z=6, C=1, Y=H, X=W), voxel=(1.0, 1.0, 1.0),
                    calibrated=(False, False, False))
    description = formats.describe(path)
    assert description.pixel_size is None and description.z_step is None
    assert formats.physical_metadata(path) == {}


def test_calibrated_xy_with_an_uncalibrated_z(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(Z=6, C=1, Y=H, X=W), voxel=(0.4, 0.4, 1.0),
                    calibrated=(True, True, False))
    description = formats.describe(path)
    assert description.pixel_size == (0.4, 0.4) and description.z_step is None


def test_a_calibrated_one_micron_z_step_is_kept(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(Z=6, C=1, Y=H, X=W), voxel=(0.4, 0.4, 1.0))
    assert formats.describe(path).z_step == 1.0


def test_a_single_plane_has_no_z_step(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(C=1, Y=H, X=W), voxel=(0.4, 0.4, 2.0))
    assert formats.describe(path).z_step is None


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("sizes", [dict(Y=H, X=W, C=3), dict(Y=H, X=W, S=3)])
def test_rgb_camera_frames_are_a_colour_picture(monkeypatch, tmp_path, dtype, sizes):
    path, module = _open(monkeypatch, tmp_path, sizes, rgb=True, dtype=dtype)
    description = formats.describe(path)
    assert description.rgb and description.channels == 3
    assert description.channel_names == ["Red", "Green", "Blue"]
    assert formats.detect_image_type(path).verdict == "brightfield"
    reader = formats.open_reader(path)
    block = reader.read_rgb(0, 2, 9, 3, 15)
    assert block.dtype == np.uint8 and block.shape == (7, 12, 3)
    np.testing.assert_array_equal(block, colour_pattern(H, W)[2:9, 3:15])
    assert module.made[-1].frames_read[0] == 0


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_a_colour_pictures_channel_read_is_eight_bit_like_its_rgb_read(monkeypatch, tmp_path, dtype):
    # CZI and LIF answer read(channel) of a colour image in the 8 bits the
    # image's `dtype` for a colour pyramid is (uint8); ND2 must too.
    path, _ = _open(monkeypatch, tmp_path, dict(Y=H, X=W, C=3), rgb=True, dtype=dtype)
    reader = formats.open_reader(path)
    got = reader.read(0, 1, 2, 9, 3, 15)
    assert got.dtype == np.uint8
    np.testing.assert_array_equal(got, colour_pattern(H, W)[2:9, 3:15, 1])


def test_a_fluorescence_file_is_detected_as_channels(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, dict(C=2, Y=H, X=W))
    detection = formats.detect_image_type(path)
    assert detection.verdict == "fluorescence" and "2 channels" in detection.reason
    assert not formats.is_rgb(path)


def test_a_large_frame_gets_derived_coarse_levels_read_from_level_zero(monkeypatch, tmp_path):
    big = 1500

    def frames(index):
        x = np.arange(big) // 100
        return (np.arange(2)[:, None, None] * 1000 + x[None, None, :]
                + np.zeros((2, big, big), dtype=np.int64)).astype(np.uint16)

    path, module = _open(monkeypatch, tmp_path, dict(C=2, Y=big, X=big), dtype=np.uint16,
                         frame_fn=frames)
    pyramid = formats.open_image(path)
    assert pyramid.level_shapes == [[1500, 1500], [750, 750]]
    assert pyramid[1].shape == (2, 750, 750)
    np.testing.assert_allclose(pyramid[1][1, 0:4, 0:4], 1000, atol=1)
    np.testing.assert_allclose(pyramid[1][0, 0:4, 740:750], 14, atol=1)
    assert pyramid[0][1, 0:3, 1450:1453].tolist() == [[1014] * 3] * 3
    assert (pyramid.z, pyramid.t) == (0, 0)
    pyramid.close()
    assert module.made[0].closed


def test_the_default_plane_is_the_middle_of_the_stack(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(T=2, Z=5, C=2, Y=H, X=W))
    pyramid = formats.open_image(path)
    assert (pyramid.z, pyramid.t) == (2, 0)
    np.testing.assert_array_equal(pyramid[0][1, 0:5, 0:5], plane(nd2_key(2, 1), H, W)[:5, :5])
    other = pyramid.plane(z=4, t=1)
    np.testing.assert_array_equal(other[0][0, 0:5, 0:5], plane(nd2_key(9, 0), H, W)[:5, :5])


def test_a_failure_while_opening_closes_the_file(monkeypatch, tmp_path):
    path, module = _open(monkeypatch, tmp_path, dict(C=1, Y=H, X=W), voxel_error=RuntimeError("bad"))
    with pytest.raises(RuntimeError, match="bad"):
        formats.open_reader(path)
    assert module.made[0].closed


def test_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "nd2", None)
    path = touch(tmp_path, "a.nd2")
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(path)
    assert "nd2" in info.value.INSTALL and "pip install" in str(info.value)
    assert "Nikon ND2" in str(info.value)
