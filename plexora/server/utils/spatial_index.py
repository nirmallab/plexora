"""Which cell is at a point -- on this frame, on these planes.

The index behind the viewer's right-click and QC's hover. One
`scipy.spatial.cKDTree` over every cell's (x, y), with each row's frame and
plane extent held beside it as plain arrays, and a filter after the tree:

- (x, y) and not (x, y, z), because what a person clicks on a plane is a cell
  with pixels under the cursor. A tall cell clicked forty planes from its
  centroid is still there and still near in x/y; a tree over scaled z would
  rank a small cell on the plane ahead of it.
- one tree and not one per plane or per frame: those multiply the memory by
  the cells' typical depth times the frame count, for an answer the filter
  gives from the hits of one bounded query.
- a 2-D project (no frame asked for, no planes) takes `tree.query(k=1)`,
  unbounded, exactly the nearest-cell answer it has always had.

A cell's planes are its `bbox_z0..bbox_z1` (inclusive) when the table has them
-- the measured 3-D object table does -- else its rounded centroid plane give
or take half its typical depth (`n_planes`), or one plane. A cell with no
usable z matches no plane. Planes asked of a table with no z role are ignored,
as a frame asked of one with no frame role is.

Stateless: the caller holds the index (data_model for the loaded project, QC
per project) and decides when it is stale.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: The measured object table's inclusive plane extent of each cell.
EXTENT_COLUMNS = ("bbox_z0", "bbox_z1")
#: How many planes each cell spans, when the extent itself is not stored.
DEPTH_COLUMN = "n_planes"
#: Its inclusive extent in x and y, which sizes a cell better than spacing.
SIZE_COLUMNS = ("bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1")

#: A filtered pick looks this many typical cell diameters from the point ...
REACH_DIAMETERS = 3.0
#: ... and never less or more than these, in full-resolution pixels.
MIN_REACH_PX = 8.0
MAX_REACH_PX = 256.0

#: A row with no usable z: an extent that overlaps no range of planes.
_NO_Z = (np.iinfo(np.int32).max, np.iinfo(np.int32).min)

#: Bumped whenever what `state()` holds changes meaning; cached copies of an
#: older state are rebuilt rather than read.
STATE_VERSION = 1


def _floats(frame, column):
    import polars as pl

    return frame[column].cast(pl.Float64, strict=False).to_numpy().astype(np.float64)


@dataclass
class SpatialIndex:
    tree: object
    #: Tree position -> table row, or None when every row is in the tree.
    rows: np.ndarray | None
    xs: np.ndarray
    ys: np.ndarray
    frames: np.ndarray | None
    z0: np.ndarray | None
    z1: np.ndarray | None
    #: How far a filtered pick looks (full-resolution pixels).
    reach: float
    #: Typical distance between neighbouring cells (pixels): sqrt(area / n).
    spacing: float
    #: Which columns the extent came from: EXTENT_COLUMNS, DEPTH_COLUMN or
    #: "centroid"; None without a z role.
    extent_from: str | None = None

    # -- building ------------------------------------------------------------

    @classmethod
    def build(cls, frame, *, x, y, z=None, t=None):
        """The index of `frame`'s rows by columns `x`, `y` (and optionally
        `z`, `t`); None when no row has a finite position."""
        from scipy.spatial import cKDTree

        xs, ys = _floats(frame, x), _floats(frame, y)
        finite = np.isfinite(xs) & np.isfinite(ys)
        if not finite.any():
            return None
        rows = None if finite.all() else np.flatnonzero(finite)
        if rows is not None:
            xs, ys = xs[rows], ys[rows]
        columns = set(frame.columns)

        def pick(values):
            return values if rows is None else values[rows]

        frames = None
        if t and t in columns:
            raw = _floats(frame, t)
            frames = pick(np.where(np.isfinite(raw), raw, -1).astype(np.int32))

        z0 = z1 = None
        extent_from = None
        if z and z in columns:
            z0, z1, extent_from = cls._extent(frame, z, columns)
            z0, z1 = pick(z0), pick(z1)

        spacing, reach = cls._reach(frame, xs, ys, columns, pick)
        return cls(tree=cKDTree(np.column_stack([xs, ys])), rows=rows, xs=xs, ys=ys,
                   frames=frames, z0=z0, z1=z1, reach=reach, spacing=spacing,
                   extent_from=extent_from)

    @staticmethod
    def _extent(frame, z, columns):
        if set(EXTENT_COLUMNS) <= columns:
            lo, hi = (_floats(frame, c) for c in EXTENT_COLUMNS)
            ok = np.isfinite(lo) & np.isfinite(hi)
            how = "bbox"
        else:
            centre = _floats(frame, z)
            tol = 1
            if DEPTH_COLUMN in columns:
                depth = _floats(frame, DEPTH_COLUMN)
                depth = depth[np.isfinite(depth)]
                if depth.size:
                    tol = max(1, int(round(float(np.median(depth)) / 2)))
            plane = np.floor(centre + 0.5)
            lo, hi = plane - tol, plane + tol
            ok = np.isfinite(centre)
            how = "centroid"
        z0 = np.full(lo.shape, _NO_Z[0], dtype=np.int32)
        z1 = np.full(lo.shape, _NO_Z[1], dtype=np.int32)
        z0[ok] = lo[ok].astype(np.int32)
        z1[ok] = hi[ok].astype(np.int32)
        return z0, z1, how

    @staticmethod
    def _reach(frame, xs, ys, columns, pick):
        n = xs.size
        extent = max(1.0, float(np.ptp(xs)) * float(np.ptp(ys))) if n > 1 else 1.0
        spacing = float(np.sqrt(extent / max(1, n)))
        diameter = spacing
        if set(SIZE_COLUMNS) <= columns:
            x0, x1, y0, y1 = (pick(_floats(frame, c)) for c in SIZE_COLUMNS)
            sides = np.maximum(x1 - x0, y1 - y0) + 1.0
            sides = sides[np.isfinite(sides)]
            if sides.size:
                diameter = float(np.median(sides))
        reach = float(np.clip(REACH_DIAMETERS * diameter, MIN_REACH_PX, MAX_REACH_PX))
        return spacing, reach

    # -- asking --------------------------------------------------------------

    def __len__(self):
        return int(self.xs.size)

    def _row(self, at):
        return int(at) if self.rows is None else int(self.rows[int(at)])

    def nearest(self, x, y, *, t=None, planes=None, radius=None):
        """(table row, distance in pixels) of the cell nearest (x, y), or None.

        `t` keeps to one frame; `planes` = (first, last) keeps to cells whose
        extent overlaps those planes (inclusive). Either one bounds the search
        at `radius` (default: `reach`), because a cell further than a few cell
        diameters from the cursor is not the one under it. Neither: the
        nearest cell anywhere.
        """
        by_frame = t is not None and self.frames is not None
        by_plane = planes is not None and self.z0 is not None
        if not (by_frame or by_plane):
            distance, at = self.tree.query([float(x), float(y)], k=1)
            if not np.isfinite(distance):
                return None
            return self._row(at), float(distance)
        bound = self.reach if radius is None else float(radius)
        hits = self.tree.query_ball_point([float(x), float(y)], bound)
        if not len(hits):
            return None
        hits = np.asarray(hits, dtype=np.intp)
        keep = np.ones(hits.size, dtype=bool)
        if by_frame:
            keep &= self.frames[hits] == int(t)
        if by_plane:
            lo, hi = int(min(planes)), int(max(planes))
            keep &= (self.z0[hits] <= hi) & (self.z1[hits] >= lo)
        hits = hits[keep]
        if not hits.size:
            return None
        # query_ball_point returns positions in index order, not by distance.
        distances = np.hypot(self.xs[hits] - x, self.ys[hits] - y)
        best = int(np.argmin(distances))
        return self._row(hits[best]), float(distances[best])

    # -- caching -------------------------------------------------------------

    def state(self) -> dict:
        """Everything needed to rebuild this index without the table, for a
        disk cache. Plain arrays and the tree; no reference to this class."""
        return {"version": STATE_VERSION, "tree": self.tree, "rows": self.rows,
                "xs": self.xs, "ys": self.ys, "frames": self.frames,
                "z0": self.z0, "z1": self.z1, "reach": self.reach,
                "spacing": self.spacing, "extent_from": self.extent_from}

    @classmethod
    def from_state(cls, state):
        """The index `state()` described, or None for a state of another
        version (or not a state at all)."""
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return None
        fields = {k: state[k] for k in ("tree", "rows", "xs", "ys", "frames", "z0", "z1",
                                        "reach", "spacing", "extent_from")}
        return cls(**fields)
