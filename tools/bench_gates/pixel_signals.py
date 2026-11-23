"""Deterministic pixel evidence for a gate: is a cell its own light source?

A reader judges whether a cell carries its own stain or is lit by a bright
neighbour, a haze or an artefact. Part of that judgement is a measurement:
a cell whose own light is brighter than its brightest neighbour's is a local
source of the stain; a cell darker than a neighbour may be lit by it. This
module measures that, per cell and per marker, and turns it into evidence
the engine's own loss can read. Nothing here is a reader, a floor or a rule
of its own; the plan and its guard rails are in
docs/internal/bench/PIXEL_EVIDENCE.md.

Signal A -- neighbour-referenced contrast ("self-lit share")
    contrast = own - brightest neighbour, both in the fit's (log) space and
    measured alike: the table's per-cell means (`version="table"`), or the
    mean of a disk of the image's own cell radius at every centroid
    (`version="pixel"`). Neighbours are the cells whose centroid lies within
    `REACH` cell diameters: the cells that touch.

    The null is the image's own: the contrast of the cells below the
    background's mode (`scoring.background`), stratified by how many
    neighbours a cell has (a cell among eight neighbours meets a brighter one
    more often than a cell among two). A cell's p is the share of its
    stratum's null at or above its contrast.

    Per interval between candidate gates, the self-lit share is Storey's
    estimate with lambda one half: s = 1 - 2 * share(p > 1/2), the fraction
    of cells the null cannot account for. Not every positive cell can stand
    out (a positive among positives is never brighter than all of them), so
    the shares are divided by the detection efficiency `e`, the self-lit
    share of cells above every candidate -- positive under any of them --
    in the same strata. The ratio estimates each interval's positive share.

    The pixel gate is then the candidate the engine's own loss
    (`hypotheses.evidence.monotone` + `losses`, symmetric) would choose if
    these shares were a reader's: no onset rule and no level of its own.

Signal B -- spill-controlled exclusive background
    The marker's 99th percentile among cells clearly positive for a lineage
    that excludes it (above the partner's gate by the engine's tolerance),
    left out where the cell's neighbours are brighter in the marker than the
    image's median neighbour: the FACS negative control of
    `bivariate.negative_control`, without the spill that inflates it.

Every result carries a `validity` record. An invalid signal has no effect
anywhere and is never read as a failed stain.

Stage 1 (2026-11-23) found no role for signal A -- not a gate, a tie-break,
a check or a reader -- and too little evidence for B's check, so this lives
beside the offline tool and nothing in the engine imports it. The numbers
and the reason (the contrast marks spill-lit cells, not positives: a
positive among positives is darker than its brightest neighbour) are in
docs/internal/bench/PIXEL_EVIDENCE.md.
"""

from __future__ import annotations

import math

import numpy as np

VERSION = "1"

#: Neighbours: cells whose centroid lies within this many cell diameters
#: (centres of touching round cells sit one diameter apart). Structural, not
#: fitted: geometry is in units of the image's own cell size.
REACH = 1.5

#: At most this many neighbours are looked at per cell (the nearest).
MAX_NEIGHBOURS = 12

#: A neighbour-count stratum of the null needs this many cells, or it is
#: pooled with the next denser one (`bivariate.THRESHOLDS["control_min_cells"]`).
MIN_NULL = 50

#: The pixel version needs a cell diameter of at least this many pixels at
#: the level it reads: below it a disk is a handful of pixels.
MIN_DIAMETER_PX = 8.0

#: The engine's constants the evidence reuses rather than choosing its own
#: (hypotheses.PARAMS): material cells and the user's retest noise.
MIN_BETWEEN = 20
TOLERANCE = 0.2

#: The pixel version's disk radius, as a fraction of the cell diameter: the
#: cell's own body.
DISK_FRACTION = 0.5


# -- geometry --------------------------------------------------------------------------------


def cell_diameter(area) -> float | None:
    """The image's own cell diameter (position units): that of a disk with
    the median cell area. None without areas."""
    if area is None:
        return None
    area = np.asarray(area, dtype=np.float64)
    area = area[np.isfinite(area) & (area > 0)]
    if not area.size:
        return None
    return float(2.0 * math.sqrt(float(np.median(area)) / math.pi))


def diameter_from_spacing(xy) -> float | None:
    """A cell diameter from positions alone (no areas): the median distance
    to the nearest neighbour, which is about one diameter in tissue."""
    from scipy.spatial import cKDTree

    xy = np.asarray(xy, dtype=np.float64)
    if xy.shape[0] < 3:
        return None
    d, _ = cKDTree(xy).query(xy, k=2)
    d = d[:, 1]
    d = d[np.isfinite(d) & (d > 0)]
    return float(np.median(d)) if d.size else None


def neighbours(xy, diameter, reach=REACH, k=MAX_NEIGHBOURS):
    """(index [n, k], count [n]): each cell's neighbours within `reach`
    diameters, nearest first, -1 padded; itself left out."""
    from scipy.spatial import cKDTree

    xy = np.asarray(xy, dtype=np.float64)
    n = xy.shape[0]
    if n < 2:
        return np.full((n, k), -1, dtype=np.int64), np.zeros(n, dtype=np.int64)
    kk = min(k + 1, n)
    dist, idx = cKDTree(xy).query(xy, k=kk, distance_upper_bound=float(reach) * float(diameter))
    idx = np.where(np.isfinite(dist), idx, -1).astype(np.int64)
    # Drop the cell itself wherever it appears (ties put it anywhere).
    own = idx == np.arange(n)[:, None]
    idx[own] = -1
    order = np.argsort(idx < 0, axis=1, kind="stable")
    idx = np.take_along_axis(idx, order, axis=1)[:, :k]
    if idx.shape[1] < k:
        idx = np.pad(idx, ((0, 0), (0, k - idx.shape[1])), constant_values=-1)
    return idx, (idx >= 0).sum(axis=1)


def neighbour_max(values, idx):
    """The brightest neighbour's value per cell (NaN without neighbours)."""
    values = np.asarray(values, dtype=np.float64)
    safe = np.where(idx >= 0, idx, 0)
    taken = np.where(idx >= 0, values[safe], np.nan)
    with np.errstate(all="ignore"):
        out = np.nanmax(np.where(np.isnan(taken), -np.inf, taken), axis=1)
    out[~np.isfinite(out)] = np.nan
    return out


def neighbour_mean(values, idx):
    """The mean of the neighbours' values per cell (NaN without neighbours)."""
    values = np.asarray(values, dtype=np.float64)
    safe = np.where(idx >= 0, idx, 0)
    taken = np.where(idx >= 0, values[safe], np.nan)
    with np.errstate(all="ignore"):
        return np.nanmean(taken, axis=1)


def contrast(values, idx):
    """own - brightest neighbour (NaN for a cell without neighbours, which
    is not scored: on dark matrix every cell looks like its own source)."""
    values = np.asarray(values, dtype=np.float64)
    return values - neighbour_max(values, idx)


# -- the pixel version's measurement --------------------------------------------------------


def disk_offsets(radius_px: float):
    """(dy, dx) of the pixels within `radius_px` of a centre pixel."""
    r = max(float(radius_px), 0.5)
    m = int(math.ceil(r))
    yy, xx = np.mgrid[-m:m + 1, -m:m + 1]
    keep = yy * yy + xx * xx <= r * r
    return yy[keep].astype(np.int64), xx[keep].astype(np.int64)


def disk_means(plane, ys, xs, radius_px, chunk=50_000):
    """The mean of `plane` in a disk at each (ys, xs) (pixels of `plane`);
    NaN where the disk leaves the plane."""
    dy, dx = disk_offsets(radius_px)
    h, w = plane.shape
    ys = np.rint(np.asarray(ys, dtype=np.float64)).astype(np.int64)
    xs = np.rint(np.asarray(xs, dtype=np.float64)).astype(np.int64)
    out = np.full(ys.shape[0], np.nan)
    m = int(max(abs(dy).max(), abs(dx).max()))
    inside = (ys >= m) & (ys < h - m) & (xs >= m) & (xs < w - m)
    where = np.flatnonzero(inside)
    for start in range(0, where.size, chunk):
        sel = where[start:start + chunk]
        yy = ys[sel, None] + dy[None, :]
        xx = xs[sel, None] + dx[None, :]
        out[sel] = plane[yy, xx].astype(np.float64).mean(axis=1)
    return out


# -- the null and the self-lit shares -------------------------------------------------------


def null_strata(contrast_, counts, null_rows, min_null=MIN_NULL):
    """{stratum: sorted null contrasts} and the stratum of every count: the
    contrast of the null rows by neighbour count, a count with fewer than
    `min_null` null cells pooled with the next denser one (the densest pooled
    downward). Counts never seen in the null take the nearest stratum."""
    contrast_ = np.asarray(contrast_, dtype=np.float64)
    counts = np.asarray(counts, dtype=np.int64)
    ok = null_rows & np.isfinite(contrast_) & (counts > 0)
    present = sorted(int(c) for c in np.unique(counts[ok]))
    groups, current = [], []
    for c in present:
        current.append(c)
        if int(np.isin(counts[ok], current).sum()) >= min_null:
            groups.append(current)
            current = []
    if current:
        if groups:
            groups[-1] = groups[-1] + current
        else:
            groups.append(current)
    strata, of_count = {}, {}
    for g, members in enumerate(groups):
        rows = ok & np.isin(counts, members)
        if int(rows.sum()) < min_null:
            continue
        strata[g] = np.sort(contrast_[rows])
        for c in members:
            of_count[c] = g
    if not strata:
        return {}, {}
    known = sorted(of_count)
    top = int(counts.max()) if counts.size else 0
    for c in range(1, top + 1):
        if c not in of_count:
            nearest = min(known, key=lambda k: (abs(k - c), -k))
            of_count[c] = of_count[nearest]
    return strata, of_count


def pvalues(contrast_, counts, strata, of_count):
    """Each cell's right-tail p against its stratum's null (the share of
    null contrasts at or above its own, +1 corrected); NaN when unscored."""
    contrast_ = np.asarray(contrast_, dtype=np.float64)
    counts = np.asarray(counts, dtype=np.int64)
    out = np.full(contrast_.shape[0], np.nan)
    for c, g in of_count.items():
        rows = (counts == c) & np.isfinite(contrast_)
        if not rows.any():
            continue
        null = strata[g]
        above = null.size - np.searchsorted(null, contrast_[rows], side="left")
        out[rows] = (above + 1.0) / (null.size + 1.0)
    return out


def storey_share(p) -> tuple:
    """(self-lit share, its standard error, cells): 1 - 2 * share(p > 1/2),
    clipped to [0, 1]; the cells the null cannot account for."""
    p = np.asarray(p, dtype=np.float64)
    p = p[np.isfinite(p)]
    n = int(p.size)
    if not n:
        return None, None, 0
    f = float((p > 0.5).mean())
    share = min(1.0, max(0.0, 1.0 - 2.0 * f))
    se = 2.0 * math.sqrt(max(f * (1.0 - f), 0.25 / n) / n)
    return share, se, n


def interval_shares(values, p, bounds):
    """Per interval (bounds[k], bounds[k+1]] and above the top bound: the
    cells, the cells scored, and the self-lit share with its error."""
    values = np.asarray(values, dtype=np.float64)
    edges = list(bounds) + [np.inf]
    out = []
    for k in range(len(edges) - 1):
        rows = np.isfinite(values) & (values > edges[k]) & (values <= edges[k + 1])
        share, se, n = storey_share(p[rows])
        out.append({"from": float(edges[k]), "to": float(edges[k + 1]),
                    "cells": int(rows.sum()), "scored": n,
                    "share": share, "se": se})
    return out


# -- the pixel gate -------------------------------------------------------------------------


def level_losses(n_cells, fitted, level=0.5):
    """`evidence.losses` with the two errors weighted so the gate sits where
    the fitted share crosses `level` (one half: the engine's symmetric loss,
    scaled by one half)."""
    shares = [0.5 if f is None else float(f) for f in fitted]
    out = []
    for j in range(len(n_cells) + 1):
        below = sum(n * s for n, s in zip(n_cells[:j], shares[:j])) * (1.0 - level)
        above = sum(n * (1.0 - s) for n, s in zip(n_cells[j:], shares[j:])) * level
        out.append(below + above)
    return out


def _monotone(fractions, weights):
    from plexora.plugins.gating.server.autogate.hypotheses.evidence import monotone

    return monotone(fractions, weights)


def pixel_gate(values, p, bounds, *, levels=(0.5,), min_between=MIN_BETWEEN,
               normalise=True) -> dict:
    """The candidate (one of `bounds`, sorted) the engine's loss chooses from
    the self-lit shares, with its validity record.

    `values` are the fit-space values of every cell, `p` each cell's p
    (`pvalues`; NaN when unscored). The shares between the bounds are divided
    by the detection efficiency (`normalise`), made non-decreasing weighted
    by the cells scored, and each candidate's loss counts every cell of its
    interval (`evidence.losses`). One gate per `levels` entry; the first is
    the gate."""
    bounds = [float(b) for b in bounds]
    rows = interval_shares(values, p, bounds)
    inside, top = rows[:-1], rows[-1]
    validity = {"valid": True, "reasons": []}

    def invalid(reason):
        validity["valid"] = False
        validity["reasons"].append(reason)

    if len(bounds) < 2:
        invalid("fewer than two candidates")
    efficiency, eff_se = top["share"], top["se"]
    if normalise:
        if top["scored"] < min_between or efficiency is None:
            invalid("too few cells above every candidate to measure detection")
        elif efficiency - 2.0 * (eff_se or 0.0) <= 0.0:
            # Positives never stand out from their neighbours: packed
            # positives, or no own signal at all. A no-call, never "failed".
            invalid("positives do not stand out from their neighbours (flat)")
    scale = efficiency if (normalise and efficiency) else 1.0
    fractions = [min(1.0, (r["share"] or 0.0) / scale) if r["scored"] else 0.0 for r in inside]
    weights = [float(r["scored"]) for r in inside]
    fitted = _monotone(fractions, weights) if any(weights) else [None] * len(inside)
    n_cells = [r["cells"] for r in inside]
    n_above = top["cells"]
    gates = {}
    for level in levels:
        loss = level_losses(n_cells, fitted, level)
        j = int(np.argmin(loss))
        gates[str(level)] = {"index": j, "gate": bounds[j] if j < len(bounds) else None,
                             "loss": [round(float(v), 2) for v in loss]}
    first = gates[str(levels[0])]
    j = first["index"]
    if validity["valid"]:
        if j == 0:
            invalid("every interval stands out: the rise spans the window")
        elif j >= len(bounds) - 1:
            invalid("no interval stands out: no crossing inside the window")
    excess = sum(n * (f or 0.0) for n, f in zip(n_cells[j:], fitted[j:])) + \
        n_above * (1.0 if normalise else (efficiency or 0.0))
    if validity["valid"] and excess < min_between:
        invalid("fewer than min_between cells stand out above the gate")
    return {"version": VERSION, "gate": first["gate"] if validity["valid"] else None,
            "candidate": first["gate"], "gates": {k: v["gate"] for k, v in gates.items()},
            "efficiency": efficiency, "efficiency_se": eff_se,
            "intervals": [{**r, "fitted": None if f is None else round(float(f), 4)}
                          for r, f in zip(inside, fitted)],
            "above": top, "loss": first["loss"], "validity": validity}


def signal_a(values, contrast_, counts, mode, bounds, *, levels=(0.5,), normalise=True,
             min_between=MIN_BETWEEN) -> dict:
    """Signal A end to end: the null from the cells below `mode`, every
    cell's p, and the pixel gate over `bounds`."""
    values = np.asarray(values, dtype=np.float64)
    null_rows = np.isfinite(values) & (values < float(mode))
    strata, of_count = null_strata(contrast_, counts, null_rows)
    if not strata:
        return {"version": VERSION, "gate": None, "candidate": None,
                "validity": {"valid": False, "reasons": ["no null: too few cells below the mode"]}}
    p = pvalues(contrast_, counts, strata, of_count)
    out = pixel_gate(values, p, bounds, levels=levels, normalise=normalise,
                     min_between=min_between)
    out["null"] = {"strata": {str(g): int(v.size) for g, v in strata.items()},
                   "median": {str(g): round(float(np.median(v)), 4) for g, v in strata.items()}}
    out["p"] = p
    return out


def tile_agreement(values, p, xy, bounds, *, tiles=3, tolerance=TOLERANCE, **kw) -> dict:
    """The pixel gate per position-quantile tile (`tiles` x `tiles`, as
    `scoring._tiles`): agreement when every valid tile's gate lies within
    `tolerance` of the median of them."""
    xy = np.asarray(xy, dtype=np.float64)
    qx = np.quantile(xy[:, 0], np.linspace(0, 1, tiles + 1))
    qy = np.quantile(xy[:, 1], np.linspace(0, 1, tiles + 1))
    gates = []
    for i in range(tiles):
        for j in range(tiles):
            rows = (xy[:, 0] >= qx[i]) & (xy[:, 0] <= qx[i + 1]) & \
                (xy[:, 1] >= qy[j]) & (xy[:, 1] <= qy[j + 1])
            g = pixel_gate(np.where(rows, values, np.nan), np.where(rows, p, np.nan),
                           bounds, **kw)
            gates.append(g["gate"])
    valid = [g for g in gates if g is not None]
    if len(valid) < 2:
        return {"tiles": gates, "agree": None, "spread": None}
    med = float(np.median(valid))
    spread = float(max(abs(g - med) for g in valid))
    return {"tiles": gates, "agree": spread <= tolerance + 1e-9, "spread": round(spread, 4),
            "median": med}


# -- the comparator: the binary local-peak onset (offline only) -----------------------------


def peak_onset(values, contrast_, bounds, mode, *, share_of_rise=0.1, smooth=1):
    """The independent run's index: the share of local peaks (contrast > 0)
    per interval, its baseline below `mode` and its plateau above the top
    bound; the onset is the first interval whose (smoothed) share has
    covered `share_of_rise` of the way from baseline to plateau. Offline
    comparator only: `share_of_rise` is that run's choice."""
    values = np.asarray(values, dtype=np.float64)
    peak = np.asarray(contrast_, dtype=np.float64) > 0
    scored = np.isfinite(contrast_) & np.isfinite(values)
    base_rows = scored & (values < mode)
    top_rows = scored & (values > bounds[-1])
    if base_rows.sum() < MIN_NULL or top_rows.sum() < MIN_BETWEEN:
        return {"onset": None, "reason": "too few cells for baseline or plateau"}
    base, plateau = float(peak[base_rows].mean()), float(peak[top_rows].mean())
    shares = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        rows = scored & (values > a) & (values <= b)
        shares.append(float(peak[rows].mean()) if rows.any() else np.nan)
    s = np.asarray(shares)
    if smooth and s.size >= 3:
        s = np.convolve(np.nan_to_num(s, nan=base), np.ones(3) / 3, mode="same")
    target = base + share_of_rise * (plateau - base)
    if plateau <= base:
        return {"onset": None, "baseline": base, "plateau": plateau, "reason": "no rise"}
    hit = np.flatnonzero(s >= target)
    return {"onset": float(bounds[hit[0]]) if hit.size else None, "baseline": base,
            "plateau": plateau}


# -- signal B: the spill-controlled exclusive background ------------------------------------


def exclusive_background(values, nb_mean, partners, *, tolerance=TOLERANCE,
                         percentile=99.0, min_cells=MIN_NULL) -> dict:
    """The marker's `percentile` among cells clearly positive for an
    exclusive partner (`partners`: [(partner values, partner gate)], each at
    least `tolerance` above its gate), left out where the marker's
    neighbour mean is above the image's median neighbour mean.

    {value, n, partners, validity}; value None with fewer than `min_cells`."""
    values = np.asarray(values, dtype=np.float64)
    nb_mean = np.asarray(nb_mean, dtype=np.float64)
    member = np.zeros(values.shape[0], dtype=bool)
    used = []
    for pv, gate in partners:
        if gate is None:
            continue
        pv = np.asarray(pv, dtype=np.float64)
        clear = np.isfinite(pv) & (pv >= float(gate) + tolerance)
        member |= clear
        used.append(int(clear.sum()))
    finite_nb = np.isfinite(nb_mean)
    cap = float(np.median(nb_mean[finite_nb])) if finite_nb.any() else np.inf
    rows = member & np.isfinite(values) & finite_nb & (nb_mean <= cap)
    n = int(rows.sum())
    if n < min_cells:
        return {"value": None, "n": n, "partners": used,
                "validity": {"valid": False, "reasons": ["too few clear exclusive cells"]}}
    return {"value": float(np.percentile(values[rows], percentile)), "n": n, "partners": used,
            "neighbour_cap": cap, "validity": {"valid": True, "reasons": []}}


def grid(values, mode, step=0.1, top_quantile=0.995):
    """Candidates every `step` (the engine's `min_gap`) from the background's
    mode to the `top_quantile` of the values: a lattice with no estimator in
    it, for scoring the pixel gate on its own."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    hi = float(np.quantile(values, top_quantile))
    lo = float(mode)
    if not hi > lo + step:
        return []
    start = math.ceil(lo / step) * step
    return [round(v, 4) for v in np.arange(start, hi + 1e-9, step)]
