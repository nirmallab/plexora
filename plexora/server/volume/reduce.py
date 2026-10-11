"""Projecting a range of planes into one: max, min, mean, std, median, percentiles.

Streams planes rather than stacking them: a projection tile at level 0 of a
40-plane stack is 40 tile reads, and holding all of them at once is 80 MB for
one 1024-pixel tile -- times six channels arriving together. Max and min are
running `np.maximum/minimum(out=)`; mean and std keep float64 running sums
(exact for any realistic stack of 16-bit planes: 1000 planes of 65535^2 is
4e12, far inside float64's integer range); median and percentiles need every
plane's value at a pixel and are computed per 256x256 block so the stack held
at once stays small.

The percentile is `numpy`'s `lower` method -- an actual pixel value, never an
interpolation between two -- and that choice is recorded in provenance.

Output dtype: the source's for every served operator (a mean or std rounds to
the nearest integer; std of values in [0, M] is at most M/2, so it always
fits), float32 for `sum`, which is why `sum` is never served as a tile.
"""

from __future__ import annotations

import os
import threading
from typing import Callable

import numpy as np

#: At most this many projections computing at once, process-wide
#: (`PLEXORA_PROJECTION_GATE`, default half the cores, at least two). A
#: browser asks for six channels' tiles together and each one is a Z-plane
#: read; unbounded, a scrub through projections queues hundreds of plane reads
#: in parallel and nothing else on the server answers. Two was the old bound,
#: from when every read held its file's lock throughout: the readers decode
#: outside their locks now (volume/chunk_cache.py), so more can overlap.
def _gate_size() -> int:
    try:
        return max(1, int(os.environ.get("PLEXORA_PROJECTION_GATE", "")))
    except ValueError:
        return max(2, (os.cpu_count() or 4) // 2)


GATE = threading.BoundedSemaphore(_gate_size())

#: Block side for the median/percentile path.
_SORT_BLOCK = 256

#: Percentile method, stated in provenance.
PERCENTILE_METHOD = "lower"


def reduce_planes(read_plane: Callable[[int], np.ndarray], z0: int, z1: int,
                  op: str, q: float | None = None, *, dtype=None) -> np.ndarray:
    """Planes z0..z1 (inclusive) reduced by `op`.

    `read_plane(z)` returns one 2-D array; every call must return the same
    shape. `dtype` is the output type for served operators (defaults to the
    first plane's).
    """
    if z1 < z0:
        z0, z1 = z1, z0
    count = z1 - z0 + 1
    with GATE:
        if op in ("max", "min"):
            out = None
            fold = np.maximum if op == "max" else np.minimum
            for z in range(z0, z1 + 1):
                plane = np.asarray(read_plane(z))
                if out is None:
                    out = np.array(plane, copy=True)
                else:
                    fold(out, plane, out=out)
            return out.astype(dtype, copy=False) if dtype is not None else out
        if op in ("mean", "std", "sum"):
            total = None
            squares = None
            first_dtype = None
            for z in range(z0, z1 + 1):
                plane = np.asarray(read_plane(z))
                if first_dtype is None:
                    first_dtype = plane.dtype
                    total = np.zeros(plane.shape, dtype=np.float64)
                    if op == "std":
                        squares = np.zeros(plane.shape, dtype=np.float64)
                values = plane.astype(np.float64)
                total += values
                if squares is not None:
                    squares += values * values
            if op == "sum":
                return total.astype(np.float32)
            mean = total / count
            if op == "mean":
                result = mean
            else:
                variance = np.maximum(squares / count - mean * mean, 0.0)
                result = np.sqrt(variance)
            return _cast(result, dtype or first_dtype)
        if op in ("med", "pct"):
            fraction = 0.5 if op == "med" else float(q if q is not None else 50.0) / 100.0
            return _rank(read_plane, z0, z1, fraction, dtype)
    raise ValueError(f"unknown projection {op!r}")


def _rank(read_plane, z0, z1, fraction, dtype):
    """The `fraction` order statistic of every pixel across planes z0..z1.

    `k = floor(fraction * (Z - 1))`: numpy's `lower` method, so the answer is
    always one of the planes' own values.
    """
    planes = [np.asarray(read_plane(z)) for z in range(z0, z1 + 1)]
    count = len(planes)
    k = int(np.floor(fraction * (count - 1)))
    height, width = planes[0].shape[-2], planes[0].shape[-1]
    out = np.empty((height, width), dtype=dtype or planes[0].dtype)
    for y in range(0, height, _SORT_BLOCK):
        for x in range(0, width, _SORT_BLOCK):
            block = np.stack([plane[y:y + _SORT_BLOCK, x:x + _SORT_BLOCK]
                              for plane in planes])
            out[y:y + _SORT_BLOCK, x:x + _SORT_BLOCK] = np.partition(block, k, axis=0)[k]
    return out


def _cast(values: np.ndarray, dtype) -> np.ndarray:
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.rint(values), info.min, info.max).astype(dtype)
    return values.astype(dtype)


def reduce_stack(stack: np.ndarray, op: str, q: float | None = None, axis: int = 0):
    """The same operators over an in-memory stack (tests, exports, agents)."""
    stack = np.asarray(stack)
    moved = np.moveaxis(stack, axis, 0)
    return reduce_planes(lambda z: moved[z], 0, moved.shape[0] - 1, op, q,
                         dtype=None if op == "sum" else stack.dtype)
