"""The table's cells as arrays: ids, positions, size, and the neighbour grid.

Every automatic-gating statistic that has a *where* in it -- tile residuals,
edge enrichment, neighbour consistency, spatially spread samples -- needs the
same few row-aligned arrays. Built once per handle set and cached, so forty
markers pay for one geometry read and one neighbour grid.
"""

from __future__ import annotations

import re

import numpy as np

#: Metadata columns that hold a cell's size, most specific first.
AREA_COLUMN = re.compile(r"^(cell_)?(area|nucleus_area|size)$", re.I)

#: Radius of "a cell's neighbourhood": ~3 cell diameters.
NEIGHBOUR_RADIUS_UM = 30.0
NEIGHBOUR_RADIUS_PX = 60.0


class Cells:
    """Row-aligned arrays for one table. `valid` = finite position and an id."""

    __slots__ = ("ids", "xs", "ys", "valid", "area", "area_column", "pixel_um", "n")

    def __init__(self, ids, xs, ys, valid, area, area_column, pixel_um):
        self.ids, self.xs, self.ys, self.valid = ids, xs, ys, valid
        self.area, self.area_column, self.pixel_um = area, area_column, pixel_um
        self.n = int(xs.shape[0])

    def radius_px(self, um=NEIGHBOUR_RADIUS_UM, px=NEIGHBOUR_RADIUS_PX):
        return float(um / self.pixel_um) if self.pixel_um else float(px)


def pixel_um(ds):
    def compute():
        from plexora.server.utils import pixel_scale

        found = pixel_scale.pixel_size(ds.project)
        return float(found["value"]) if found else None

    return ds.cached(("autogate.pixel_um",), compute)


def cells(ds) -> Cells:
    """The table's geometry, cached on the handle set."""
    def compute():
        from plexora.server.utils.label_overlay import cell_ids

        frame = ds.table.geometry()
        schema = ds.schema
        n = int(frame.height) if frame is not None else 0
        ids = np.full(n, -1, dtype=np.int64)
        if frame is not None:
            raw_ids, keep = cell_ids(frame, schema.cell_id if schema else None)
            ids[keep] = raw_ids.astype(np.int64)
        xs = np.full(n, np.nan)
        ys = np.full(n, np.nan)
        if frame is not None and schema and schema.x in frame.columns \
                and schema.y in frame.columns:
            xs = frame[schema.x].to_numpy().astype(np.float64)
            ys = frame[schema.y].to_numpy().astype(np.float64)
        valid = np.isfinite(xs) & np.isfinite(ys) & (ids >= 0)
        area, area_column = None, None
        for name in list(ds.table.metadata_columns) + list(ds.table.markers):
            if AREA_COLUMN.match(str(name)):
                try:
                    values = ds.table.columns([name])[name]
                except Exception:
                    continue
                area = np.asarray(values, dtype=np.float64)
                if area.shape[0] == n:
                    area_column = name
                    break
                area = None
        return Cells(ids, xs, ys, valid, area, area_column, pixel_um(ds))

    return ds.cached(("autogate.cells",), compute)


def grid(ds, radius):
    """The neighbour grid over every valid cell, for one radius. Cached."""
    from plexora.plugins.gating.server.autogate import kernels

    def compute():
        c = cells(ds)
        xs = np.where(c.valid, c.xs, np.nan)
        ys = np.where(c.valid, c.ys, np.nan)
        return kernels.Grid(xs, ys, radius)

    return ds.cached(("autogate.grid", round(float(radius), 3)), compute)


def neighbour_density(ds, radius=None):
    """Neighbours within `radius` for every row (0 for invalid rows). Cached."""
    from plexora.plugins.gating.server.autogate import kernels

    c = cells(ds)
    radius = c.radius_px() if radius is None else float(radius)

    def compute():
        n, _ = kernels.neighbour_counts(grid(ds, radius))
        return np.where(c.valid, n, 0)

    return ds.cached(("autogate.density", round(radius, 3)), compute)


def values(ds, marker):
    """The marker column over every row, float32 with NaN -- and NaN as well in
    the rows QC left out of estimation (plexora/agent/cell_exclusions.py), so
    every `isfinite` below skips them: fits, strata, flips, partners."""
    from plexora.agent import cell_exclusions

    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float32)
    return cell_exclusions.masked(ds, marker, raw)


def eligible(ds, marker=None):
    """`Cells.valid` without the rows QC left out (for `marker`, when given)."""
    from plexora.agent import cell_exclusions

    c = cells(ds)
    keep = cell_exclusions.row_mask(ds, marker)
    return c.valid if keep is None or keep.shape[0] != c.n else c.valid & keep


def seeded_subset(indices, size, seed):
    indices = np.asarray(indices)
    if indices.shape[0] <= size:
        return indices
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(indices, size=size, replace=False))


def spearman(a, b, *, size=200_000, seed=0):
    """Spearman's rho over the rows where both are finite (seeded subsample)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    both = np.flatnonzero(np.isfinite(a) & np.isfinite(b))
    if both.shape[0] < 20:
        return None
    both = seeded_subset(both, size, seed)
    ra = np.argsort(np.argsort(a[both], kind="stable"), kind="stable").astype(np.float64)
    rb = np.argsort(np.argsort(b[both], kind="stable"), kind="stable").astype(np.float64)
    ra -= ra.mean()
    rb -= rb.mean()
    denominator = np.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / denominator) if denominator > 0 else None


def grouped_median(groups, values, n_groups):
    """Median of `values` per integer group in [0, n_groups); NaN when empty."""
    groups = np.asarray(groups, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    out = np.full(n_groups, np.nan)
    if not groups.shape[0]:
        return out, np.zeros(n_groups, dtype=np.int64)
    order = np.lexsort((values, groups))
    g, v = groups[order], values[order]
    counts = np.bincount(g, minlength=n_groups)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    present = counts > 0
    lo = starts + (counts - 1) // 2
    hi = starts + counts // 2
    out[present] = (v[lo[present]] + v[hi[present]]) / 2
    return out, counts
