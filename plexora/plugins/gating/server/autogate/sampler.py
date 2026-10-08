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
from plexora.plugins.gating.server.autogate import schemas
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


def _record(c, v, col, row, stratum, why, density, area_rank=None, spatial=None):
    value = float(v[row])
    record = {"cell_id": int(c.ids[row]), "x": float(c.xs[row]), "y": float(c.ys[row]),
              "value": value, "percentile": col.percentile_of(value),
              "stratum": stratum, "why": why}
    if density is not None:
        record["density_pct"] = float(density[row])
    if area_rank is not None:
        record["area_pct"] = float(area_rank[row])
    if spatial:
        # `spatial.cell_record`: only with the session's spatial switch on.
        record.update(spatial)
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
                     strata=None, within=None, spatial=None, partners=None) -> dict:
    """{strata: {name: [cells]}, inconsistent: [cells], edges, band_half, counts}.

    `within` (`{marker, gate}`) samples only among that partner's positives:
    the cells a conditional gate ("positive only within CD45+") calls.

    `spatial` (`{mode, map, pixel_um, select}`, the session's switch on):
    every record carries its environment (`spatial.cell_record`); with
    `select` (the default) the near-gate strata are spread over the
    environments (`allocate`) instead of the density terciles, and
    `priority` adds the cells that test whether errors concentrate in one
    environment (`PRIORITY_GROUPS`; `partners` = `[{partner, gate,
    relation}]` for `partner_conflict`). None: today's sample, unchanged."""
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    fit = profmod.fit_for(ds, marker)
    edges, h = stratum_edges(fit, col, low)
    vf = col.to_fit(v)
    eligible = c.valid & np.isfinite(vf)
    if within:
        w = cellmod.values(ds, within["marker"])
        top = float(profmod.column(ds, within["marker"]).sorted32[-1])
        eligible &= gate_rule.passes(w, float(within["gate"]), top)
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
    feats, gate = _spatial_features(ds, marker, low, high, spatial)
    select = feats is not None and spatial.get("select", True)
    limits = {}

    def env(row):
        return spatial_record(feats, gate, row) if feats is not None else None

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
        near_gate = name in ("just_below", "borderline", "just_above")
        median = np.median(vf[members])
        if select and near_gate:
            picked = _pick_by_env(c, feats, gate, members, n, spatial_k, rng,
                                  np.abs(vf[members] - median), limits, name)
        else:
            tiles = spatial_tiles(c, members, spatial_k)
            tercile = np.clip((np.nan_to_num(density_pct[members], nan=50.0) // 33.34)
                              .astype(np.int64), 0, 2)
            groups = tercile * spatial_k * spatial_k + tiles if near_gate else tiles
            picked = round_robin(members, groups, n, rng, prefer=np.abs(vf[members] - median))
        records = [_record(c, v, col, int(r), name, "stratum", density_pct, area_pct,
                           spatial=env(int(r)))
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
                                               area_pct, spatial=env(row)))
        out["strata"][name] = records
        budget -= len(records)

    if include_inconsistent and budget > 0 and rows_all.shape[0] > 10:
        out["inconsistent"] = inconsistent_cells(
            ds, c, v, col, low, high, rows_all, n=min(include_inconsistent, budget),
            seed=seed, density=density_pct)
    out["n_cells"] = sum(len(r) for r in out["strata"].values()) + len(out["inconsistent"])
    if feats is not None:
        out["spatial"] = {"mode": spatial.get("mode"), "select": bool(select),
                          "snr_available": bool(gate.snr.get("available"))}
    if select:
        out["priority"], out["priority_counts"] = _priority_cells(
            ds, c, v, col, marker, feats, gate, low, high, rng, spatial_k, partners,
            density_pct, budget)
        out["limits"] = limits
    return out


def spatial_record(feats, gate, row):
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    return spatialmod.cell_record(feats, gate, row)


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
                spatial_k=8, seed=0, within=None, spatial=None) -> dict:
    """The cells whose call changes between neighbouring candidate thresholds.

    Interval i holds the cells with `t_i < v <= t_{i+1}` (float32) -- exactly
    the cells a move from t_i to t_{i+1} flips from positive to negative.
    `within` (`{marker, gate}`) keeps only that partner's positives, as a
    conditional gate counts them; `n_positive` is then counted among them too.
    `spatial` (the session's switch on) splits each interval's flips by where
    they sit (`n_flip_dense`, `n_flip_sparse`, `n_flip_low_snr`) and gives
    each cell its environment; the cells chosen are the same.
    """
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    thresholds = sorted(float(t) for t in candidates)
    rng = np.random.default_rng(seed)
    v32 = v.astype(np.float32)
    usable = c.valid & np.isfinite(v32)
    if within:
        w = cellmod.values(ds, within["marker"])
        top = float(profmod.column(ds, within["marker"]).sorted32[-1])
        usable &= gate_rule.passes(w, float(within["gate"]), top)
        members = v32[usable]

        def n_positive(low):
            return int(gate_rule.passes(members, float(low), high).sum())
    else:
        def n_positive(low):
            return col.n_positive(low, high)
    feats, gate = _spatial_features(ds, marker, thresholds[0] if thresholds else 0.0, high,
                                    spatial)
    intervals = []
    for lo_t, hi_t in zip(thresholds[:-1], thresholds[1:]):
        inside = usable & (v32 > np.float32(lo_t)) & (v32 <= np.float32(hi_t))
        rows = np.flatnonzero(inside)
        picked = _spread_by_value(c, v, rows, n_per_interval, spatial_k, rng)
        interval = {"from": lo_t, "to": hi_t,
                    "n_flip": n_positive(lo_t) - n_positive(hi_t),
                    "cells": [_record(c, v, col, int(r), "flip", "flips", None,
                                      spatial=spatial_record(feats, gate, int(r))
                                      if feats is not None else None)
                              for r in picked]}
        if feats is not None:
            from plexora.plugins.gating.server.autogate import spatial as spatialmod

            interval.update(n_flip_dense=int(feats.dense_env[rows].sum()),
                            n_flip_sparse=int((~feats.dense_env[rows]).sum()),
                            n_flip_low_snr=int((gate.snr_class[rows]
                                                == spatialmod.SNR_LOW).sum()))
        intervals.append(interval)
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
            "intervals": intervals, "regression": regression, "seed": int(seed),
            "n_positive_at": [n_positive(t) for t in thresholds],
            "within": within["marker"] if within else None}


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


# -- spatial environments (`SessionOptions.spatial` stratify and decide) ----------------

#: The environments a near-gate sample is spread over, in a fixed order
#: (`spatial.env_of`): density, and SNR when the image says it.
ENV_ORDER = ("sparse/high", "dense/high", "sparse/low", "dense/low", "sparse/unknown",
             "dense/unknown", "sparse", "dense")

#: The priority groups a spatial sample adds (`priority`): cells that test
#: whether the gate's errors are concentrated in one environment.
PRIORITY_GROUPS = ("weak_pos_bright", "weak_pos_clear", "neg_beside_strong",
                   "strong_reference", "partner_conflict", "high_background")

#: The rows a like-for-like look shows (`contrast_cells`), in order.
CONTRAST_ROWS = ("bright", "clear", "dense_clear", "low_snr", "reference", "background")


def allocate(n, counts, order=ENV_ORDER) -> dict:
    """{env: cells} for `n` cells over environments holding `counts` cells:
    one for each present environment in `order`, then round by round in
    descending count (ties by `order`), never more than an environment has.
    Pure, deterministic."""
    order = [e for e in order if counts.get(e, 0) > 0] + \
        sorted(e for e in counts if counts.get(e, 0) > 0 and e not in order)
    take = {e: 0 for e in order}
    left = int(n)
    for env in order:
        if left <= 0:
            break
        take[env] = 1
        left -= 1
    ranked = sorted(order, key=lambda e: (-counts[e], order.index(e)))
    while left > 0:
        moved = False
        for env in ranked:
            if left <= 0:
                break
            if take[env] < counts[env]:
                take[env] += 1
                left -= 1
                moved = True
        if not moved:
            break
    return {e: k for e, k in take.items() if k}


def _spatial_features(ds, marker, low, high, spatial):
    """(feats, gate) for a sample's `spatial` payload (`{mode, map,
    pixel_um}`), or (None, None)."""
    if not spatial:
        return None, None
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    bgmap = spatialmod.decode_map(spatial.get("map"))
    feats = spatialmod.per_cell(ds, marker, bgmap=bgmap, pixel_um=spatial.get("pixel_um"))
    gate = spatialmod.at_gate(ds, marker, feats, low, high, mode=spatial.get("mode") or "C",
                              bgmap=bgmap)
    return feats, gate


def _distinct(c, candidates, k, min_distance, taken):
    """Up to `k` of `candidates` (in their order) no two within
    `min_distance` of each other or of `taken` (rows already picked): two
    cells side by side say one thing twice."""
    out = []
    keep = list(taken)
    for row in candidates:
        if len(out) >= k:
            break
        x, y = c.xs[row], c.ys[row]
        if any((c.xs[o] - x) ** 2 + (c.ys[o] - y) ** 2 < min_distance ** 2 for o in keep):
            continue
        out.append(int(row))
        keep.append(int(row))
    return out


def _pick_by_env(c, feats, gate, members, n, spatial_k, rng, prefer, limits, name):
    """`n` of `members` spread over the environments (`allocate`), each
    environment's picks round-robin over the slide, near-duplicates
    skipped."""
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    envs = spatialmod.env_of(feats, gate, members)
    present = {str(e): int(k) for e, k in zip(*np.unique(envs, return_counts=True))}
    take = allocate(n, present)
    expected = ENV_ORDER[:4] if gate.snr.get("available") else ("sparse", "dense")
    missing = [e for e in expected if e not in present]
    if missing:
        limits.setdefault(name, missing)
    min_distance = schemas.SPATIAL_RULES["near_duplicate_diameters"] * feats.cell_diameter_px
    picked = []
    for env in list(ENV_ORDER) + sorted(set(take) - set(ENV_ORDER)):
        k = take.get(env, 0)
        if not k:
            continue
        inside = envs == env
        rows = members[inside]
        candidates = round_robin(rows, spatial_tiles(c, rows, spatial_k), 2 * k + 2, rng,
                                 prefer=prefer[inside])
        picked.extend(_distinct(c, candidates, k, min_distance, picked))
    return np.asarray(picked, dtype=np.int64)


def _band_masks(feats, gate):
    vf, g, h = feats.vf, gate.g, gate.h
    elig = feats.eligible
    with np.errstate(invalid="ignore"):
        above = elig & (vf > g) & (vf <= g + 2 * h)
        below = elig & (vf > g - 2 * h) & (vf <= g)
    return above, below


def _partner_conflict(ds, marker, feats, gate, low, high, partners):
    """Cells near the gate the first partner with a control population calls
    wrong: positive and partner-negative (subset, co-expressed) or positive
    and partner-positive (exclusive)."""
    from plexora.plugins.gating.server.autogate import bivariate

    for partner in partners or ():
        relation = partner.get("relation")
        if relation not in bivariate.CONTROL_POPULATION:
            continue
        w = cellmod.values(ds, partner["partner"])
        top = float(profmod.column(ds, partner["partner"]).sorted32[-1])
        pp = gate_rule.passes(w, float(partner["gate"]), top)
        wrong = pp if relation == "exclusive" else ~pp & np.isfinite(w)
        return gate.near & gate.positive & wrong
    return None


def priority_masks(ds, marker, feats, gate, low, high, partners=None) -> dict:
    """{group: row mask} for `PRIORITY_GROUPS` (a group the evidence cannot
    form is absent: `high_background` without SNR, `partner_conflict`
    without a partner)."""
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    above, below = _band_masks(feats, gate)
    spill = gate.spill != spatialmod.SPILL["none"]
    low_snr = gate.snr_class == spatialmod.SNR_LOW
    clean = ~gate.bright_neighbour & ~gate.spill_suspect
    out = {"weak_pos_bright": above & spill,
           "weak_pos_clear": above & clean & ~low_snr,
           "neg_beside_strong": below & spill,
           "strong_reference": feats.eligible & (feats.vf >= feats.strong_fit) & clean
           & ~low_snr & ~gate.unreliable}
    conflict = _partner_conflict(ds, marker, feats, gate, low, high, partners)
    if conflict is not None:
        out["partner_conflict"] = conflict
    if gate.snr.get("available"):
        out["high_background"] = gate.near & low_snr
    return out


def _priority_cells(ds, c, v, col, marker, feats, gate, low, high, rng, spatial_k,
                    partners, density_pct, budget):
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    n = int(schemas.SPATIAL_RULES["priority_per_group"])
    out, counts = {}, {}
    for name, mask in priority_masks(ds, marker, feats, gate, low, high, partners).items():
        rows = np.flatnonzero(mask)
        counts[name] = int(rows.size)
        if not rows.size or budget <= 0:
            out[name] = []
            continue
        target = feats.strong_fit if name == "strong_reference" else gate.g
        picked = round_robin(rows, spatial_tiles(c, rows, spatial_k), min(n, budget), rng,
                             prefer=np.abs(feats.vf[rows] - target))
        out[name] = [_record(c, v, col, int(r), name, "priority", density_pct,
                             spatial=spatialmod.cell_record(feats, gate, int(r)))
                     for r in picked]
        budget -= len(out[name])
    return out, counts


def contrast_cells(ds, marker, low, high=None, *, side="above", n_per_row=6, seed=0,
                   within=None, spatial=None, partners=None, spatial_k=8) -> dict:
    """The like-for-like rows (`CONTRAST_ROWS`): cells of the same intensity
    -- the band on `side` of the gate -- in different surroundings: beside a
    bright neighbour (`bright`), with none, sparse (`clear`) and dense
    (`dense_clear`), in low SNR (`low_snr`, needs the image), the strong
    positives (`reference`) and clear negatives in low SNR (`background`). A
    row under `min_env_row_cells` cells is left out and listed in `limits`."""
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    feats, gate = _spatial_features(ds, marker, low, high, spatial or {"mode": "C"})
    rng = np.random.default_rng(seed)
    eligible = feats.eligible.copy()
    if within:
        w = cellmod.values(ds, within["marker"])
        top = float(profmod.column(ds, within["marker"]).sorted32[-1])
        eligible &= gate_rule.passes(w, float(within["gate"]), top)
    above, below = _band_masks(feats, gate)
    band = (above if side == "above" else below) & eligible
    spill = gate.spill != spatialmod.SPILL["none"]
    low_snr = gate.snr_class == spatialmod.SNR_LOW
    clean = ~gate.bright_neighbour & ~gate.spill_suspect & ~low_snr
    masks = {"bright": band & spill,
             "clear": band & clean & ~feats.dense_env,
             "dense_clear": band & clean & feats.dense_env,
             "low_snr": band & low_snr if gate.snr.get("available") else None,
             # Strong positives in clean surroundings: a reference read where the
             # background or a neighbour may have made it bright is no reference.
             "reference": eligible & (feats.vf >= feats.strong_fit) & clean & ~gate.unreliable,
             "background": (eligible & (feats.vf <= gate.g - 2 * gate.h) & low_snr)
             if gate.snr.get("available") else None}
    density_pct = _density_percentile(ds, c)
    rows, counts, limits = {}, {}, []
    minimum = int(schemas.SPATIAL_RULES["min_env_row_cells"])
    for name in CONTRAST_ROWS:
        mask = masks.get(name)
        if mask is None:
            limits.append({"row": name, "why": "needs the image's background"})
            continue
        members = np.flatnonzero(mask)
        counts[name] = int(members.size)
        if members.size < minimum:
            limits.append({"row": name, "why": f"{members.size} cells"})
            continue
        target = feats.strong_fit if name == "reference" else gate.g
        picked = round_robin(members, spatial_tiles(c, members, spatial_k), n_per_row, rng,
                             prefer=np.abs(feats.vf[members] - target))
        rows[name] = [_record(c, v, col, int(r), name, "contrast", density_pct,
                              spatial=spatialmod.cell_record(feats, gate, int(r)))
                      for r in picked]
    return {"marker": marker, "gate": {"low": float(low), "high": high}, "side": side,
            "rows": rows, "counts": counts, "limits": limits, "seed": int(seed),
            "fit_space": "log1p" if col.to_log else "values",
            "snr_available": bool(gate.snr.get("available"))}
