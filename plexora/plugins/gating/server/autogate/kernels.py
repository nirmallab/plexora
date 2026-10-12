"""The one spatial loop automatic gating needs, compiled: neighbours in a radius.

Three questions share it -- how crowded is each cell's neighbourhood (the
density strata), what fraction of a cell's neighbours a gate calls positive
(a positive among negatives is the cheapest segmentation or spill signal
there is), and whether a cell has a positive neighbour of ANOTHER marker close
by (a co-expression orphan that is really spill-over, not a wrong gate). Each
is "for these query points, count the table's points within r, and how many of
those carry a flag".

A grid hash does it in one pass. Points are bucketed by a square grid whose
side is at least `r`, so every neighbour within `r` sits in the 3x3 buckets
around the query's own; the buckets are one argsort of the bucket keys (numpy)
and the counting loop is compiled (`plexora.server.utils.jit.njit`). 2M cells at
a 60-pixel radius is well under a second; `scipy.spatial.cKDTree` is the
oracle the tests compare against.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.server.utils.jit import njit

#: Buckets the grid may have; past this the bucket side grows beyond `r`, which
#: stays correct (the 3x3 search still covers the radius) and bounds memory.
MAX_BUCKETS = 16_000_000


class Grid:
    """Points bucketed for radius queries. Built once per (table, radius)."""

    __slots__ = ("xs", "ys", "x0", "y0", "side", "nx", "ny", "order", "starts", "radius")

    def __init__(self, xs, ys, radius):
        xs = np.ascontiguousarray(xs, dtype=np.float64)
        ys = np.ascontiguousarray(ys, dtype=np.float64)
        finite = np.isfinite(xs) & np.isfinite(ys)
        if not finite.all():
            # Parked far outside every bucket a real point can reach.
            xs = np.where(finite, xs, np.inf)
            ys = np.where(finite, ys, np.inf)
        radius = float(radius)
        if not radius > 0:
            raise ValueError("radius must be positive")
        if finite.any():
            x0, x1 = float(xs[finite].min()), float(xs[finite].max())
            y0, y1 = float(ys[finite].min()), float(ys[finite].max())
        else:
            x0 = x1 = y0 = y1 = 0.0
        side = radius
        extent = max(x1 - x0, y1 - y0, radius)
        if (extent / side + 1) ** 2 > MAX_BUCKETS:
            side = extent / (math.sqrt(MAX_BUCKETS) - 1)
        nx = int((x1 - x0) // side) + 1
        ny = int((y1 - y0) // side) + 1
        gx = np.where(finite, np.floor((np.where(finite, xs, x0) - x0) / side), 0).astype(np.int64)
        gy = np.where(finite, np.floor((np.where(finite, ys, y0) - y0) / side), 0).astype(np.int64)
        np.clip(gx, 0, nx - 1, out=gx)
        np.clip(gy, 0, ny - 1, out=gy)
        key = gy * nx + gx
        # Non-finite points get a key past every real bucket, so they are
        # never visited.
        key = np.where(finite, key, nx * ny)
        self.order = np.argsort(key, kind="stable").astype(np.int64)
        counts = np.bincount(key, minlength=nx * ny + 1)[:nx * ny]
        self.starts = np.zeros(nx * ny + 1, dtype=np.int64)
        np.cumsum(counts, out=self.starts[1:])
        self.xs, self.ys = xs, ys
        self.x0, self.y0, self.side, self.nx, self.ny = x0, y0, side, nx, ny
        self.radius = radius


@njit
def _count(qx, qy, qself, xs, ys, flags, r2, order, starts, x0, y0, side, nx, ny,
           out_n, out_f):
    for i in range(qx.shape[0]):
        x = qx[i]
        y = qy[i]
        if not (np.isfinite(x) and np.isfinite(y)):
            out_n[i] = 0
            out_f[i] = 0
            continue
        gx = int((x - x0) // side)
        gy = int((y - y0) // side)
        n = 0
        f = 0
        for by in range(gy - 1, gy + 2):
            if by < 0 or by >= ny:
                continue
            for bx in range(gx - 1, gx + 2):
                if bx < 0 or bx >= nx:
                    continue
                b = by * nx + bx
                for k in range(starts[b], starts[b + 1]):
                    j = order[k]
                    if j == qself[i]:
                        continue
                    dx = xs[j] - x
                    dy = ys[j] - y
                    if dx * dx + dy * dy <= r2:
                        n += 1
                        if flags[j]:
                            f += 1
        out_n[i] = n
        out_f[i] = f


def neighbour_counts(grid, query=None, flags=None, radius=None):
    """(n_neighbours, n_flagged) within `radius` (default the grid's) of each
    query point, the point itself excluded.

    `query` is an index array into the grid's points (default all of them);
    `flags` a boolean array over the grid's points (default none flagged).
    """
    radius = grid.radius if radius is None else float(radius)
    if radius > grid.side + 1e-9:
        raise ValueError("a radius larger than the grid's bucket needs its own grid")
    if query is None:
        query = np.arange(grid.xs.shape[0], dtype=np.int64)
    query = np.ascontiguousarray(query, dtype=np.int64)
    # Visited in bucket order, so consecutive queries read the same few
    # buckets from cache; answers are scattered back to the caller's order.
    visit = np.argsort(_bucket_of(grid, grid.xs[query], grid.ys[query]), kind="stable")
    query_sorted = query[visit]
    if flags is None:
        flags = np.zeros(grid.xs.shape[0], dtype=np.bool_)
    flags = np.ascontiguousarray(flags, dtype=np.bool_)
    sorted_n = np.zeros(query.shape[0], dtype=np.int64)
    sorted_f = np.zeros(query.shape[0], dtype=np.int64)
    if query.shape[0]:
        _count(grid.xs[query_sorted], grid.ys[query_sorted], query_sorted, grid.xs,
               grid.ys, flags, radius * radius, grid.order, grid.starts, grid.x0, grid.y0,
               grid.side, grid.nx, grid.ny, sorted_n, sorted_f)
    out_n = np.empty_like(sorted_n)
    out_f = np.empty_like(sorted_f)
    out_n[visit] = sorted_n
    out_f[visit] = sorted_f
    return out_n, out_f


def _bucket_of(grid, xs, ys):
    finite = np.isfinite(xs) & np.isfinite(ys)
    gx = np.floor((np.where(finite, xs, grid.x0) - grid.x0) / grid.side)
    gy = np.floor((np.where(finite, ys, grid.y0) - grid.y0) / grid.side)
    key = np.clip(gy, 0, grid.ny - 1) * grid.nx + np.clip(gx, 0, grid.nx - 1)
    return np.where(finite, key, grid.nx * grid.ny).astype(np.int64)


def neighbour_counts_at(grid, qx, qy, flags=None, radius=None):
    """`neighbour_counts` for arbitrary points that are not in the grid."""
    radius = grid.radius if radius is None else float(radius)
    if radius > grid.side + 1e-9:
        raise ValueError("a radius larger than the grid's bucket needs its own grid")
    qx = np.ascontiguousarray(qx, dtype=np.float64)
    qy = np.ascontiguousarray(qy, dtype=np.float64)
    if flags is None:
        flags = np.zeros(grid.xs.shape[0], dtype=np.bool_)
    flags = np.ascontiguousarray(flags, dtype=np.bool_)
    out_n = np.zeros(qx.shape[0], dtype=np.int64)
    out_f = np.zeros(qx.shape[0], dtype=np.int64)
    if qx.shape[0]:
        qself = np.full(qx.shape[0], -1, dtype=np.int64)
        _count(qx, qy, qself, grid.xs, grid.ys, flags, radius * radius, grid.order,
               grid.starts, grid.x0, grid.y0, grid.side, grid.nx, grid.ny, out_n, out_f)
    return out_n, out_f


def prime():
    xs = np.array([0.0, 1.0, 5.0, 9.0], dtype=np.float64)
    ys = np.array([0.0, 1.0, 5.0, 9.0], dtype=np.float64)
    grid = Grid(xs, ys, 2.0)
    neighbour_counts(grid, flags=np.array([True, False, True, False]))
    neighbour_counts_at(grid, np.array([0.5]), np.array([0.5]))
