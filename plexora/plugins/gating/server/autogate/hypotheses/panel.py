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

Positive sentinels (`n_positive_sentinels`, VERSION 6, off until the reader
audit on the person's labelled cells says they help): clear positives from
the brightest tenth, above the highest candidate by the tolerance. They say
whether a look's reader missed plain stain; like the negatives they are
never evidence about any gate.

Every cell is marked `crowded` when its brightest neighbour among the six
nearest is brighter than it by more than `crowding_margin` (fit units):
from the table alone (`classifier.features.table_features`), no pixels.
With `strata_bounds` the cells of each interval between them are counted
by that mark (`crowding`), so the reads of each kind can be weighed by the
cells it holds (`controller.crowding`).
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import sampler

#: A positive sentinel comes from at least this many candidates' worth of
#: cells, or none is shown (a rare marker's own tail look reads those).
MIN_POSITIVE_POOL = 20


def crowded(ds, marker, col, margin):
    """Per row of the table: True when the brightest of the six nearest
    neighbours is brighter by more than `margin` (fit units), False when
    not, None for the whole column when positions or the feature are
    missing."""
    from plexora.plugins.gating.server.classifier import features

    try:
        matrix, names = features.table_features(ds, marker)
    except Exception:  # noqa: BLE001 -- a stratum, never a reason to fail a look
        return None
    if "neighbour_max" not in names:
        return None
    v = np.asarray(matrix[:, names.index("value")], dtype=np.float64)
    peak = np.asarray(matrix[:, names.index("neighbour_max")], dtype=np.float64)
    with np.errstate(invalid="ignore"):
        gap = col.to_fit(peak) - col.to_fit(v)
        return np.where(np.isfinite(gap), gap > float(margin), False)


def panel_cells(ds, marker, intervals, *, exclude=(), n_sentinels=4, sentinel_below=None,
                spatial_k=8, seed=0, ids=(), n_positive_sentinels=0, sentinel_above=None,
                strata_bounds=None, crowding_margin=None) -> dict:
    """{cells, sentinels[, crowding]}: `intervals` is [{from, to, n}] (cells
    with from < value <= to); `exclude` the cell ids already shown; `ids`
    cells chosen by id (a classifier check's), shown too unless excluded."""
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    v32 = v.astype(np.float32)
    rng = np.random.default_rng(seed)
    usable = c.valid & np.isfinite(v32)
    if len(exclude):
        usable &= ~np.isin(c.ids, np.asarray(list(exclude), dtype=c.ids.dtype))
    crowd = None
    if crowding_margin is not None:
        crowd = crowded(ds, marker, col, crowding_margin)
        if crowd is not None and crowd.shape[0] != v.shape[0]:
            crowd = None

    def mark(record, r):
        if crowd is not None:
            record["crowded"] = int(bool(crowd[r]))
        return record

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
            out.append(mark(record, int(r)))
    if len(ids):
        rows = np.flatnonzero(usable & np.isin(c.ids, np.asarray(list(ids), dtype=c.ids.dtype)))
        for r in rows:
            record = sampler._record(c, v, col, int(r), "panel", "chosen", None)
            record["interval"] = None
            out.append(mark(record, int(r)))
    sentinels = []
    if n_sentinels and sentinel_below is not None:
        below = np.flatnonzero(usable & (v32 <= np.float32(sentinel_below)))
        if below.shape[0]:
            order = below[np.argsort(v[below], kind="stable")]
            lower = order[:max(1, order.shape[0] // 2)]
            groups = sampler.spatial_tiles(c, lower, spatial_k)
            picked = sampler.round_robin(lower, groups, int(n_sentinels), rng)
            sentinels = [dict(sampler._record(c, v, col, int(r), "sentinel",
                                              "clear negative", None), sentinel=True,
                              side="negative")
                         for r in picked]
    if n_positive_sentinels and sentinel_above is not None:
        finite = v32[c.valid & np.isfinite(v32)]
        decile = np.float32(np.quantile(finite, 0.9)) if finite.size else np.float32(np.inf)
        pool = np.flatnonzero(usable & (v32 > np.float32(sentinel_above)) & (v32 >= decile))
        if pool.shape[0] >= MIN_POSITIVE_POOL:
            groups = sampler.spatial_tiles(c, pool, spatial_k)
            picked = sampler.round_robin(pool, groups, int(n_positive_sentinels), rng)
            sentinels += [dict(sampler._record(c, v, col, int(r), "sentinel",
                                               "clear positive", None), sentinel=True,
                               side="positive")
                          for r in picked]
    result = {"marker": marker, "cells": out, "sentinels": sentinels, "seed": int(seed)}
    if crowd is not None and strata_bounds:
        live = c.valid & np.isfinite(v32)
        bounds = [np.float32(b) for b in strata_bounds]
        result["crowding"] = [
            {"1": int((live & crowd & (v32 > a) & (v32 <= b)).sum()),
             "0": int((live & ~crowd & (v32 > a) & (v32 <= b)).sum())}
            for a, b in zip(bounds[:-1], bounds[1:])]
    return result


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
