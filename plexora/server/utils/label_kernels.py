"""Label-mask loops, compiled: per-label boxes and the outline rule.

`label_overlay.boundary_mask` is the viewer's outline rule written as numpy
shifted-slice comparisons -- (2r+1)^2 - 1 full-array passes, each allocating.
`boundary_mask_kernel` is the same rule as one pass over the pixels, and
`label_overlay` dispatches to it when kernels are compiled. `label_bboxes`
answers what no stored table does -- where each cell's mask actually is, and
how big -- in one pass over a crop, for the gating evidence that checks a
table's centroid against the mask it came from.

Every function has a pure-numpy twin in the tests; see plexora/server/utils/jit.py.
"""

from __future__ import annotations

import numpy as np

from plexora.server.utils.jit import njit


@njit
def _boundary(labels, radius, out):
    height, width = labels.shape
    for y in range(height):
        for x in range(width):
            value = labels[y, x]
            if value == 0:
                continue
            y0 = max(0, y - radius)
            y1 = min(height - 1, y + radius)
            x0 = max(0, x - radius)
            x1 = min(width - 1, x + radius)
            found = False
            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    if labels[yy, xx] != value:
                        found = True
                        break
                if found:
                    break
            out[y, x] = found
    return out


def boundary_mask_kernel(labels, radius=1):
    """True where a labelled pixel has a differently-labelled neighbour within
    `radius` (Chebyshev); neighbours outside the array do not count.

    Identical to `label_overlay.boundary_mask` pixel for pixel.
    """
    labels = np.ascontiguousarray(labels)
    if labels.dtype.kind not in "iu":
        labels = labels.astype(np.int64)
    out = np.zeros(labels.shape, dtype=np.bool_)
    if labels.size == 0:
        return out
    return _boundary(labels, max(1, int(radius)), out)


@njit
def _bboxes(labels, ids, stats):
    # stats columns: count, y0, x0, y1, x1, sum_y, sum_x
    height, width = labels.shape
    n = ids.shape[0]
    for y in range(height):
        for x in range(width):
            value = labels[y, x]
            if value == 0:
                continue
            # ids is sorted: binary search.
            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) >> 1
                if ids[mid] < value:
                    lo = mid + 1
                else:
                    hi = mid
            if lo >= n or ids[lo] != value:
                continue
            row = lo
            if stats[row, 0] == 0:
                stats[row, 1] = y
                stats[row, 2] = x
                stats[row, 3] = y
                stats[row, 4] = x
            else:
                if y < stats[row, 1]:
                    stats[row, 1] = y
                if x < stats[row, 2]:
                    stats[row, 2] = x
                if y > stats[row, 3]:
                    stats[row, 3] = y
                if x > stats[row, 4]:
                    stats[row, 4] = x
            stats[row, 0] += 1
            stats[row, 5] += y
            stats[row, 6] += x
    return stats


def label_bboxes(labels, ids=None):
    """Per label: `{ids, count, y0, x0, y1, x1, cy, cx}` as arrays aligned with
    `ids` (sorted; default every non-zero label present).

    Bounds are inclusive pixel indices; `cy, cx` the mask centroid. A label in
    `ids` that is absent has count 0 and NaN centroid.
    """
    labels = np.ascontiguousarray(labels)
    if ids is None:
        ids = np.unique(labels)
        ids = ids[ids != 0]
    ids = np.ascontiguousarray(np.sort(np.asarray(ids)).astype(labels.dtype, copy=False))
    stats = np.zeros((ids.shape[0], 7), dtype=np.int64)
    if ids.shape[0] and labels.size:
        _bboxes(labels, ids, stats)
    count = stats[:, 0]
    with np.errstate(invalid="ignore", divide="ignore"):
        cy = np.where(count > 0, stats[:, 5] / np.maximum(count, 1), np.nan)
        cx = np.where(count > 0, stats[:, 6] / np.maximum(count, 1), np.nan)
    return {"ids": ids, "count": count, "y0": stats[:, 1], "x0": stats[:, 2],
            "y1": stats[:, 3], "x1": stats[:, 4], "cy": cy, "cx": cx}


def prime():
    tiny = np.zeros((6, 6), dtype=np.uint32)
    tiny[1:4, 1:4] = 3
    tiny[4:6, 4:6] = 7
    boundary_mask_kernel(tiny, 1)
    label_bboxes(tiny)
    # The dtypes the renderers actually hand over.
    boundary_mask_kernel(tiny.astype(np.int32), 2)
    label_bboxes(tiny.astype(np.uint16))
