"""The cells of one look: a blind panel.

Cells are drawn from the disputed intervals the controller planned, spread
over each interval's values and over the tissue (`sampler._spread_by_value`),
never one shown before for this marker. A few negative sentinels -- cells in
the lower half of those below the lowest candidate, which no candidate calls
positive -- are mixed in. The whole panel is shuffled (seeded) and each cell
captioned by its number only: no value, call, gate or ranking reaches the
picture or the question. Which interval each cell came from, and how it was
selected, stays with code, so a fraction is never read off the enriched
sample as if it were the image's.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import sampler


def panel_cells(ds, marker, intervals, *, exclude=(), n_sentinels=4, sentinel_below=None,
                spatial_k=8, seed=0, ids=()) -> dict:
    """{cells, sentinels}: `intervals` is [{from, to, n}] (cells with
    from < value <= to); `exclude` the cell ids already shown; `ids` cells
    chosen by id (a classifier check's), shown too unless excluded."""
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    v32 = v.astype(np.float32)
    rng = np.random.default_rng(seed)
    usable = c.valid & np.isfinite(v32)
    if len(exclude):
        usable &= ~np.isin(c.ids, np.asarray(list(exclude), dtype=c.ids.dtype))
    out = []
    for index, iv in enumerate(intervals):
        inside = usable & (v32 > np.float32(iv["from"])) & (v32 <= np.float32(iv["to"]))
        rows = np.flatnonzero(inside)
        picked = sampler._spread_by_value(c, v, rows, int(iv["n"]), spatial_k, rng)
        for r in picked:
            record = sampler._record(c, v, col, int(r), "panel", "disputed", None)
            record["interval"] = index
            record["selection"] = {"interval_cells": int(rows.shape[0]),
                                   "picked": int(len(picked))}
            out.append(record)
    if len(ids):
        rows = np.flatnonzero(usable & np.isin(c.ids, np.asarray(list(ids), dtype=c.ids.dtype)))
        for r in rows:
            record = sampler._record(c, v, col, int(r), "panel", "chosen", None)
            record["interval"] = None
            out.append(record)
    sentinels = []
    if n_sentinels and sentinel_below is not None:
        below = np.flatnonzero(usable & (v32 <= np.float32(sentinel_below)))
        if below.shape[0]:
            order = below[np.argsort(v[below], kind="stable")]
            lower = order[:max(1, order.shape[0] // 2)]
            groups = sampler.spatial_tiles(c, lower, spatial_k)
            picked = sampler.round_robin(lower, groups, int(n_sentinels), rng)
            sentinels = [dict(sampler._record(c, v, col, int(r), "sentinel",
                                              "clear negative", None), sentinel=True)
                         for r in picked]
    return {"marker": marker, "cells": out, "sentinels": sentinels, "seed": int(seed)}


def arrange(cells, sentinels, seed) -> list:
    """The panel's order (seeded shuffle) with each cell numbered 1, 2, ...:
    the number is the caption and the label's key."""
    every = list(cells) + list(sentinels)
    order = np.random.default_rng(seed).permutation(len(every))
    return [dict(every[i], caption=str(n + 1), number=str(n + 1))
            for n, i in enumerate(order)]


def rows_of(arranged, per_row) -> list:
    """Collage rows of `per_row` cells; a row's label is its numbers only."""
    rows = []
    for start in range(0, len(arranged), per_row):
        chunk = arranged[start:start + per_row]
        rows.append({"key": f"r{start // per_row + 1}",
                     "label": f"cells {chunk[0]['number']} to {chunk[-1]['number']}",
                     "cells": chunk})
    return rows
