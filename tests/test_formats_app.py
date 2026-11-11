"""DeltaVision, Olympus VSI and Hamamatsu NDPIS through the app, and the
value-map helpers the z-stacks lean on.

One end-to-end test per format (inspect, import, channel names, config, a
tile), then the volume source of a z-stack in DV and in VSI, then
`value_range.map_as` and the level of a `ValuePyramid` over a DICOM-style
`_MonoLevel`. Expected values are the arrays the fixtures wrote.
"""

import io

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.server.providers.local import LocalImageProvider
from plexora.server.utils import value_range
from tests.formats_fixtures import (VsiStack, dv_plane, in_colour, vsi_expected, vsi_value,
                                    write_dv, write_ndpi, write_ndpis, write_vsi)


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    return plexora.app.test_client()


def _source(tmp_path):
    return tmp_path / "src"


def _register(client, path, name):
    inspected = client.post("/import/inspect", json={"paths": [str(path)]})
    assert inspected.status_code == 200, inspected.data[:400]
    response = client.post("/import/sample", json={"paths": [str(path)], "name": name})
    assert response.status_code == 200, response.data[:400]
    assert client.get(f"/init_database?datasource={name}").status_code == 200
    names = client.get(f"/get_channel_names?datasource={name}").get_json()
    entry = client.get("/config").get_json()[name]
    return inspected.get_json(), names, entry


def _tile(client, src, level=0, x=0, y=0):
    response = client.get(f"{src}{level}/{x}_{y}.png?q=hd")
    assert response.status_code == 200, response.data[:300]
    return np.asarray(Image.open(io.BytesIO(response.data)))


# -- one end-to-end test per format ---------------------------------------------


def test_a_deltavision_file_imports_and_serves_its_channels(tmp_path, client):
    path = write_dv(_source(tmp_path) / "cells.dv", height=64, width=96, depth=1, waves=2,
                    times=1, wavelengths=(523, 608, 0, 0, 0), pixel_size=(0.1, 0.1, 0.0))
    proposal, names, entry = _register(client, path, "cells")

    assert proposal["samples"], proposal
    assert list(names) == ["523 nm", "608 nm"]
    assert [c["fullname"] for c in entry["imageData"]] == ["523 nm", "608 nm"]
    assert entry["image_kind"] == "vendor"
    assert entry["num_channels"] == 2
    assert (entry["height"], entry["width"]) == (64, 96)
    tiles = [_tile(client, entry["imageData"][i]["src"]) for i in range(2)]
    for index, tile in enumerate(tiles):
        assert tile.shape[:2] == (64, 96)
        np.testing.assert_array_equal(tile, dv_plane(0, index, 0, 64, 96, "u2"))


def test_a_vsi_slide_scan_imports_and_serves_its_channels(tmp_path, client):
    stack = VsiStack(2, height=301, width=401, tile=128, dims=[(4, 2)],
                     channel_names=("DAPI", "Cy5"), pixel_size=(0.5, 0.5))
    path = write_vsi(_source(tmp_path), stem="scan", stacks=[stack])
    proposal, names, entry = _register(client, path, "scan")

    assert proposal["samples"], proposal
    assert list(names) == ["DAPI", "Cy5"]
    assert entry["image_kind"] == "vendor"
    assert (entry["height"], entry["width"]) == (301, 401)
    for channel in range(2):
        tile = _tile(client, entry["imageData"][channel]["src"])
        assert tile.shape[:2] == (301, 401)
        np.testing.assert_array_equal(tile, vsi_expected(stack, 0, channel, 0, 0, 301, 0, 401))


def test_an_rgb_vsi_is_a_brightfield_project(tmp_path, client):
    stack = VsiStack(2, height=200, width=300, tile=128, dims=[], pixel_type=2, samples=3,
                     background=0xFFFFFF, levels=2)
    path = write_vsi(_source(tmp_path), stem="he", stacks=[stack])
    _proposal, _names, entry = _register(client, path, "he")

    assert entry["image_kind"] == "brightfield"
    assert entry["imageTypeDetected"] == "brightfield"
    assert (entry["height"], entry["width"]) == (200, 300)
    tile = _tile(client, entry["imageData"][0]["src"])
    assert tile.shape[:2] == (200, 300)
    ys, xs = np.arange(200), np.arange(300)
    want = np.stack([vsi_value(0, 0, 0, ys, xs, s) for s in range(3)], -1)
    np.testing.assert_array_equal(tile[..., :3], want)


def test_an_ndpis_set_imports_as_its_channels(tmp_path, client):
    rng = np.random.default_rng(8)
    blue = rng.integers(10, 250, (200, 300)).astype(np.uint8)
    green = rng.integers(10, 250, (200, 300)).astype(np.uint8)
    folder = _source(tmp_path)
    write_ndpi(folder / "a.ndpi", in_colour(blue, 2), "DAPI 2 (387)", resolution=0.46)
    write_ndpi(folder / "b.ndpi", in_colour(green, 1), "FITC (494)", resolution=0.46)
    path = write_ndpis(folder / "set.ndpis", ["a.ndpi", "b.ndpi"])
    proposal, names, entry = _register(client, path, "set")

    assert proposal["samples"], proposal
    assert list(names) == ["DAPI 2 (387)", "FITC (494)"]
    assert entry["image_kind"] == "vendor"
    assert (entry["height"], entry["width"]) == (200, 300)
    for channel, plane in enumerate((blue, green)):
        np.testing.assert_array_equal(_tile(client, entry["imageData"][channel]["src"]), plane)


# -- z-stacks as volumes ----------------------------------------------------------


@pytest.mark.parametrize("mode", [6, 1], ids=["uint16", "int16"])
def test_a_deltavision_z_stack_opens_as_a_volume(tmp_path, mode):
    path = write_dv(tmp_path / "stack.dv", height=48, width=64, depth=5, waves=2, times=1,
                    mode=mode, sequence=2, endian=">", pixel_size=(0.1, 0.1, 0.25))
    provider = LocalImageProvider(str(path))
    channels, _overview, metadata = provider.open()
    try:
        source = provider.volume_source
        assert source is not None
        volume = source.volume
        assert (volume.depth, volume.frames, volume.channels) == (5, 1, 2)
        assert (volume.height, volume.width) == (48, 64)
        assert volume.spacing.z.value == pytest.approx(0.25)
        assert metadata["physical_size_z"] == pytest.approx(0.25)
        first = np.asarray(source.plane_pyramid(0, 0)["0"][1])
        last = np.asarray(source.plane_pyramid(0, 4)["0"][1])
        assert not np.array_equal(first, last)
        # the values fit the range, so the map is the identity and moving
        # through z shows the planes that were written
        np.testing.assert_array_equal(first, dv_plane(0, 1, 0, 48, 64, "u2"))
        np.testing.assert_array_equal(last, dv_plane(4, 1, 0, 48, 64, "u2"))
        # and the 2-D reader shows the middle plane
        np.testing.assert_array_equal(np.asarray(channels["0"][0]),
                                      dv_plane(2, 0, 0, 48, 64, "u2"))
    finally:
        provider.volume_source.close()


def test_a_vsi_z_stack_opens_as_a_volume(tmp_path):
    stack = VsiStack(2, height=200, width=260, tile=128, dims=[(4, 2), (1, 4)],
                     channel_names=("DAPI", "Cy5"), pyramid=False, z_step=2.5,
                     pixel_size=(0.5, 0.5))
    path = write_vsi(tmp_path, stem="zs", stacks=[stack])
    provider = LocalImageProvider(str(path))
    provider.open()
    source = provider.volume_source
    try:
        assert source is not None
        volume = source.volume
        assert (volume.depth, volume.frames, volume.channels) == (4, 1, 2)
        assert volume.spacing.z.value == pytest.approx(2.5)
        assert volume.spacing.x.value == pytest.approx(0.5)
        first = np.asarray(source.plane_pyramid(0, 0)["0"][0])
        last = np.asarray(source.plane_pyramid(0, 3)["0"][0])
        assert not np.array_equal(first, last)
        np.testing.assert_array_equal(first, vsi_expected(stack, 0, 0, 0, 0, 200, 0, 260))
        np.testing.assert_array_equal(last, vsi_expected(stack, 0, 0, 3, 0, 200, 0, 260))
        assert volume.default_view.z == 2
    finally:
        source.close()


def test_a_single_plane_file_is_not_a_volume(tmp_path):
    path = write_dv(tmp_path / "flat.dv", depth=1, waves=2, times=1)
    provider = LocalImageProvider(str(path))
    provider.open()
    assert provider.volume_source is None


# -- value_range --------------------------------------------------------------------


class _Plane:
    """A float stack, `[channel, rows, cols]`, as a level."""

    def __init__(self, data):
        self.data = np.asarray(data)
        self.shape = self.data.shape
        self.dtype = self.data.dtype

    def __getitem__(self, key):
        return self.data[key]

    def __array__(self, dtype=None, copy=None):
        return self.data if dtype is None else self.data.astype(dtype)


def test_map_as_uses_the_offset_and_scale_of_the_plane_it_was_chosen_from():
    rng = np.random.default_rng(0)
    first = (rng.random((2, 40, 50)) * 3.0).astype(np.float32)       # 0..3: scaled up
    first[1] -= 1.5                                                    # channel 1 has negatives
    other = (rng.random((2, 40, 50)) * 30.0).astype(np.float32)
    mapped, _ = value_range.present(_Plane(first), None)
    assert isinstance(mapped, value_range.ValuePyramid)
    transform = mapped.value_transform
    assert transform[0]["scale"] > 1 and transform[1]["offset"] < 0

    again = value_range.map_as(_Plane(other), mapped)
    assert again.value_transform == transform
    got = np.asarray(again["0"][:])
    for channel in range(2):
        offset, scale = transform[channel]["offset"], transform[channel]["scale"]
        want = np.clip(np.rint((other[channel].astype(np.float64) - offset) * scale), 0, 65535)
        np.testing.assert_array_equal(got[channel], want.astype(np.uint16))
    # not re-chosen from `other`: its own peak would have mapped to the top
    assert got[0].max() == 65535 or transform[0]["scale"] * 30 > 65535
    own, _ = value_range.present(_Plane(other), None)
    assert own.value_transform[0]["scale"] != transform[0]["scale"]
    assert again.level_shapes == [[40, 50]]


def test_the_array_of_a_value_pyramid_over_a_dicom_mono_level_works():
    """`np.asarray(level)` used to raise: a `_MonoLevel` takes an integer
    channel and nothing else, and the level asked it for `[...]`."""
    from plexora.server.utils import dicom_wsi

    rng = np.random.default_rng(1)
    data = (rng.random((2, 30, 45)) * 3.0).astype(np.float32)

    class Source:
        def __init__(self, plane):
            self._plane = plane
            self.height, self.width = plane.shape

        def read(self, y0, y1, x0, x1):
            return self._plane[y0:y1, x0:x1]

    level = dicom_wsi._MonoLevel([Source(data[0]), Source(data[1])], 30, 45, np.float32)
    transform_source = value_range.present(_Plane(data), None)[0]
    offset = np.array([e["offset"] for e in transform_source.value_transform])
    scale = np.array([e["scale"] for e in transform_source.value_transform])
    pyramid = value_range.ValuePyramid([value_range._Level(level, offset, scale)], offset,
                                       scale, level)
    got = np.asarray(pyramid["0"])
    assert got.shape == (2, 30, 45) and got.dtype == np.uint16
    for channel in range(2):
        want = np.clip(np.rint((data[channel].astype(np.float64) - offset[channel])
                               * scale[channel]), 0, 65535)
        np.testing.assert_array_equal(got[channel], want.astype(np.uint16))
    # and a requested dtype is honoured
    assert np.asarray(pyramid["0"], dtype=np.float32).dtype == np.float32
