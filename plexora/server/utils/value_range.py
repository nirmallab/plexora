"""Pixel types the viewer has no tile for, presented as the one it has.

Every tile Plexora serves is unsigned 8- or 16-bit, and the readers got there
with `astype('uint16')`. That is the identity for the images the app was built
on and wrong for the rest, silently:

- **float32/float64** -- inForm/Vectra component data (unmixed signal, 0 to a
  few tens), deconvolution and ratio images, ImageJ 32-bit stacks. A cast
  keeps the integer part: a 0-1 image became zeros and ones, a 0-55 one had
  55 grey levels, and both opened black.
- **signed integers** -- a negative value wrapped to the top of the range.
- **32-bit integers** -- anything above 65535 wrapped.

`present` gives such an image a per-channel linear map onto uint16, chosen
once at open from the image's own values, and hands back a pyramid that
applies it on every read (the `NgffPyramid` contract, so the tile route, the
statistics and the overview take the paths they already take) with the
overview mapped the same way, because contrast windows are computed from the
overview and applied to the tiles.

**The map.** For channel `c`: `u = clip(round((v - offset[c]) * scale[c]))`.
`offset` is the channel's minimum when that is negative and 0 otherwise, so
zero stays zero for the images where zero means "no signal". `scale` is 1
when the values already fit and already use the range (integers that fit;
floats that are counts, peak >= 1024): those images read exactly as they did.
Otherwise the channel's peak is mapped to the top of the range -- to a quarter
of it when the peak was measured on a reduced level, where a bright punctum
has been averaged into its neighbours and the full-resolution peak is higher.
Values above that clip.

The map is kept on the pyramid (`value_transform`) so that a reader that needs
the file's own numbers can invert it. Nothing in the viewer shows them yet:
the intensity it reports for a float image is the mapped one.
"""
from __future__ import annotations

import numpy as np

__all__ = ["needs_view", "present", "ValuePyramid"]

#: The most samples read to learn an image's range: the finest level that
#: fits is scanned whole.
_SCAN_SAMPLES = 64 * 1024 * 1024
_SLAB_BYTES = 128 * 1024 * 1024
#: How far above a reduced level's peak the full-resolution peak is allowed
#: to be before it clips.
_REDUCED_HEADROOM = 4.0
_TOP = 65535.0


def needs_view(dtype) -> bool:
    dtype = np.dtype(dtype)
    if dtype.kind == "f":
        return True
    if dtype.kind == "i":
        return True
    return dtype.kind == "u" and dtype.itemsize > 2


class _Level:
    """One level, indexed `[channel, rows, cols]`, read through the map."""

    __slots__ = ("_array", "_offset", "_scale", "shape", "ndim", "dtype", "chunks", "source_dtype")

    def __init__(self, array, offset, scale):
        self._array = array
        self._offset = offset
        self._scale = scale
        self.shape = tuple(int(side) for side in array.shape)
        self.ndim = len(self.shape)
        self.dtype = np.dtype(np.uint16)
        self.source_dtype = np.dtype(array.dtype)
        self.chunks = getattr(array, "chunks", None)

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, key):
        values = np.asarray(self._array[key])
        first = key[0] if isinstance(key, tuple) else key
        if isinstance(first, (int, np.integer)):
            offset, scale = self._offset[int(first)], self._scale[int(first)]
        elif first is Ellipsis:
            offset = self._offset.reshape((-1,) + (1,) * (values.ndim - 1))
            scale = self._scale.reshape((-1,) + (1,) * (values.ndim - 1))
        else:
            picked_offset, picked_scale = self._offset[first], self._scale[first]
            offset = picked_offset.reshape((-1,) + (1,) * (values.ndim - 1))
            scale = picked_scale.reshape((-1,) + (1,) * (values.ndim - 1))
        return _apply(values, offset, scale)

    def __array__(self, dtype=None, copy=None):
        # The source's own `__array__`, not `self[...]`: a region reader's level
        # (`dicom_wsi._MonoLevel`, which the TIFF region path uses too) takes an
        # integer channel and nothing else.
        values = np.asarray(self._array)
        shaped = (-1,) + (1,) * (values.ndim - 1)
        out = _apply(values, self._offset.reshape(shaped), self._scale.reshape(shaped))
        return out.astype(dtype) if dtype is not None else out


def _apply(values, offset, scale):
    # `asarray`, and no in-place repair: one pixel (`level[c, y, x]`) arrives
    # as a scalar, which numpy will not repair in place.
    mapped = (np.asarray(values, dtype=np.float64) - offset) * scale
    mapped = np.nan_to_num(mapped, nan=0.0, posinf=_TOP, neginf=0.0)
    return np.clip(np.rint(mapped), 0, _TOP).astype(np.uint16)


class ValuePyramid:
    """Levels read through a value map; shaped like `ome_zarr.NgffPyramid`
    (not a `zarr.Array`, no `.shape`, `pyramid[str(level)]`)."""

    def __init__(self, levels, offset, scale, source):
        self._levels = levels
        self.source = source
        self.value_transform = [{"offset": float(o), "scale": float(s)} for o, s in zip(offset, scale)]

    def __len__(self):
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key):
        try:
            return 0 <= int(key) < len(self._levels)
        except (TypeError, ValueError):
            return False

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    #: What the source pyramid says about itself that is still true of this
    #: one. A list, not a blanket forward: the website decides whether the
    #: PAGE may read a TIFF's tiles itself by asking the pyramid for
    #: `array_keys` (plexora/browser/node_spec.py), and the page reads the
    #: file's own samples -- which for this image are not the tiles' values.
    _FORWARDED = ("multiscale", "axes", "path", "extension", "base_levels",
                  "channel_names", "is_color", "close")

    def __getattr__(self, name):
        if name in self._FORWARDED:
            return getattr(self.source, name)
        raise AttributeError(name)

    @property
    def level_shapes(self):
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]


def _levels_of(channels) -> list:
    if hasattr(channels, "shape"):
        return [channels]
    try:
        count = len(channels)
    except TypeError:
        count = None
    if count is None:
        count = sum(1 for _ in channels.array_keys())
    return [channels[str(index)] for index in range(count)]


def _range(levels):
    """`(low, high, exact)` per channel: the finest level small enough to
    read whole, scanned a slab at a time."""
    index = next((i for i, level in enumerate(levels)
                  if int(np.prod([int(side) for side in level.shape])) <= _SCAN_SAMPLES), len(levels) - 1)
    level = levels[index]
    planes, height, width = (int(side) for side in level.shape)
    itemsize = np.dtype(level.dtype).itemsize
    rows = max(1, _SLAB_BYTES // max(1, width * itemsize))
    low = np.full(planes, np.inf)
    high = np.full(planes, -np.inf)
    for plane in range(planes):
        for y0 in range(0, height, rows):
            slab = np.asarray(level[plane, y0:y0 + rows])
            if slab.dtype.kind == "f":
                slab = slab[np.isfinite(slab)]
            if slab.size:
                low[plane] = min(low[plane], float(slab.min()))
                high[plane] = max(high[plane], float(slab.max()))
    low[~np.isfinite(low)] = 0.0
    high[~np.isfinite(high)] = 0.0
    return low, high, index == 0


def _transform(low, high, exact, dtype):
    kind, small = dtype.kind, dtype.itemsize <= 2
    offset = np.where(low < 0, low, 0.0)
    if kind == "i" and small:
        # int8/int16: the whole type fits once its lowest value is zero.
        return offset, np.ones_like(offset)
    peak = high - offset
    headroom = 1.0 if exact else _REDUCED_HEADROOM
    top = peak * headroom
    scale = np.ones_like(peak)
    for channel in range(peak.size):
        if not top[channel] > 0:
            continue
        fits = top[channel] <= _TOP
        if kind in "iu":
            keep = fits or (exact and peak[channel] <= _TOP)
        else:
            keep = fits and peak[channel] >= 1024
        if not keep:
            scale[channel] = _TOP / top[channel]
    return offset, scale


def map_as(channels, like: ValuePyramid) -> ValuePyramid:
    """`channels` read through the map `like` was given: another plane of the
    same stack, which must draw on the same scale as the plane the map was
    chosen from, or moving through z would change the contrast."""
    offset = np.array([entry["offset"] for entry in like.value_transform])
    scale = np.array([entry["scale"] for entry in like.value_transform])
    levels = _levels_of(channels)
    return ValuePyramid([_Level(level, offset, scale) for level in levels], offset, scale,
                        channels)


def present(channels, overview):
    """`(channels, overview)` as the viewer can draw them: untouched for an
    8- or 16-bit unsigned image, and through a value map for anything else."""
    try:
        levels = _levels_of(channels)
        if not levels or len(levels[0].shape) != 3 or not needs_view(levels[0].dtype):
            return channels, overview
        low, high, exact = _range(levels)
        offset, scale = _transform(low, high, exact, np.dtype(levels[0].dtype))
    except Exception:  # noqa: BLE001 -- an image that reads as before beats one that does not open
        import logging

        logging.getLogger(__name__).warning("No value map for this image", exc_info=True)
        return channels, overview
    pyramid = ValuePyramid([_Level(level, offset, scale) for level in levels], offset, scale, channels)
    if overview is not None and getattr(overview, "ndim", 0) == 3 and overview.shape[0] == offset.size:
        shaped = (-1, 1, 1)
        # NaN and the infinities as the tiles map them (a pooled block that
        # held one NaN is NaN): contrast windows are computed from this.
        overview = np.clip(np.nan_to_num(
            (np.asarray(overview, dtype=np.float64) - offset.reshape(shaped)) * scale.reshape(shaped),
            nan=0.0, posinf=_TOP, neginf=0.0), 0, _TOP)
    return pyramid, overview
