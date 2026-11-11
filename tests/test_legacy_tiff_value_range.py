"""Pixel types the viewer has no tile for, presented as uint16.

Regression tests for `plexora/server/utils/value_range.py` and its use in
`LocalImageProvider.open`: float, signed and 32-bit images come back as a
pyramid of uint16 tiles that keep the source's order, keep zero at zero for
non-negative data, carry a `value_transform` that inverts to the source within
one mapped step, and an overview in the same mapped units; uint8/uint16 are
untouched.

Expectations are stated from the written arrays, not from the reader.
"""

import numpy as np
import pytest
import tifffile
import zarr

from plexora.server.models import data_model
from plexora.server.providers.local import LocalImageProvider
from plexora.server.utils import value_range

C, H, W = 3, 600, 700


def _float01(seed=0):
    rng = np.random.default_rng(seed)
    data = rng.random((C, H, W)).astype(np.float32)
    data[:, :10, :10] = 0.0
    return data


def _source_cases():
    rng = np.random.default_rng(11)
    cases = {
        "float_0_1": _float01(),
        "float_0_55": (rng.random((C, H, W)) * 55).astype(np.float32),
        "float_negative": (rng.standard_normal((C, H, W)) * 3).astype(np.float32),
        "float64_counts": rng.integers(0, 5000, (C, H, W)).astype(np.float64),
        "int16_negative": rng.integers(-2000, 3000, (C, H, W)).astype(np.int16),
        "int8": rng.integers(-100, 100, (C, H, W)).astype(np.int8),
        "uint32_small": rng.integers(0, 40000, (C, H, W)).astype(np.uint32),
        "uint32_large": rng.integers(0, 3_000_000, (C, H, W)).astype(np.uint32),
    }
    for name in ("float_0_1", "float_0_55", "float64_counts", "uint32_small", "uint32_large"):
        cases[name][:, :10, :10] = 0
    return cases


CASES = _source_cases()


def _with_nonfinite():
    data = _float01(seed=4) * 10
    data[0, 100, 100] = np.nan
    data[1, 200, 200] = np.inf
    data[2, 300, 300] = -np.inf
    return data


def _inverse(mapped, transform, channel):
    entry = transform[channel]
    return mapped.astype(np.float64) / entry["scale"] + entry["offset"]


def _assert_mapped(source, mapped, transform, channel):
    """What every mapped channel must satisfy, against its source."""
    assert mapped.dtype == np.uint16
    src = source.astype(np.float64).ravel()
    out = mapped.astype(np.float64).ravel()
    finite = np.isfinite(src)
    src, out = src[finite], out[finite]
    # Order kept (non-decreasing in source order) where not clipped.
    order = np.argsort(src, kind="stable")
    assert np.all(np.diff(out[order]) >= 0)
    # Distinct source values stay distinguishable at the extremes.
    assert out[src == src.max()].min() > out[src == src.min()].max() or src.max() == src.min()
    # Zero stays zero for non-negative data.
    if src.min() >= 0:
        assert np.all(out[src == 0] == 0)
    # The transform inverts to the source within one mapped step.
    entry = transform[channel]
    back = out / entry["scale"] + entry["offset"]
    unclipped = out < 65535
    step = 1.0 / entry["scale"]
    assert np.all(np.abs(back[unclipped] - src[unclipped]) <= step * 0.5 + 1e-6 * max(1.0, abs(src).max()))


# -- present on a zarr array / group ----------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_present_maps_a_flat_array(name):
    source = CASES[name]
    array = zarr.array(source, chunks=(1, 256, 256))
    overview = source[:, ::4, ::4].copy()
    pyramid, mapped_overview = value_range.present(array, overview)
    assert pyramid is not array
    assert len(pyramid) == 1
    level = pyramid["0"]
    assert level.dtype == np.uint16
    transform = pyramid.value_transform
    assert len(transform) == C
    for c in range(C):
        mapped = np.asarray(level[c, :, :])
        _assert_mapped(source[c], mapped, transform, c)
    whole = np.asarray(level[...])
    assert whole.shape == source.shape and whole.dtype == np.uint16
    # The overview in the tiles' units: mapping the source overview gives it.
    ov = np.asarray(mapped_overview, dtype=np.float64)
    assert ov.shape == overview.shape
    for c in range(C):
        tiles_units = np.asarray(level[c, ::4, ::4]).astype(np.float64)
        assert np.all(np.abs(ov[c] - tiles_units) <= 0.5 + 1e-9)


def test_integers_that_fit_read_exactly():
    for name in ("int16_negative", "int8", "uint32_small", "float64_counts"):
        source = CASES[name]
        pyramid, _ = value_range.present(zarr.array(source), None)
        low = np.minimum(source.reshape(C, -1).min(axis=1), 0).astype(np.float64)
        for c in range(C):
            assert pyramid.value_transform[c]["scale"] == 1.0, name
            assert np.array_equal(np.asarray(pyramid["0"][c]).astype(np.int64),
                                  source[c].astype(np.int64) - int(low[c])), name


def test_a_unit_float_uses_the_range():
    pyramid, _ = value_range.present(zarr.array(CASES["float_0_1"]), None)
    for c in range(C):
        mapped = np.asarray(pyramid["0"][c])
        assert mapped.max() == 65535
        # More than a handful of grey levels: the old cast gave 0 and 1.
        assert np.unique(mapped).size > 10000


def test_a_large_uint32_channel_does_not_wrap():
    source = CASES["uint32_large"]
    pyramid, _ = value_range.present(zarr.array(source), None)
    mapped = np.asarray(pyramid["0"][0])
    peak = np.unravel_index(np.argmax(source[0]), source[0].shape)
    assert mapped[peak] == 65535
    assert mapped.max() == 65535


def test_channels_are_mapped_each_on_their_own():
    source = CASES["uint32_small"].copy()
    source[2] = source[2] * 50            # only channel 2 overflows uint16
    pyramid, _ = value_range.present(zarr.array(source), None)
    assert pyramid.value_transform[0]["scale"] == 1.0
    assert pyramid.value_transform[2]["scale"] < 1.0
    assert np.array_equal(np.asarray(pyramid["0"][0]), source[0].astype(np.uint16))


def test_nan_and_inf():
    source = _with_nonfinite()
    overview = source[:, ::2, ::2].copy()
    pyramid, mapped_overview = value_range.present(zarr.array(source), overview)
    level = pyramid["0"]
    planes = [np.asarray(level[c, :, :]) for c in range(C)]
    assert planes[0][100, 100] == 0              # NaN is no signal
    assert planes[1][200, 200] == 65535          # +inf saturates
    assert planes[2][300, 300] == 0              # -inf floors
    # The range is the finite values': the finite peak is the top.
    finite_peak = np.unravel_index(np.nanargmax(np.where(np.isfinite(source[0]), source[0], -1)),
                                   source[0].shape)
    assert planes[0][finite_peak] == 65535
    # The overview the contrast windows are computed from holds no NaN/inf.
    assert np.all(np.isfinite(np.asarray(mapped_overview, dtype=np.float64)))


def test_a_single_pixel_reads_like_a_slice():
    # `level[c, y, x]` is a read of one value; it must agree with the slice.
    source = CASES["float_0_55"]
    pyramid, _ = value_range.present(zarr.array(source), None)
    level = pyramid["0"]
    expected = np.asarray(level[1, 5:6, 7:8])[0, 0]
    assert level[1, 5, 7] == expected


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_eight_and_sixteen_bit_are_untouched(dtype):
    source = np.random.default_rng(0).integers(0, np.iinfo(dtype).max, (C, 64, 64)).astype(dtype)
    array = zarr.array(source)
    overview = source[:, ::2, ::2]
    out, out_overview = value_range.present(array, overview)
    assert out is array
    assert out_overview is overview
    group = zarr.group()
    group.create_array("0", data=source)
    group.create_array("1", data=source[:, ::2, ::2])
    out, _ = value_range.present(group, None)
    assert out is group


def test_present_maps_every_level_of_a_group_with_one_map():
    source = CASES["float_0_55"]
    group = zarr.group()
    group.create_array("0", data=source, chunks=(1, 256, 256))
    group.create_array("1", data=source[:, ::2, ::2], chunks=(1, 256, 256))
    pyramid, _ = value_range.present(group, None)
    assert len(pyramid) == 2
    assert pyramid.level_shapes == [[H, W], [-(-H // 2), -(-W // 2)]]
    for c in range(C):
        level0 = np.asarray(pyramid["0"][c])
        level1 = np.asarray(pyramid["1"][c])
        # The same map at both levels: level 1 is level 0's every other pixel.
        assert np.array_equal(level1, level0[::2, ::2])


# -- end to end: LocalImageProvider.open + read_tile ------------------------


def _write(path, data, pyramid):
    with tifffile.TiffWriter(path, bigtiff=True) as writer:
        writer.write(data, photometric="minisblack", tile=(256, 256), metadata=None,
                     subifds=2 if pyramid else 0)
        if pyramid:
            for step in (2, 4):
                writer.write(data[:, ::step, ::step], subfiletype=1, photometric="minisblack",
                             tile=(256, 256), metadata=None)


def _tiles(channels, channel, level, shape, size=256):
    height, width = shape
    rows = []
    for ty in range(-(-height // size)):
        row = [np.asarray(data_model.read_tile(channels, channel, level, f"{tx}_{ty}", size, size))
               for tx in range(-(-width // size))]
        rows.append(np.concatenate(row, axis=1))
    return np.concatenate(rows, axis=0)


@pytest.mark.parametrize("pyramid", [False, True])
@pytest.mark.parametrize("name", ["float_0_1", "float_0_55", "float_negative",
                                  "int16_negative", "int8", "uint32_large"])
def test_a_file_opens_as_uint16_tiles_through_the_map(tmp_path, name, pyramid):
    source = CASES[name]
    path = tmp_path / f"{name}.tif"
    _write(path, source, pyramid)
    channels, overview, _ = LocalImageProvider(str(path)).open()
    transform = channels.value_transform
    assert len(transform) == C
    for c in range(C):
        tiles = _tiles(channels, c, 0, (H, W))
        assert tiles.shape == (H, W)
        assert tiles.dtype == np.uint16
        _assert_mapped(source[c], tiles, transform, c)
    if pyramid:
        own = source[:, ::2, ::2]
        coarse = _tiles(channels, 1, 1, own.shape[-2:])
        assert coarse.dtype == np.uint16
        _assert_mapped(own[1], coarse, transform, 1)
    # The overview is in the tiles' units: within the mapped range, and its
    # channel means match the mapped full-resolution means.
    ov = np.asarray(overview, dtype=np.float64)
    assert ov.shape[0] == C
    assert np.all(np.isfinite(ov)) and ov.min() >= 0 and ov.max() <= 65535
    for c in range(C):
        tiles = _tiles(channels, c, 0, (H, W)).astype(np.float64)
        assert abs(ov[c].mean() - tiles.mean()) <= 0.02 * max(tiles.mean(), 1.0) + 1.0, c


@pytest.mark.parametrize("pyramid", [False, True])
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_eight_and_sixteen_bit_files_read_exactly(tmp_path, dtype, pyramid):
    source = np.random.default_rng(3).integers(0, np.iinfo(dtype).max, (C, H, W)).astype(dtype)
    path = tmp_path / "plain.tif"
    _write(path, source, pyramid)
    channels, _, _ = LocalImageProvider(str(path)).open()
    assert not hasattr(channels, "value_transform")
    for c in range(C):
        tiles = _tiles(channels, c, 0, (H, W))
        assert np.array_equal(tiles.astype(np.int64), source[c].astype(np.int64))


def test_a_nan_float_file_opens(tmp_path):
    source = _with_nonfinite()
    path = tmp_path / "nan.tif"
    _write(path, source, pyramid=False)
    channels, overview, _ = LocalImageProvider(str(path)).open()
    tile = np.asarray(data_model.read_tile(channels, 0, 0, "0_0", 256, 256))
    assert tile.dtype == np.uint16 and tile[100, 100] == 0
    assert np.all(np.isfinite(np.asarray(overview, dtype=np.float64)))
