"""Resolution levels for an image that is already an array in this process.

The notebook case: somebody has a numpy (or zarr, or dask) array in a kernel and
wants to look at it, and writing an OME-TIFF first is the step this exists to
remove. A viewer needs a PYRAMID -- it asks for the whole field of view at level
n before it asks for a tile at level 0 -- and a bare array is one level.

What comes out is an `ome_zarr.NgffPyramid`, not a new class. That type is
already exactly the shape everything downstream reads (`group[str(level)]`, a
`len()`, levels indexed `[channel, rows, cols]`, and deliberately no `.shape` --
see its module docstring for why each of those is load-bearing), and a second
implementation of the same duck-type would be a second thing to keep in step
with `read_tile`, `quantization_window_of` and the node's single-plane test.

Two rules the derivation follows, and both are matched to what the file-based
paths already do rather than chosen fresh:

- **An image is mean-pooled**, level by level, through `ome_zarr._reduce2` --
  the same halving, with the same edge replication, that `build_extension`
  writes into a derived store. A pyramid built here and one built from the same
  array written to disk therefore hold the same numbers.
- **Labels are subsampled, never averaged.** The mean of two cell ids is a third
  cell's id. `pyramidize_segmentation_mask` takes every `factor`-th row and
  column of level 0; so does this, which for a numpy array makes every coarse
  level a strided VIEW and costs no memory at all.

Level 0 is always the caller's own array, never a copy. The derived levels an
image gains add about a third of it, which is the price of being able to zoom
out; an array too large to pay that is one that belongs on disk, and
`plexora.memory` says so before it gets here.
"""

from __future__ import annotations

import numpy as np

from plexora.server.utils.ome_zarr import (
    EXTENSION_TARGET,
    NgffPyramid,
    _cast_like,
    _reduce2,
)

#: Slab height for the pooling pass, in bytes of source read at a time. The
#: same bound `build_extension` uses: deriving a level from a 50000-row plane
#: must not need the plane in RAM even when the plane is a lazy zarr array.
_SLAB_BYTES = 256 * 1024 * 1024


def image_pyramid(array, target: int = EXTENSION_TARGET) -> NgffPyramid:
    """`array` as a zoomable pyramid, finest level first.

    `array` is (channel, rows, cols) and may be anything that slices like one --
    numpy, zarr, dask. Level 0 is handed through untouched, so a caller holding
    a lazy array keeps it lazy for full-resolution tiles; only the derived
    levels are materialized, and each is derived from the one above it in
    bounded slabs rather than from level 0 in one read.
    """
    array = _lazy_safe(_as_3d(array))
    levels = [array]
    height, width = int(array.shape[-2]), int(array.shape[-1])
    channels = int(array.shape[0])
    dtype = np.dtype(array.dtype)
    source = array
    while max(height, width) > target:
        out_height, out_width = -(-height // 2), -(-width // 2)
        derived = np.empty((channels, out_height, out_width), dtype=dtype)
        rows = max(1, _SLAB_BYTES // max(1, width * dtype.itemsize * 2))
        for channel in range(channels):
            for start in range(0, out_height, rows):
                stop = min(start + rows, out_height)
                block = np.asarray(source[channel, start * 2:min(stop * 2, height)])
                derived[channel, start:stop, :] = _cast_like(_reduce2(block), dtype)
        levels.append(derived)
        source = derived
        height, width = out_height, out_width
    return NgffPyramid(levels, axes=("c", "y", "x"))


def label_pyramid(array, target: int = EXTENSION_TARGET) -> NgffPyramid:
    """A 2-D label mask as a zoomable pyramid, finest level first.

    Every coarse level is `level0[::factor, ::factor]` with `factor = 2**level`,
    which is what `pyramidize_segmentation_mask` writes for a mask on disk. For
    a numpy array each of those is a view, so a mask costs exactly its own
    memory however many levels it gains.

    Nearest-neighbour is not an approximation to be improved on later: label ids
    are names, and the mean of cell 41 and cell 43 is cell 42.
    """
    array = _lazy_safe(_as_2d(array))
    height, width = int(array.shape[-2]), int(array.shape[-1])
    levels = [array]
    factor = 1
    while max(-(-height // factor), -(-width // factor)) > target:
        factor *= 2
        levels.append(array[::factor, ::factor])
    return NgffPyramid(levels, axes=("y", "x"))


class _NumpyLevel:
    """A level that answers every read with a real numpy array.

    Level 0 may be anything that slices -- a zarr array, a dask array, the
    `.data` of an xarray a SpatialData handed over. Those are exactly what
    should stay lazy: only the slab a tile asks for is worth materializing. But
    `read_tile` goes straight on to `.astype`, `.view` and a `np.append`, and
    the tile encoder after it is numpy from end to end, so a dask array reaching
    that far turns a tile request into a graph nobody computes.

    So the laziness is kept and the boundary is drawn here: the slice is taken
    on the source, and what comes back out is materialized. A plain numpy array
    is never wrapped -- `_lazy_safe` hands it straight through, so the ordinary
    case pays nothing at all.
    """

    __slots__ = ("_array", "shape", "ndim", "dtype", "nbytes")

    def __init__(self, array):
        self._array = array
        self.shape = tuple(int(size) for size in array.shape)
        self.ndim = len(self.shape)
        self.dtype = np.dtype(array.dtype)
        self.nbytes = 0  # the source owns its memory; see level_bytes

    def __getitem__(self, index):
        return np.asarray(self._array[index])

    def __array__(self, dtype=None, copy=None):
        values = np.asarray(self._array)
        return values.astype(dtype) if dtype is not None else values


def _lazy_safe(array):
    """`array` if it is already numpy, else a `_NumpyLevel` over it."""
    return array if isinstance(array, np.ndarray) else _NumpyLevel(array)


def _as_3d(array):
    """A (channel, rows, cols) view of what the caller passed.

    A single-channel image is routinely handed over as a bare 2-D array, which
    would otherwise be read as a one-row-per-channel image with as many channels
    as it has rows -- a shape that opens without error and draws nothing
    recognisable.
    """
    ndim = getattr(array, "ndim", None)
    if ndim == 2:
        return array[np.newaxis, ...]
    if ndim != 3:
        raise ValueError(
            f"an image array must be 2-D (rows, cols) or 3-D "
            f"(channels, rows, cols); this one is {ndim}-D")
    return array


def _as_2d(array):
    """A (rows, cols) view of a label mask.

    A mask written by a segmentation pipeline often carries a leading axis of
    length one, which the tile reader -- which slices a mask as `[rows, cols]`
    -- would read as the row axis.
    """
    ndim = getattr(array, "ndim", None)
    if ndim == 3 and int(array.shape[0]) == 1:
        return array[0]
    if ndim != 2:
        raise ValueError(
            f"a label mask must be 2-D (rows, cols); this one is {ndim}-D"
            + (" with more than one leading plane" if ndim == 3 else ""))
    return array


def level_bytes(pyramid) -> int:
    """How much memory this pyramid's levels add up to.

    Reported rather than assumed, because the two builders above differ by an
    order of magnitude in what they cost: an image's derived levels are real
    arrays and a mask's are views of one. Used for the resource fingerprint and
    for the size warning.
    """
    total = 0
    seen = set()
    for index in range(len(pyramid)):
        level = pyramid[index]
        base = getattr(level, "base", None)
        # A strided view of level 0 (the label case) is not memory of its own.
        owner = id(base) if base is not None else id(level)
        if owner in seen:
            continue
        seen.add(owner)
        total += int(getattr(level, "nbytes", 0) or 0)
    return total
