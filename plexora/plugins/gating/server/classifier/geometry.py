"""The table's cells as row-aligned arrays: ids, positions, size.

The classifier's own, and not automatic gating's `autogate.cells`, which
holds the same few arrays: the classifier is Free and the autogate package is
the Paid half that is to ship apart (docs/LICENSING.md, "splitting out
plexora-ai"), so nothing here may import it.
"""

from __future__ import annotations

import re

import numpy as np

#: Metadata columns that hold a cell's size, most specific first.
AREA_COLUMN = re.compile(r"^(cell_)?(area|nucleus_area|size)$", re.I)


class Cells:
    """Row-aligned arrays for one table. `valid` = finite position and an id."""

    __slots__ = ("ids", "xs", "ys", "valid", "area", "n")

    def __init__(self, ids, xs, ys, valid, area):
        self.ids, self.xs, self.ys, self.valid, self.area = ids, xs, ys, valid, area
        self.n = int(xs.shape[0])


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
        area = None
        for name in list(ds.table.metadata_columns) + list(ds.table.markers):
            if AREA_COLUMN.match(str(name)):
                try:
                    values = np.asarray(ds.table.columns([name])[name], dtype=np.float64)
                except Exception:
                    continue
                if values.shape[0] == n:
                    area = values
                    break
        return Cells(ids, xs, ys, valid, area)

    return ds.cached(("classifier.cells",), compute)
