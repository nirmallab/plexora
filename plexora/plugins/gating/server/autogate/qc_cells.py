"""Technical QC a marker can be given from the cell table alone.

Where the pixels are not needed, the table answers faster and over the whole
image: whether positives cluster by tile more than chance allows, whether the
background drifts across the slide (illumination), whether positivity tracks
cell size (segmentation) or the nuclear stain (bleed-through), whether the
tissue edge is enriched, how bright positives are against the background.

All vectorised: tile statistics are `bincount`s over one integer tile index,
Moran's I is two shifted-array products, the illumination surface is one
least-squares fit of six terms. Flags here are soft unless noted; the overview
QC (`plexora.agent.evidence.image_qc`) is what confirms an image-level failure.
"""

from __future__ import annotations

import numpy as np

from plexora.agent import gate_rule
from plexora.plugins.gating.server.autogate import cells as cellmod

#: [cal] cut-points.
THRESHOLDS = {
    "min_tile_cells": 10,
    "moran_clustered": 0.5,
    "gradient_r2": 0.4,
    "gradient_range_fit": 0.4,
    "size_rho": 0.4,
    "size_quintile_ratio": 3.0,
    "dna_rho": 0.5,
    "edge_ratio_high": 2.0,
    "edge_ratio_low": 0.5,
    "edge_min_cells": 200,
    "edge_tile_um": 50.0,
    "edge_tile_px": 100.0,
}


def tiles(c, valid, n_target=200, lo=8, hi=128):
    """(tile index per row or -1, nx, ny, centres (u, v) in [-1, 1])."""
    idx = np.flatnonzero(valid)
    out = np.full(c.n, -1, dtype=np.int64)
    if not idx.shape[0]:
        return out, 1, 1, np.zeros((1, 2))
    g = int(np.clip(round(np.sqrt(idx.shape[0] / n_target)), lo, hi))
    xs, ys = c.xs[idx], c.ys[idx]
    x0, y0 = xs.min(), ys.min()
    extent = max(xs.max() - x0, ys.max() - y0, 1e-9)
    side = extent / g
    nx = max(1, int(np.ceil((xs.max() - x0) / side + 1e-9)))
    ny = max(1, int(np.ceil((ys.max() - y0) / side + 1e-9)))
    tx = np.clip(((xs - x0) // side).astype(np.int64), 0, nx - 1)
    ty = np.clip(((ys - y0) // side).astype(np.int64), 0, ny - 1)
    out[idx] = ty * nx + tx
    cx = (np.arange(nx) + 0.5) / nx * 2 - 1
    cy = (np.arange(ny) + 0.5) / ny * 2 - 1
    u, v = np.meshgrid(cx, cy)
    return out, nx, ny, np.stack([u.ravel(), v.ravel()], axis=1)


def morans_i(grid2d):
    """Moran's I over a 2-D array with NaN for empty tiles, rook adjacency."""
    valid = np.isfinite(grid2d)
    if valid.sum() < 4:
        return None
    z = grid2d - grid2d[valid].mean()
    zz = np.where(valid, z, 0.0)
    h = valid[:, :-1] & valid[:, 1:]
    v = valid[:-1, :] & valid[1:, :]
    cross = (zz[:, :-1] * zz[:, 1:])[h].sum() + (zz[:-1, :] * zz[1:, :])[v].sum()
    pairs = h.sum() + v.sum()
    denominator = (zz[valid] ** 2).sum()
    if pairs == 0 or denominator <= 0:
        return None
    return float(valid.sum() / pairs * cross / denominator)


def surface_fit(centres, values, weights=None):
    """(r2, range) of a 2nd-order polynomial fitted to per-tile values."""
    ok = np.isfinite(values)
    if ok.sum() < 8:
        return None, None
    u, v = centres[ok, 0], centres[ok, 1]
    design = np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)
    y = values[ok]
    w = np.sqrt(weights[ok]) if weights is not None else np.ones_like(y)
    coef, *_ = np.linalg.lstsq(design * w[:, None], y * w, rcond=None)
    fitted = design @ coef
    total = ((y - np.average(y, weights=w * w)) ** 2 * w * w).sum()
    residual = ((y - fitted) ** 2 * w * w).sum()
    r2 = float(1 - residual / total) if total > 0 else 0.0
    return r2, float(fitted.max() - fitted.min())


def edge_rows(c, valid, tile_px):
    """Rows whose ~tile is on the tissue's edge (an empty 8-neighbour)."""
    idx = np.flatnonzero(valid)
    edge = np.zeros(c.n, dtype=bool)
    if idx.shape[0] < 50:
        return edge
    xs, ys = c.xs[idx], c.ys[idx]
    x0, y0 = xs.min(), ys.min()
    nx = int((xs.max() - x0) // tile_px) + 1
    ny = int((ys.max() - y0) // tile_px) + 1
    if nx * ny > 25_000_000:
        return edge
    tx = ((xs - x0) // tile_px).astype(np.int64)
    ty = ((ys - y0) // tile_px).astype(np.int64)
    occupied = np.zeros((ny + 2, nx + 2), dtype=bool)
    occupied[ty + 1, tx + 1] = True
    empty_near = np.zeros_like(occupied)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                empty_near |= ~np.roll(np.roll(occupied, dy, axis=0), dx, axis=1)
    is_edge = occupied & empty_near
    edge[idx] = is_edge[ty + 1, tx + 1]
    return edge


def cell_qc(ds, col, profile, *, seed=0) -> dict:
    """The cell-level QC block of a profile (JSON-safe)."""
    from plexora.agent.presets import nuclear_channel

    t = THRESHOLDS
    c = cellmod.cells(ds)
    v = cellmod.values(ds, col.marker)
    fit = profile["fit"]
    gate_raw = fit["gate_raw"]
    finite = np.isfinite(v)
    valid = c.valid & finite
    positive = gate_rule.passes(v, gate_raw, float(col.sorted32[-1]))
    vf = col.to_fit(v)
    # Cells in a floor spike (unmeasured, `model.floor_spike`) are negatives,
    # but not background: packed into one dropped tile they would read as an
    # illumination gradient, and as a size or DNA correlation.
    floor = (vf <= col.fit[col.floor_n - 1]) if col.floor_n else np.zeros(v.shape, dtype=bool)
    measured = finite & ~floor
    out = {"n_cells_positioned": int(valid.sum()), "flags": []}

    tile, nx, ny, centres = tiles(c, valid)
    n_tiles = nx * ny
    rows = np.flatnonzero(valid)
    counts = np.bincount(tile[rows], minlength=n_tiles).astype(np.float64)
    pos_counts = np.bincount(tile[rows], weights=positive[rows].astype(np.float64),
                             minlength=n_tiles)
    enough = counts >= t["min_tile_cells"]
    p_tile = np.where(enough, pos_counts / np.maximum(counts, 1), np.nan)
    out["tiles"] = {"nx": nx, "ny": ny, "occupied": int(enough.sum())}
    moran = morans_i(p_tile.reshape(ny, nx))
    p_all = float(positive[rows].mean()) if rows.shape[0] else 0.0
    dispersion = None
    if enough.sum() > 1 and 0 < p_all < 1:
        expected = counts[enough] * p_all
        chi = ((pos_counts[enough] - expected) ** 2 / (expected * (1 - p_all))).sum()
        dispersion = float(chi / (enough.sum() - 1))
    out["spatial"] = {"moran_i": moran, "overdispersion": dispersion}
    if moran is not None and moran > t["moran_clustered"]:
        out["flags"].append("spatially_clustered")

    background = rows[(vf[rows] <= fit["gate_fit"]) & ~floor[rows]]
    medians, bg_counts = cellmod.grouped_median(tile[background], vf[background], n_tiles)
    medians = np.where(bg_counts >= t["min_tile_cells"], medians, np.nan)
    r2, spread = surface_fit(centres, medians, weights=bg_counts.astype(np.float64))
    out["illumination"] = {"r2": r2, "range_fit": spread}
    if (r2 is not None and r2 > t["gradient_r2"] and spread is not None
            and spread > t["gradient_range_fit"]):
        out["flags"].append("illumination_gradient_cells")

    out["size"] = None
    if c.area is not None:
        rho = cellmod.spearman(c.area, np.where(measured, vf, np.nan), seed=seed)
        area_ok = np.isfinite(c.area) & finite
        pf_q = None
        if area_ok.sum() >= 100:
            edges = np.quantile(c.area[area_ok], [0.2, 0.4, 0.6, 0.8])
            quintile = np.searchsorted(edges, c.area[area_ok], side="right")
            pos_q = np.bincount(quintile, weights=positive[area_ok].astype(np.float64),
                                minlength=5)
            n_q = np.bincount(quintile, minlength=5)
            pf_q = [float(pos_q[i] / n_q[i]) if n_q[i] else None for i in range(5)]
        ratio = (pf_q[4] / pf_q[0] if pf_q and pf_q[0] and pf_q[4] is not None else None)
        out["size"] = {"column": c.area_column, "rho": rho, "pf_by_quintile": pf_q,
                       "q5_over_q1": ratio}
        if (rho is not None and abs(rho) > t["size_rho"]) or (
                ratio is not None and ratio > t["size_quintile_ratio"]):
            out["flags"].append("size_correlated")

    out["nuclear"] = None
    dna = nuclear_channel(ds.table.markers)
    if dna and dna != col.marker:
        rho = cellmod.spearman(cellmod.values(ds, dna), np.where(measured, vf, np.nan),
                               seed=seed)
        out["nuclear"] = {"channel": dna, "rho": rho}
        if rho is not None and rho > t["dna_rho"]:
            out["flags"].append("nuclear_bleed")

    tile_px = (t["edge_tile_um"] / c.pixel_um) if c.pixel_um else t["edge_tile_px"]
    edge = edge_rows(c, valid, tile_px)
    n_edge, n_inner = int((edge & valid).sum()), int((~edge & valid).sum())
    out["edge"] = {"n_edge_cells": n_edge, "ratio": None}
    if n_edge >= t["edge_min_cells"] and n_inner >= t["edge_min_cells"]:
        pf_edge = float(positive[edge & valid].mean())
        pf_inner = float(positive[~edge & valid].mean())
        ratio = pf_edge / pf_inner if pf_inner > 0 else None
        out["edge"].update(pf_edge=pf_edge, pf_inner=pf_inner, ratio=ratio)
        if ratio is not None and ratio > t["edge_ratio_high"]:
            out["flags"].append("edge_enriched")
        elif ratio is not None and ratio < t["edge_ratio_low"]:
            out["flags"].append("edge_depleted")
    return out
