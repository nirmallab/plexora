"""The last check before a gate is accepted: the whole image, in numbers.

A judgement made on two dozen cells can be right about those cells and wrong
about the slide. So after a look has settled on a threshold, six checks run
over every cell -- cheaply, with no image -- and only when one fails is a
picture of the whole image asked for:

1. the positive fraction sits inside the expected band (when one is known);
2. the final gate is within 1.5 background sds of the GMM gate [cal];
3. no more than 10 % of the positive calls flip between the two [cal];
4. no region disagrees with the rest -- per-tile positive fractions, after
   regressing out each tile's mean intensity, have few outliers, and the
   tissue edge is not enriched more than twofold;
5. every partner relation is no worse than it was at the GMM gate;
6. the gate is inside the guard band.
"""

from __future__ import annotations

import numpy as np

from plexora.agent import gate_rule
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import qc_cells

#: [cal]
THRESHOLDS = {"max_delta_bg_sd": 1.5, "max_flip_share": 0.10, "tile_outlier_mad": 3.0,
              "max_outlier_tile_share": 0.05, "max_edge_ratio": 2.0,
              "partner_tolerance": 0.10, "min_tile_cells": 20}


def tile_residuals(ds, marker, low, high=None):
    """(outlier tiles, occupied tiles, edge ratio) for a gate."""
    col = profmod.column(ds, marker)
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(col.sorted32[-1]) if high is None else float(high)
    finite = np.isfinite(v)
    valid = c.valid & finite
    positive = gate_rule.passes(v, low, high)
    tile, nx, ny, _centres = qc_cells.tiles(c, valid)
    rows = np.flatnonzero(valid)
    n_tiles = nx * ny
    counts = np.bincount(tile[rows], minlength=n_tiles).astype(np.float64)
    pos = np.bincount(tile[rows], weights=positive[rows].astype(np.float64), minlength=n_tiles)
    mean = np.bincount(tile[rows], weights=col.to_fit(v[rows]), minlength=n_tiles)
    ok = counts >= THRESHOLDS["min_tile_cells"]
    outliers = 0
    if ok.sum() >= 6:
        p = pos[ok] / counts[ok]
        x = mean[ok] / counts[ok]
        design = np.stack([np.ones_like(x), x], axis=1)
        coef, *_ = np.linalg.lstsq(design, p, rcond=None)
        residual = p - design @ coef
        mad = np.median(np.abs(residual - np.median(residual))) * 1.4826
        if mad > 0:
            outliers = int((np.abs(residual) > THRESHOLDS["tile_outlier_mad"] * mad).sum())
    tile_px = (qc_cells.THRESHOLDS["edge_tile_um"] / c.pixel_um if c.pixel_um
               else qc_cells.THRESHOLDS["edge_tile_px"])
    edge = qc_cells.edge_rows(c, valid, tile_px)
    ratio = None
    if (edge & valid).sum() >= 50 and (~edge & valid).sum() >= 50:
        inner = positive[~edge & valid].mean()
        ratio = float(positive[edge & valid].mean() / inner) if inner > 0 else None
    return outliers, int(ok.sum()), ratio


def numeric_checks(ds, marker, final, gmm, prior=None, partners=()) -> dict:
    """The six checks for `final` against the GMM gate `gmm`."""
    from plexora.plugins.gating.server.autogate import bivariate, candidates

    t = THRESHOLDS
    col = profmod.column(ds, marker)
    fit = model.fit_for(ds, marker)
    checks = []

    def check(name, ok, value, limit, note=None):
        checks.append({"name": name, "ok": bool(ok) if ok is not None else None,
                       "value": value, "limit": limit, **({"note": note} if note else {})})

    n_final = col.n_positive(final)
    n_gmm = col.n_positive(gmm)
    pf = n_final / col.n_finite if col.n_finite else None
    if prior and pf is not None:
        lo, hi = float(prior[0]), float(prior[1])
        check("prior_fraction", lo <= pf <= hi, pf, [lo, hi])
    else:
        check("prior_fraction", None, pf, None, "no expected fraction known")
    if fit is not None:
        _mu, sd_bg, _w = profmod._background((fit["means"], fit["sds"], fit["weights"]))
        delta = abs(float(col.to_fit(final)) - float(col.to_fit(gmm))) / max(sd_bg, 1e-12)
        check("delta_from_gmm", delta <= t["max_delta_bg_sd"], delta, t["max_delta_bg_sd"])
    else:
        check("delta_from_gmm", None, None, None, "no fit")
    flips = abs(n_final - n_gmm)
    share = flips / max(1, max(n_final, n_gmm))
    check("flip_share", share <= t["max_flip_share"], share, t["max_flip_share"])
    outliers, occupied, edge_ratio = tile_residuals(ds, marker, final)
    tile_share = outliers / occupied if occupied else 0.0
    check("spatial_tiles", tile_share <= t["max_outlier_tile_share"], tile_share,
          t["max_outlier_tile_share"], f"{outliers} of {occupied} tiles")
    check("edge_ratio", None if edge_ratio is None else edge_ratio <= t["max_edge_ratio"],
          edge_ratio, t["max_edge_ratio"])
    worse = []
    for partner in partners or ():
        at_final = bivariate.bivariate_numbers(
            ds, marker, final, partner["partner"], float(partner["gate"]),
            relation=partner.get("relation") or "independent", with_grid=False)
        at_gmm = bivariate.bivariate_numbers(
            ds, marker, gmm, partner["partner"], float(partner["gate"]),
            relation=partner.get("relation") or "independent", with_grid=False)
        if at_final["contradiction"] > at_gmm["contradiction"] + t["partner_tolerance"]:
            worse.append({"partner": partner["partner"],
                          "at_final": at_final["contradiction"],
                          "at_gmm": at_gmm["contradiction"]})
    check("partners", not worse if partners else None, worse or None, t["partner_tolerance"])
    inside = candidates.inside_guard(ds, marker, final)
    check("guard_band", inside, None, None)
    failed = [c["name"] for c in checks if c["ok"] is False]
    return {"marker": marker, "final": float(final), "gmm": float(gmm),
            "n_positive": n_final, "fraction": pf, "checks": checks,
            "ok": not failed, "failed": failed}
