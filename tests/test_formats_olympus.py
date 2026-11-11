"""Olympus OIR (`oirfile`) and OIB/OIF (`oiffile`) through
`plexora/server/utils/formats/{oir,oib}.py`, and the shared pieces they stand
on (`base.WholeArrayReader`, `base.as_tzcyx`, `base.names_or_numbered`).

The fakes hand back an array in which every element is its flat index, so a
read is checked against a named-axis slice of the array the fake holds.
"""

import sys

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatSupportMissing, FormatUnreadable
from plexora.server.utils.formats import base
from plexora.server.utils.formats.oib import _number
from tests.formats_fakes import coded, install_oib, install_oir, touch

# -- base --------------------------------------------------------------------


@pytest.mark.parametrize("dims", ["TZCYX", "YX", "CYX", "ZYX", "TYX", "ZCYX", "XYC", "CZTYX", "XYZ"])
def test_as_tzcyx_puts_every_named_axis_in_order(dims):
    sizes = {"T": 2, "Z": 3, "C": 4, "Y": 5, "X": 6}
    shape = tuple(sizes[d] for d in dims)
    array = coded(shape)
    out = base.as_tzcyx(array, dims)
    assert out.shape == tuple(sizes[d] if d in dims else 1 for d in "TZCYX")
    for index in np.ndindex(*out.shape):
        named = dict(zip("TZCYX", index))
        source = tuple(named[d] for d in dims)
        assert out[index] == array[source]


@pytest.mark.parametrize("dims", ["QCYX", "CQYX", "CYXQ", "SYX", "TSZCYX"])
def test_as_tzcyx_reads_an_unknown_axis_at_its_first_index(dims):
    sizes = {"Q": 3, "S": 3, "T": 2, "Z": 2, "C": 2, "Y": 4, "X": 5}
    array = coded(tuple(sizes[d] for d in dims))
    out = base.as_tzcyx(array, dims)
    assert out.shape == tuple(sizes[d] if d in dims else 1 for d in "TZCYX")
    for index in np.ndindex(*out.shape):
        named = dict(zip("TZCYX", index))
        assert out[index] == array[tuple(named.get(d, 0) for d in dims)]


def test_as_tzcyx_accepts_a_tuple_of_dims():
    array = coded((2, 3, 4))
    out = base.as_tzcyx(array, ("Z", "Y", "X"))
    assert out.shape == (1, 2, 1, 3, 4)
    np.testing.assert_array_equal(out[0, :, 0], array)


@pytest.mark.parametrize("names, count, expected", [
    (["A", "B"], 2, ["A", "B"]),
    (["A", "A"], 2, ["A", "A (2)"]),
    (["", "B", ""], 3, ["Channel 1", "B", "Channel 3"]),
    (["A"], 3, ["A", "Channel 2", "Channel 3"]),
    ([None, "  C  "], 2, ["Channel 1", "C"]),
    ([], 2, ["Channel 1", "Channel 2"]),
    (["A", "A", "A"], 3, ["A", "A (2)", "A (3)"]),
])
def test_names_or_numbered(names, count, expected):
    assert base.names_or_numbered(names, count) == expected


class _Whole(base.WholeArrayReader):
    format_name = "Whole"

    def __init__(self, array):
        super().__init__()
        self.array = array
        self.loads = []
        self.levels = [array.shape[-2:]]
        self.t_count, self.z_count, self.channels = array.shape[:3]
        self.dtype = array.dtype

    def _load(self, out=None):
        self.loads.append(out)
        return self.array


def test_whole_array_reader_serves_slices_of_a_once_loaded_array():
    array = coded((2, 3, 4, 8, 10))
    reader = _Whole(array)
    got = reader.read(0, 2, 1, 7, 3, 9, z=1, t=1)
    np.testing.assert_array_equal(got, array[1, 1, 2, 1:7, 3:9])
    assert got.flags["C_CONTIGUOUS"]
    reader.read(0, 0, 0, 8, 0, 10)
    assert reader.loads == [None]
    reader.close()
    reader.read(0, 0, 0, 8, 0, 10)
    assert len(reader.loads) == 2          # closed: the array is let go and read again on demand


def test_whole_array_reader_maps_a_large_image_to_a_memory_file(monkeypatch):
    array = coded((1, 1, 2, 8, 10))
    reader = _Whole(array)
    monkeypatch.setattr(base.WholeArrayReader, "MEMORY_BYTES", array.nbytes - 1)
    reader.read(0, 0, 0, 2, 0, 2)
    assert reader.loads == ["memmap"]
    other = _Whole(array)
    monkeypatch.setattr(base.WholeArrayReader, "MEMORY_BYTES", array.nbytes)
    other.read(0, 0, 0, 2, 0, 2)
    assert other.loads == [None]


# -- OIR ---------------------------------------------------------------------

H, W = 12, 16


def _oir(monkeypatch, tmp_path, name="a.oir", **kwargs):
    module = install_oir(monkeypatch, **kwargs)
    return touch(tmp_path, name), module


def test_oir_time_series_shape_names_scale_and_reads(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("T", "C", "Y", "X"), shape=(3, 2, H, W),
                        scales={"X": 0.414, "Y": 0.5, "T": 2.0},
                        units={"X": "micrometer", "Y": "micrometer", "T": "second"},
                        channels=["Alexa 488", "Cy5"])
    reader = formats.open_reader(path)
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (2, 1, 3)
        assert reader.dtype == np.dtype(np.uint32) and not reader.rgb
        assert reader.channel_names == ["Alexa 488", "Cy5"]
        assert reader.pixel_size == (0.414, 0.5)
        assert reader.z_step is None
        assert reader.images == ["a"] and reader.note() == ""
        for t in range(3):
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 2, 9, 3, 15, t=t),
                                              module.array[t, c, 2:9, 3:15])
        np.testing.assert_array_equal(reader.read(0, 1, H - 1, H, W - 1, W), module.array[0, 1, -1:, -1:])
    finally:
        reader.close()


def test_oir_z_stack_maps_z_and_channel(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("Z", "C", "Y", "X"), shape=(5, 3, H, W),
                        scales={"X": 0.2, "Y": 0.2, "Z": -1.5}, units={"X": "micrometer", "Z": "micrometer"},
                        channels=["A", "B", "C"])
    reader = formats.open_reader(path)
    assert (reader.z_count, reader.channels, reader.t_count) == (5, 3, 1)
    assert reader.z_step == 1.5                           # the stack's direction is not a size
    for z in range(5):
        for c in range(3):
            np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W, z=z),
                                          module.array[z, c])


def test_oir_axes_in_any_order(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("C", "Z", "T", "Y", "X"), shape=(2, 3, 2, H, W))
    reader = formats.open_reader(path)
    np.testing.assert_array_equal(reader.read(0, 1, 1, 5, 2, 6, z=2, t=1), module.array[1, 2, 1, 1:5, 2:6])


def test_oir_a_single_plane_image(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("Y", "X"), shape=(H, W))
    reader = formats.open_reader(path)
    assert (reader.channels, reader.z_count, reader.t_count) == (1, 1, 1)
    assert reader.channel_names == ["Channel 1"]
    np.testing.assert_array_equal(reader.read(0, 0, 1, 5, 2, 6), module.array[1:5, 2:6])


@pytest.mark.parametrize("scale, unit, expected", [
    (0.414, "micrometer", 0.414), (414.0, "nanometer", 0.414), (0.00414, "millimeter", 4.14),
    (4e-7, "meter", 0.4), (0.414, None, 0.414), (0.414, "Micrometer", 0.414), (-0.414, "micrometer", 0.414),
])
def test_oir_pixel_size_units(monkeypatch, tmp_path, scale, unit, expected):
    path, _ = _oir(monkeypatch, tmp_path, dims=("Y", "X"), shape=(H, W),
                   scales={"X": scale, "Y": scale}, units={"X": unit, "Y": unit})
    assert formats.describe(path).pixel_size == (pytest.approx(expected), pytest.approx(expected))


def test_oir_z_step_in_nanometres(monkeypatch, tmp_path):
    path, _ = _oir(monkeypatch, tmp_path, dims=("Z", "Y", "X"), shape=(4, H, W),
                   scales={"X": 0.2, "Y": 0.2, "Z": 500.0},
                   units={"X": "micrometer", "Y": "micrometer", "Z": "nanometer"})
    assert formats.physical_metadata(path)["physical_size_z"] == pytest.approx(0.5)


def test_oir_scale_absent_or_zero_states_nothing(monkeypatch, tmp_path):
    path, _ = _oir(monkeypatch, tmp_path, dims=("Z", "Y", "X"), shape=(4, H, W),
                   scales={"X": 0, "Y": 0, "Z": 0}, units={})
    description = formats.describe(path)
    assert description.pixel_size is None and description.z_step is None
    other, _ = _oir(monkeypatch, tmp_path, name="b.oir", dims=("Y", "X"), shape=(H, W))
    assert formats.physical_metadata(other) == {}


def test_oir_y_scale_falls_back_to_x(monkeypatch, tmp_path):
    path, _ = _oir(monkeypatch, tmp_path, dims=("Y", "X"), shape=(H, W), scales={"X": 0.3},
                   units={"X": "micrometer"})
    assert formats.describe(path).pixel_size == (0.3, 0.3)


def test_oir_lists_more_channels_than_the_data_has(monkeypatch, tmp_path):
    path, _ = _oir(monkeypatch, tmp_path, dims=("C", "Y", "X"), shape=(2, H, W),
                   channels=["A", "B", "C", "D"])
    assert formats.channel_names(path) == ["Channel 1", "Channel 2"]


def test_oir_duplicate_and_empty_channel_names_are_numbered(monkeypatch, tmp_path):
    path, _ = _oir(monkeypatch, tmp_path, dims=("C", "Y", "X"), shape=(3, H, W), channels=["Cy5", "Cy5", ""])
    assert formats.channel_names(path) == ["Cy5", "Cy5 (2)", "Channel 3"]


def test_oir_extra_axes_are_read_at_index_zero(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("L", "C", "Y", "X"), shape=(3, 2, H, W))
    reader = formats.open_reader(path)
    assert reader.channels == 2
    np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), module.array[0, 1])


@pytest.mark.parametrize("dims, shape", [((), ()), (("T", "Y"), (3, H))])
def test_oir_without_an_image_is_unreadable(monkeypatch, tmp_path, dims, shape):
    path, module = _oir(monkeypatch, tmp_path, dims=dims, data=coded(shape) if shape else np.zeros(()))
    with pytest.raises(FormatUnreadable, match="holds no image"):
        formats.open_reader(path)
    assert module.state.opened == module.state.closed == 1


def test_oir_the_file_is_loaded_once_and_let_go_on_close(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("C", "Y", "X"), shape=(2, H, W))
    reader = formats.open_reader(path)
    assert module.state.loaded == []                      # opening reads the header only
    reader.read(0, 0, 0, 4, 0, 4)
    reader.read(0, 1, 4, 8, 4, 8)
    assert module.state.loaded == [None]
    assert module.state.opened == module.state.closed     # never left open


def test_oir_a_large_image_is_loaded_into_a_memory_file(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("C", "Y", "X"), shape=(2, H, W))
    monkeypatch.setattr(base.WholeArrayReader, "MEMORY_BYTES", 10)
    formats.open_reader(path).read(0, 0, 0, 4, 0, 4)
    assert module.state.loaded == ["memmap"]


def test_oir_pyramid_and_scale_bar(monkeypatch, tmp_path):
    path, module = _oir(monkeypatch, tmp_path, dims=("Z", "C", "Y", "X"), shape=(5, 2, H, W),
                        scales={"X": 0.4, "Y": 0.4, "Z": 1.0}, units={"X": "micrometer", "Z": "micrometer"})
    pyramid = formats.open_image(path)
    assert (pyramid.z, pyramid.t) == (2, 0) and pyramid[0].shape == (2, H, W)
    np.testing.assert_array_equal(pyramid[0][1, 2:6, 3:8], module.array[2, 1, 2:6, 3:8])
    assert formats.physical_metadata(pyramid) == {
        "physical_size_x": 0.4, "physical_size_x_unit": "µm", "physical_size_y": 0.4,
        "physical_size_y_unit": "µm", "physical_size_z": 1.0, "physical_size_z_unit": "µm"}
    assert formats.detect_image_type(path).verdict == "fluorescence"


def test_oir_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "oirfile", None)
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(touch(tmp_path, "a.oir"))
    assert "oirfile" in info.value.INSTALL and "pip install" in str(info.value)
    assert "Olympus OIR" in str(info.value)


# -- OIB / OIF ---------------------------------------------------------------


def _main(*, width="0,207.0", height=None, unit="um", channels=("Alexa 488",), z=None):
    main = {}
    if width is not None:
        main["Reference Image Parameter"] = {"WidthConvertValue": width,
                                             "HeightConvertValue": height if height is not None else width,
                                             "WidthUnit": unit}
    for index, name in enumerate(channels, 1):
        main[f"Channel {index} Parameters"] = {"DyeName": name, "CH Name": f"CH{index}"}
    main["Axis 0 Parameters Common"] = {"AxisCode": "X", "Interval": 207, "UnitName": "nm"}
    if z is not None:
        main["Axis 3 Parameters Common"] = dict(z)
    return main


def _oib(monkeypatch, tmp_path, name="a.oib", **kwargs):
    module = install_oib(monkeypatch, **kwargs)
    return touch(tmp_path, name), module


def test_oib_decimal_comma_pixel_size_and_the_z_step(monkeypatch, tmp_path):
    main = _main(width="0,207.0", height="0,207.0", channels=("Alexa 488", "Cy5"),
                 z=dict(AxisCode="Z", Interval=510, UnitName="nm"))
    path, module = _oib(monkeypatch, tmp_path, axes="ZCYX", shape=(5, 2, H, W), main=main)
    reader = formats.open_reader(path)
    assert reader.pixel_size == (0.207, 0.207)
    assert reader.z_step == pytest.approx(0.51)
    assert reader.channel_names == ["Alexa 488", "Cy5"]
    assert reader.levels == [(H, W)] and reader.format_name == "Olympus OIB"
    assert (reader.channels, reader.z_count, reader.t_count) == (2, 5, 1)
    for z in range(5):
        for c in range(2):
            np.testing.assert_array_equal(reader.read(0, c, 1, 8, 2, 11, z=z), module.array[z, c, 1:8, 2:11])
    assert reader.images == ["a"] and reader.note() == ""


@pytest.mark.parametrize("value, expected", [
    ("0,207.0", 0.207), ("0.207", 0.207), ("0,207", 0.207), ('"0,207.0"', 0.207), (" 1,5 ", 1.5),
    ("2", 2.0), (3, 3.0), (0.25, 0.25), ("", None), ("abc", None), (None, None),
])
def test_oib_numbers_in_the_acquiring_machines_locale(value, expected):
    assert _number(value) == expected


def test_oib_pixel_size_from_the_two_axes_and_their_unit(monkeypatch, tmp_path):
    path, _ = _oib(monkeypatch, tmp_path, axes="CYX", shape=(1, H, W),
                   main=_main(width="0,414.0", height="0,5", unit='"um"'))
    assert formats.describe(path).pixel_size == (0.414, 0.5)
    other, _ = _oib(monkeypatch, tmp_path, name="b.oib", axes="CYX", shape=(1, H, W),
                    main=_main(width="207", unit="nm"))
    assert formats.describe(other).pixel_size == (pytest.approx(0.207), pytest.approx(0.207))


def test_oib_without_a_reference_parameter_states_no_scale(monkeypatch, tmp_path):
    path, _ = _oib(monkeypatch, tmp_path, axes="CYX", shape=(1, H, W), main=_main(width=None))
    assert formats.describe(path).pixel_size is None
    assert formats.physical_metadata(path) == {}
    other, _ = _oib(monkeypatch, tmp_path, name="b.oib", axes="CYX", shape=(1, H, W), main=_main(width="0,0"))
    assert formats.describe(other).pixel_size is None


@pytest.mark.parametrize("z, expected", [
    (dict(AxisCode="Z", Interval="0,51", UnitName="um"), 0.51),
    (dict(AxisCode='"Z"', Interval="510", UnitName='"nm"'), 0.51),
    (dict(AxisCode="Z", Interval=-2, UnitName="um"), 2.0),
    (dict(AxisCode="Z", Interval=510), 0.51),                    # unit absent: nanometres
    (dict(AxisCode="T", Interval=510, UnitName="nm"), None),     # not the z axis
    (dict(AxisCode="Z", UnitName="nm"), None),
])
def test_oib_z_step(monkeypatch, tmp_path, z, expected):
    path, _ = _oib(monkeypatch, tmp_path, axes="ZYX", shape=(4, H, W), main=_main(channels=("A",), z=z))
    got = formats.describe(path).z_step
    assert got == (pytest.approx(expected) if expected else None)


def test_oib_a_single_plane_has_no_z_step(monkeypatch, tmp_path):
    path, _ = _oib(monkeypatch, tmp_path, axes="CYX", shape=(1, H, W),
                   main=_main(z=dict(AxisCode="Z", Interval=510, UnitName="nm")))
    assert formats.describe(path).z_step is None


def test_oib_channel_names_dye_then_name_numbered_when_unclear(monkeypatch, tmp_path):
    main = _main(channels=("", "Cy5", "Cy5"))
    main["Channel 1 Parameters"]["CH Name"] = "Ch1 name"
    main["Channel 2 Parameters"]["CH Name"] = "other"
    main["Channel 3 Parameters"]["DyeName"] = '"Cy5"'
    path, _ = _oib(monkeypatch, tmp_path, axes="CYX", shape=(3, H, W), main=main)
    assert formats.channel_names(path) == ["Ch1 name", "Cy5", "Cy5 (3)"]
    short, _ = _oib(monkeypatch, tmp_path, name="b.oib", axes="CYX", shape=(3, H, W), main=_main(channels=("A",)))
    assert formats.channel_names(short) == ["Channel 1", "Channel 2", "Channel 3"]
    nameless = _main(channels=("", ""))
    for section in ("Channel 1 Parameters", "Channel 2 Parameters"):
        nameless[section].pop("CH Name")
    other, _ = _oib(monkeypatch, tmp_path, name="c.oib", axes="CYX", shape=(2, H, W), main=nameless)
    assert formats.channel_names(other) == ["Channel 1", "Channel 2"]


def test_oib_channel_sections_are_in_numeric_order(monkeypatch, tmp_path):
    names = [f"Dye{n}" for n in range(1, 11)]
    main = _main(channels=names)
    shuffled = {k: main[k] for k in reversed(list(main))}
    path, _ = _oib(monkeypatch, tmp_path, axes="CYX", shape=(10, 4, 4), main=shuffled)
    assert formats.channel_names(path) == names


@pytest.mark.parametrize("axes, shape", [("TCYX", (2, 3, H, W)), ("CZYX", (2, 3, H, W)), ("YX", (H, W)),
                                         ("ZTCYX", (2, 2, 2, H, W)), ("TYX", (3, H, W))])
def test_oib_axes_in_any_order_reach_the_right_plane(monkeypatch, tmp_path, axes, shape):
    path, module = _oib(monkeypatch, tmp_path, axes=axes, shape=shape, main=_main(channels=("A", "B", "C")))
    reader = formats.open_reader(path)
    sizes = dict(zip(axes, shape))
    for t in range(sizes.get("T", 1)):
        for z in range(sizes.get("Z", 1)):
            for c in range(sizes.get("C", 1)):
                named = {"T": t, "Z": z, "C": c}
                index = tuple(named[a] for a in axes if a in named) + (slice(1, 6), slice(2, 7))
                np.testing.assert_array_equal(reader.read(0, c, 1, 6, 2, 7, z=z, t=t), module.array[index])


def test_oib_loaded_once(monkeypatch, tmp_path):
    path, module = _oib(monkeypatch, tmp_path, axes="CYX", shape=(2, H, W), main=_main(channels=("A", "B")))
    reader = formats.open_reader(path)
    assert module.state.loaded == 0
    reader.read(0, 0, 0, 3, 0, 3)
    reader.read(0, 1, 0, 3, 0, 3)
    assert module.state.loaded == 1


def test_oif_is_named_so(monkeypatch, tmp_path):
    path, _ = _oib(monkeypatch, tmp_path, name="a.oif", axes="CYX", shape=(1, H, W), main=_main())
    assert formats.describe(path).format_name == "Olympus OIF"
    assert formats.format_name(path) == "Olympus OIF"


def test_oib_without_the_package_the_error_names_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "oiffile", None)
    with pytest.raises(FormatSupportMissing) as info:
        formats.open_reader(touch(tmp_path, "a.oib"))
    assert "oiffile" in info.value.INSTALL and "pip install" in str(info.value)
    assert "Olympus OIB" in str(info.value)
