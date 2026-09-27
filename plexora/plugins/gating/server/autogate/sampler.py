"""Which cells to show: the few that say the most about where a gate belongs.

A field of tissue spends most of its pixels on cells that inform nothing -- the
bulk of the background and the obvious positives. The boundary is decided by
the cells near it, so the sample is taken in the fit's own space, in strata
laid out around the gate (`schemas.STRATA`), oversampled at the boundary,
spread over the slide (a coarse spatial grid, round-robin), across crowded and
sparse neighbourhoods (density terciles), with the smallest and largest
borderline cells and a few spatially inconsistent ones (a positive with only
negative neighbours, and the converse).

`delta_cells` is the refinement sample: between two candidate thresholds the
only cells whose call changes are the ones in between, so those are the cells
to look at, a few from each interval, plus some that no candidate changes (the
regression set that says nothing else moved).

Deterministic for a seed; every selection is a numpy mask, a `searchsorted`
and a round-robin over at most a few hundred groups.
"""

from __future__ import annotations

import numpy as np

from plexora.agent import gate_rule
from plexora.agent.limits import MAX_IDS
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate.schemas import STRATA

#: A stratum larger than this is thinned (seeded) before the round-robin.
STRATUM_CAP = 50_000


def _band_half(fit, col, low):
    """Half the borderline band in fit units: half the narrower of the two
    populations' sds (`capabilities.fit_band`'s rule), or a percentile band."""
    if fit is not None and len(fit["sds"]) >= 2:
        return 0.5 * profmod.pools(fit)["sd_edge"]
    if not col.n_finite:
        return 1.0
    at = (col.percentile_of(low) or 50.0) / 100.0
    lo, hi = profmod._sorted_quantile(col.fit, np.array([max(0.0, at - 0.05),
                                                         min(1.0, at + 0.05)]))
    return max(1e-9, float(hi - lo) / 2)


def _populations(fit):
    """(mu_bg, sd_bg, mu_pos, sd_pos) in fit units, or None."""
    if fit is None:
        return None
    p = profmod.pools(fit)
    return p["mu_bg"], p["sd_bg"], p["mu_pos"], p["sd_pos"]


def stratum_edges(fit, col, low):
    """The 9 edges (fit units) of the 8 strata, monotone."""
    g = float(col.to_fit(low))
    h = _band_half(fit, col, low)
    pops = _populations(fit)
    if pops is None:
        mu_bg, mu_pos, sd_pos = g - 4 * h, g + 4 * h, h
    else:
        mu_bg, _sd_bg, mu_pos, sd_pos = pops
    inner = np.array([mu_bg, g - 2 * h, g - h / 2, g + h / 2, g + 2 * h, mu_pos,
                      mu_pos + 2 * sd_pos], dtype=np.float64)
    # Never let a population centre cross the band: a gate near the background
    # leaves `clear_negative` empty rather than overlapping `just_below`.
    inner[:2] = np.minimum(inner[:2], inner[2])
    inner[5:] = np.maximum(inner[5:], inner[4])
    inner = np.maximum.accumulate(inner)
    return np.concatenate(([-np.inf], inner, [np.inf])), h


def spatial_tiles(c, rows, k):
    """A k x k grid over the valid cells' bounding box, per row."""
    xs, ys = c.xs[c.valid], c.ys[c.valid]
    if not xs.size:
        return np.zeros(len(rows), dtype=np.int64)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    tx = np.clip(((c.xs[rows] - x0) / max(x1 - x0, 1e-9) * k).astype(np.int64), 0, k - 1)
    ty = np.clip(((c.ys[rows] - y0) / max(y1 - y0, 1e-9) * k).astype(np.int64), 0, k - 1)
    return ty * k + tx


def round_robin(rows, groups, n, rng, *, prefer=None):
    """Up to `n` of `rows`, cycling over `groups` in a seeded order.

    Each group's first pick is its most typical member by `prefer`
    (ascending; e.g. distance from the stratum's median), the rest random, so
    a sample is both typical and not only typical. One lexsort, no loop.
    """
    rows = np.asarray(rows)
    if rows.shape[0] <= n:
        return rows
    _unique, inverse = np.unique(np.asarray(groups), return_inverse=True)
    n_groups = int(inverse.max()) + 1
    group_rank = rng.permutation(n_groups)[inverse]
    key = rng.random(rows.shape[0])
    if prefer is not None:
        by_preference = np.lexsort((np.asarray(prefer), inverse))
        first = np.ones(by_preference.shape[0], dtype=bool)
        first[1:] = inverse[by_preference][1:] != inverse[by_preference][:-1]
        key[by_preference[first]] = -1.0
    within_order = np.lexsort((key, inverse))
    starts = np.zeros(n_groups + 1, dtype=np.int64)
    np.cumsum(np.bincount(inverse, minlength=n_groups), out=starts[1:])
    rank = np.empty(rows.shape[0], dtype=np.int64)
    rank[within_order] = np.arange(rows.shape[0]) - starts[inverse[within_order]]
    chosen = np.lexsort((group_rank, rank))[:n]
    return rows[chosen]


def _record(c, v, col, row, stratum, why, density, area_rank=None):
    value = float(v[row])
    record = {"cell_id": int(c.ids[row]), "x": float(c.xs[row]), "y": float(c.ys[row]),
              "value": value, "percentile": col.percentile_of(value),
              "stratum": stratum, "why": why}
    if density is not None:
        record["density_pct"] = float(density[row])
    if area_rank is not None:
        record["area_pct"] = float(area_rank[row])
    return record


def _density_percentile(ds, c):
    density = cellmod.neighbour_density(ds).astype(np.float64)
    valid = np.flatnonzero(c.valid)
    out = np.full(c.n, np.nan)
    if valid.size:
        order = np.argsort(density[valid], kind="stable")
        ranks = np.empty(valid.size)
        ranks[order] = np.arange(valid.size)
        out[valid] = ranks / max(1, valid.size - 1) * 100
    return out


#: Cells drawn per stratum unless a caller asks for more (or fewer).
N_PER_STRATUM = 6


def stratified_cells(ds, marker, low, high=None, *, n_per_stratum=N_PER_STRATUM,
                     borderline_factor=2,
                     spatial_k=8, include_inconsistent=6, seed=0, max_ids=MAX_IDS,
                     strata=None) -> dict:
    """{strata: {name: [cells]}, inconsistent: [cells], edges, band_half, counts}."""
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    fit = profmod.fit_for(ds, marker)
    edges, h = stratum_edges(fit, col, low)
    vf = col.to_fit(v)
    eligible = c.valid & np.isfinite(vf)
    rows_all = np.flatnonzero(eligible)
    stratum_of = np.searchsorted(edges[1:-1], vf[rows_all], side="right")
    rng = np.random.default_rng(seed)
    density_pct = _density_percentile(ds, c)
    area_pct = None
    if c.area is not None:
        area_ok = np.flatnonzero(np.isfinite(c.area) & c.valid)
        area_pct = np.full(c.n, np.nan)
        if area_ok.size:
            order = np.argsort(c.area[area_ok], kind="stable")
            ranks = np.empty(area_ok.size)
            ranks[order] = np.arange(area_ok.size)
            area_pct[area_ok] = ranks / max(1, area_ok.size - 1) * 100
    wanted = strata or STRATA
    out = {"marker": marker, "gate": {"low": float(low), "high": high},
           "edges_fit": [float(e) for e in edges[1:-1]], "band_half_fit": float(h),
           "fit_space": "log1p" if col.to_log else "values", "strata": {},
           "counts": {}, "inconsistent": [], "seed": int(seed)}
    budget = int(max_ids)
    for index, name in enumerate(STRATA):
        members = rows_all[stratum_of == index]
        out["counts"][name] = int(members.shape[0])
        if name not in wanted or not members.shape[0]:
            if name in wanted:
                out["strata"][name] = []
            continue
        if members.shape[0] > STRATUM_CAP:
            members = np.sort(rng.choice(members, size=STRATUM_CAP, replace=False))
        n = n_per_stratum * (borderline_factor if name == "borderline" else 1)
        n = min(n, budget)
        tiles = spatial_tiles(c, members, spatial_k)
        tercile = np.clip((np.nan_to_num(density_pct[members], nan=50.0) // 33.34)
                          .astype(np.int64), 0, 2)
        near_gate = name in ("just_below", "borderline", "just_above")
        groups = tercile * spatial_k * spatial_k + tiles if near_gate else tiles
        median = np.median(vf[members])
        picked = round_robin(members, groups, n, rng, prefer=np.abs(vf[members] - median))
        records = [_record(c, v, col, int(r), name, "stratum", density_pct, area_pct)
                   for r in picked]
        if name == "borderline" and area_pct is not None and members.shape[0] > 2:
            area = c.area[members]
            ok = np.isfinite(area)
            if ok.sum() >= 2:
                smallest = int(members[ok][np.argmin(area[ok])])
                largest = int(members[ok][np.argmax(area[ok])])
                have = {rec["cell_id"] for rec in records}
                for row, why in ((smallest, "smallest"), (largest, "largest")):
                    if int(c.ids[row]) not in have and len(records) < n + 2:
                        records.append(_record(c, v, col, row, name, why, density_pct,
                                               area_pct))
        out["strata"][name] = records
        budget -= len(records)

    if include_inconsistent and budget > 0 and rows_all.shape[0] > 10:
        out["inconsistent"] = inconsistent_cells(
            ds, c, v, col, low, high, rows_all, n=min(include_inconsistent, budget),
            seed=seed, density=density_pct)
    out["n_cells"] = sum(len(r) for r in out["strata"].values()) + len(out["inconsistent"])
    return out


def inconsistent_cells(ds, c, v, col, low, high, rows_all, *, n, seed, density=None,
                       sample=20_000, min_neighbours=3):
    """Positives whose every neighbour is negative, and the converse -- the
    nearest the gate first, because far from it an isolated positive is more
    often biology (a lone T cell) than an error."""
    from plexora.plugins.gating.server.autogate import kernels

    positive = gate_rule.passes(v, low, high)
    grid = cellmod.grid(ds, c.radius_px())
    query = cellmod.seeded_subset(rows_all, sample, seed + 7)
    n_nb, n_pos = kernels.neighbour_counts(grid, query=query, flags=positive & c.valid)
    enough = n_nb >= min_neighbours
    lonely_pos = query[enough & positive[query] & (n_pos == 0)]
    lonely_neg = query[enough & ~positive[query] & (n_pos == n_nb)]
    g = float(col.to_fit(low))
    vf = col.to_fit(v)
    out = []
    half = max(1, n // 2)
    for rows, why, name in ((lonely_pos, "positive among negatives", "inconsistent_positive"),
                            (lonely_neg, "negative among positives", "inconsistent_negative")):
        if not rows.shape[0]:
            continue
        order = rows[np.argsort(np.abs(vf[rows] - g), kind="stable")][:half]
        out.extend(_record(c, v, col, int(r), name, why, density) for r in order)
    return out[:n]


def delta_cells(ds, marker, candidates, high=None, *, n_per_interval=8, n_regression=8,
                spatial_k=8, seed=0) -> dict:
    """The cells whose call changes between neighbouring candidate thresholds.

    Interval i holds the cells with `t_i < v <= t_{i+1}` (float32) -- exactly
    the cells a move from t_i to t_{i+1} flips from positive to negative.
    """
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    thresholds = sorted(float(t) for t in candidates)
    rng = np.random.default_rng(seed)
    v32 = v.astype(np.float32)
    usable = c.valid & np.isfinite(v32)
    intervals = []
    for lo_t, hi_t in zip(thresholds[:-1], thresholds[1:]):
        inside = usable & (v32 > np.float32(lo_t)) & (v32 <= np.float32(hi_t))
        rows = np.flatnonzero(inside)
        picked = _spread_by_value(c, v, rows, n_per_interval, spatial_k, rng)
        intervals.append({"from": lo_t, "to": hi_t,
                          "n_flip": col.n_positive(lo_t, high) - col.n_positive(hi_t, high),
                          "cells": [_record(c, v, col, int(r), "flip", "flips", None)
                                    for r in picked]})
    fit = profmod.fit_for(ds, marker)
    h = _band_half(fit, col, thresholds[0] if thresholds else 0.0)
    regression = []
    if thresholds:
        lo_edge = float(col.from_fit(col.to_fit(thresholds[0]) - h))
        hi_edge = float(col.from_fit(col.to_fit(thresholds[-1]) + h))
        below = np.flatnonzero(usable & (v32 <= np.float32(lo_edge)))
        above = np.flatnonzero(usable & (v32 > np.float32(hi_edge)))
        half = max(1, n_regression // 2)
        for rows, name, nearest_high in ((below, "stays_negative", True),
                                         (above, "stays_positive", False)):
            if not rows.shape[0]:
                continue
            order = np.argsort(v[rows], kind="stable")
            ranked = rows[order[::-1]] if nearest_high else rows[order]
            near = ranked[:max(half * 20, 200)]
            picked = round_robin(near, spatial_tiles(c, near, spatial_k), half, rng)
            regression.extend(_record(c, v, col, int(r), name, "no candidate changes it",
                                      None) for r in picked)
    return {"marker": marker, "candidates": thresholds, "high": high,
            "intervals": intervals, "regression": regression, "seed": int(seed)}


def _spread_by_value(c, v, rows, n, spatial_k, rng):
    """`n` of `rows` covering their value range evenly (equal-count chunks),
    each chunk's pick from the least-used spatial tile so far."""
    if rows.shape[0] <= n:
        return rows[np.argsort(v[rows], kind="stable")]
    ordered = rows[np.argsort(v[rows], kind="stable")]
    tiles = spatial_tiles(c, ordered, spatial_k)
    used = np.zeros(spatial_k * spatial_k, dtype=np.int64)
    chunks = np.array_split(np.arange(ordered.shape[0]), n)
    picked = []
    for chunk in chunks:
        if not chunk.size:
            continue
        load = used[tiles[chunk]] + rng.random(chunk.size) * 0.5
        best = int(chunk[np.argmin(load)])
        picked.append(ordered[best])
        used[tiles[best]] += 1
    return np.asarray(picked, dtype=np.int64)


def quadrant_cells(ds, a, gate_a, b, gate_b, *, n_per_quadrant=4, spatial_k=8, seed=0):
    """A few cells from each of the four quadrants of two gates."""
    c = cellmod.cells(ds)
    va, vb = cellmod.values(ds, a), cellmod.values(ds, b)
    col_a = profmod.column(ds, a)
    pa = gate_rule.passes(va, gate_a, float(col_a.sorted32[-1]))
    col_b = profmod.column(ds, b)
    pb = gate_rule.passes(vb, gate_b, float(col_b.sorted32[-1]))
    usable = c.valid & np.isfinite(va) & np.isfinite(vb)
    rng = np.random.default_rng(seed)
    out = {}
    for name, mask in (("both", pa & pb), (f"{a}_only", pa & ~pb), (f"{b}_only", ~pa & pb),
                       ("neither", ~pa & ~pb)):
        rows = np.flatnonzero(usable & mask)
        picked = round_robin(rows, spatial_tiles(c, rows, spatial_k), n_per_quadrant, rng)
        out[name] = [{"cell_id": int(c.ids[r]), "x": float(c.xs[r]), "y": float(c.ys[r]),
                      a: float(va[r]), b: float(vb[r])} for r in picked]
    return out
