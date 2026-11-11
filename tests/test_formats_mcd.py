"""Hyperion MCD through `plexora/server/utils/formats/mcd.py`, over a fake
`readimc`: acquisitions with float32 `(C, Y, X)` counts, metal names such as
'Er170' and panel labels such as 'CD3((1234))Er170'.
"""

import sys

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatSupportMissing, FormatUnreadable
from plexora.server.utils.formats.mcd import _name
from tests.formats_fakes import fake_acquisition, install_readimc, touch

H, W = 12, 16


def _open(monkeypatch, tmp_path, slides, name="a.mcd"):
    module = install_readimc(monkeypatch, slides)
    return touch(tmp_path, name), module


@pytest.mark.parametrize("label, metal, expected", [
    ("CD3((1234))Er170", "Er170", "CD3"),
    ("CD45((9))Nd148", "Nd148", "CD45"),
    ("Ki-67((55))Dy162", "Dy162", "Ki-67"),
    ("DNA1((1))Ir191", "Ir191", "DNA1"),
    ("", "Er170", "Er170"),
    (None, "Er170", "Er170"),
    ("Er170", "Er170", "Er170"),
    ("CD3", "Er170", "CD3"),
    ("  CD3  ", "Er170", "CD3"),
    ("((7))Er170", "Er170", "Er170"),
])
def test_label_cleaning(label, metal, expected):
    assert _name(label, metal) == expected


def test_shape_channels_names_scale_and_reads(monkeypatch, tmp_path):
    acquisition = fake_acquisition(1, "ROI_001", metals=("Er170", "Yb172", "Ir191"),
                                   labels=["CD3((1234))Er170", "", "DNA1((1))Ir191"], pixel_um=(1.0, 1.0))
    path, module = _open(monkeypatch, tmp_path, [[acquisition]])
    reader = formats.open_reader(path)
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (3, 1, 1)
        assert reader.dtype == np.dtype(np.float32) and not reader.rgb
        assert reader.channel_names == ["CD3", "Yb172", "DNA1"]
        assert reader.pixel_size == (1.0, 1.0) and reader.z_step is None
        assert reader.images == ["ROI_001"] and reader.note() == ""
        assert module.state.strict == []                       # nothing read until a pixel is asked for
        for c in range(3):
            got = reader.read(0, c, 2, 9, 3, 15)
            assert got.dtype == np.float32 and got.shape == (7, 12)
            np.testing.assert_array_equal(got, acquisition.data[c, 2:9, 3:15])
        np.testing.assert_array_equal(reader.read(0, 2, H - 1, H, W - 1, W), acquisition.data[2, -1:, -1:])
        assert module.state.strict == [False]                  # read once, an early stop tolerated
        assert module.state.opened == module.state.closed
    finally:
        reader.close()


def test_labels_given_as_none_or_missing_fall_back_to_metals(monkeypatch, tmp_path):
    acquisition = fake_acquisition(metals=("Er170", "Yb172"))
    acquisition.channel_labels = None
    path, _ = _open(monkeypatch, tmp_path, [[acquisition]])
    assert formats.channel_names(path) == ["Er170", "Yb172"]


def test_the_metal_stands_in_for_a_label_that_is_the_metal(monkeypatch, tmp_path):
    acquisition = fake_acquisition(metals=("Er170", "Yb172"), labels=["Er170", "Yb172((3))Yb172"])
    path, _ = _open(monkeypatch, tmp_path, [[acquisition]])
    assert formats.channel_names(path) == ["Er170", "Yb172"]


def test_the_same_label_on_two_channels_is_numbered(monkeypatch, tmp_path):
    acquisition = fake_acquisition(metals=("Er170", "Yb172", "Nd148"),
                                   labels=["CD3((1))Er170", "CD3((2))Yb172", ""])
    path, _ = _open(monkeypatch, tmp_path, [[acquisition]])
    assert formats.channel_names(path) == ["CD3", "CD3 (2)", "Nd148"]


@pytest.mark.parametrize("pixel, expected", [((1.0, 1.0), (1.0, 1.0)), ((1.0, 0.0), (1.0, 1.0)),
                                             ((0.5, 0.8), (0.5, 0.8)), ((0, 0), None), ((None, None), None)])
def test_pixel_size(monkeypatch, tmp_path, pixel, expected):
    path, _ = _open(monkeypatch, tmp_path, [[fake_acquisition(pixel_um=pixel)]])
    assert formats.pixel_size(path) == expected
    if expected is None:
        assert formats.physical_metadata(path) == {}
    else:
        assert formats.physical_metadata(path)["physical_size_x"] == expected[0]


def test_acquisitions_across_slides_are_listed_and_the_first_is_shown(monkeypatch, tmp_path):
    first = fake_acquisition(1, "Tonsil", height=H, width=W)
    second = fake_acquisition(2, "", height=8, width=9)
    third = fake_acquisition(3, None, height=8, width=9)
    path, _ = _open(monkeypatch, tmp_path, [[first, second], [third]])
    description = formats.describe(path)
    assert description.images == ["Tonsil", "Acquisition 2", "Acquisition 3"]
    assert description.image_index == 0
    assert (description.height, description.width) == (H, W)
    assert formats.note(path) == ('This Hyperion MCD file holds 3 images; Plexora opens "Tonsil" '
                                  'and not the other 2.')


def test_two_acquisitions_say_the_other_one(monkeypatch, tmp_path):
    path, _ = _open(monkeypatch, tmp_path, [[fake_acquisition(1, "A"), fake_acquisition(2, "B")]])
    assert formats.note(path).endswith('and not the other one.')


def test_no_acquisition_is_unreadable(monkeypatch, tmp_path):
    for slides in ([], [[]], [[], []]):
        path, _ = _open(monkeypatch, tmp_path, slides)
        with pytest.raises(FormatUnreadable, match="holds no acquisition"):
            formats.open_reader(path)


def test_the_file_is_read_again_after_it_is_closed_not_left_open(monkeypatch, tmp_path):
    acquisition = fake_acquisition()
    path, module = _open(monkeypatch, tmp_path, [[acquisition]])
    reader = formats.open_reader(path)
    reader.read(0, 0, 0, 2, 0, 2)
    reader.close()
    reader.read(0, 0, 0, 2, 0, 2)
    assert module.state.strict == [False, False]
    assert module.state.opened == module.state.closed


def test_counts_with_a_fraction_are_kept_not_rounded(monkeypatch, tmp_path):
    data = np.full((1, 4, 4), 2.75, dtype=np.float32)
    path, _ = _open(monkeypatch, tmp_path, [[fake_acquisition(metals=("Er170",), data=data)]])
    got = formats.open_reader(path).read(0, 0, 0, 4, 0, 4)
    assert got.dtype == np.float32 and (got == 2.75).all()


def test_pyramid_scale_bar_and_detection(monkeypatch, tmp_path):
    acquisition = fake_acquisition(metals=("Er170", "Yb172"), labels=["CD3((1))Er170", ""])
    path, _ = _open(monkeypatch, tmp_path, [[acquisition]])
    pyramid = formats.open_image(path)
    assert pyramid.level_shapes == [[H, W]] and pyramid[0].shape == (2, H, W)
    assert pyramid[0].dtype == np.dtype(np.float32)
    np.testing.assert_array_equal(pyramid[0][1, 3:7, 4:9], acquisition.data[1, 3:7, 4:9])
    assert pyramid.channel_names == ["CD3", "Yb172"]
    assert formats.physical_metadata(pyramid) == {
        "physical_size_x": 1.0, "physical_size_x_unit": "µm",
        "physical_size_y": 1.0, "physical_size_y_unit": "µm"}
    detection = formats.detect_image_type(path)
    assert detection.verdict == "fluorescence" and not formats.is_rgb(path)


def test_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "readimc", None)
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(touch(tmp_path, "a.mcd"))
    assert "readimc" in info.value.INSTALL and "pip install" in str(info.value)
    assert "Hyperion MCD" in str(info.value)
