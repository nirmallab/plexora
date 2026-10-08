"""The last check before a gate is accepted: the whole image, in numbers.

A judgement made on two dozen cells can be right about those cells and wrong
about the slide. So after a look has settled on a threshold, six checks run
over every cell -- cheaply, with no image -- and only when one fails is a
picture of the whole image asked for (limits in `THRESHOLDS`, all [cal]):

1. `prior_fraction`: the positive fraction sits inside the expected band (when
   one is known);
2. `delta_from_gmm`: the final gate is near the GMM gate, in background sds --
   never nearer than the smallest candidate step (a positive-population sd can
   be several background ones);
3. `flip_share`: few of the positive calls flip between the two -- never
   fewer than the smallest step the candidate generator offers would flip, or
   the check would fail every refinement on a marker with a dense boundary;
4. `spatial_tiles`: no region disagrees with the rest -- per-tile positive
   fractions, after regressing out each tile's mean intensity, have few
   outliers;
5. `edge_ratio`: the tissue edge is not enriched;
6. `partners`: the gate's positives are no less pure against each partner
   than the marker's brightest positives are (`bivariate.purity_excess`) --
   self-calibrated, so a partner that misses some real positives does not
   fail every gate;
7. `guard_band`: the gate is inside the guard band.

With spatial evidence two more (`spatial_checks`), both of where positives
are, never how many: `density_agreement` and `snr_agreement`.
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


def _tile_regression(ds, marker, low, high=None):
    """(tile per row (-1 off), occupied tiles, residual per tile, n tiles,
    positive, valid): each occupied tile's positive fraction minus what its
    mean intensity predicts (one least-squares line over the tiles); NaN
    residuals where a tile has too few cells or the tiles are too few."""
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
    residual = np.full(n_tiles, np.nan)
    if ok.sum() >= 6:
        p = pos[ok] / counts[ok]
        x = mean[ok] / counts[ok]
        design = np.stack([np.ones_like(x), x], axis=1)
        coef, *_ = np.linalg.lstsq(design, p, rcond=None)
        residual[ok] = p - design @ coef
    return tile, ok, residual, n_tiles, positive, valid


def tile_residuals(ds, marker, low, high=None):
    """(outlier tiles, occupied tiles, edge ratio) for a gate."""
    c = cellmod.cells(ds)
    _tile, ok, residuals, _n, positive, valid = _tile_regression(ds, marker, low, high)
    outliers = 0
    if ok.sum() >= 6:
        residual = residuals[ok]
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


def numeric_checks(ds, marker, final, gmm, prior=None, partners=(), spatial=None) -> dict:
    """The seven checks for `final` against the GMM gate `gmm` -- nine with
    `spatial` (`{mode, map, pixel_um}`, the session deciding with spatial
    evidence): `density_agreement` and `snr_agreement` (`spatial_checks`)."""
    from plexora.plugins.gating.server.autogate import bivariate, candidates

    t = THRESHOLDS
    col = profmod.column(ds, marker)
    fit = profmod.fit_for(ds, marker)
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
    # The smallest candidate step toward `final` (sds of the population moved
    # into, `candidates.STEPS`): a refinement the generator offered is never
    # failed for being one step, however that sd compares to the background's.
    stepped = None
    if fit is not None and final != gmm:
        pools = profmod.pools(fit)
        up = float(col.to_fit(final)) > float(col.to_fit(gmm))
        sd_into = pools["sd_pos"] if up else pools["sd_bg"]
        stepped = float(col.to_fit(gmm)) + (1 if up else -1) * candidates.STEPS[0] * sd_into
    if fit is not None:
        _mu, sd_bg, _w = profmod._background((fit["means"], fit["sds"], fit["weights"]))
        sd_bg = max(sd_bg, 1e-12)
        delta = abs(float(col.to_fit(final)) - float(col.to_fit(gmm))) / sd_bg
        limit, note = t["max_delta_bg_sd"], None
        if stepped is not None:
            small = abs(stepped - float(col.to_fit(gmm))) / sd_bg
            if small > limit:
                limit, note = float(small), "limit raised to the smallest candidate step"
        check("delta_from_gmm", delta <= limit + 1e-9, delta, limit, note)
    else:
        check("delta_from_gmm", None, None, None, "no fit")
    flips = abs(n_final - n_gmm)
    share = flips / max(1, max(n_final, n_gmm))
    limit, note = t["max_flip_share"], None
    if stepped is not None:
        n_step = col.n_positive(float(col.from_fit(stepped)))
        share_small = abs(n_step - n_gmm) / max(1, max(n_step, n_gmm))
        if share_small > limit:
            limit = float(share_small)
            note = "limit raised to the share the smallest candidate step flips"
    check("flip_share", share <= limit + 1e-12, share, limit, note)
    outliers, occupied, edge_ratio = tile_residuals(ds, marker, final)
    tile_share = outliers / occupied if occupied else 0.0
    check("spatial_tiles", tile_share <= t["max_outlier_tile_share"], tile_share,
          t["max_outlier_tile_share"], f"{outliers} of {occupied} tiles")
    check("edge_ratio", None if edge_ratio is None else edge_ratio <= t["max_edge_ratio"],
          edge_ratio, t["max_edge_ratio"])
    worse, judged = [], 0
    for partner in partners or ():
        purity = bivariate.purity_excess(
            ds, marker, final, partner["partner"], float(partner["gate"]),
            relation=partner.get("relation") or "independent")
        if purity is None:
            continue
        judged += 1
        if purity["excess"] > t["partner_tolerance"]:
            worse.append({"partner": partner["partner"],
                          "relation": partner.get("relation"),
                          **{k: round(v, 4) if isinstance(v, float) else v
                             for k, v in purity.items()}})
    check("partners", not worse if judged else None, worse or None, t["partner_tolerance"],
          "impure share among the gate's positives minus among their brightest tenth")
    inside = candidates.inside_guard(ds, marker, final)
    check("guard_band", inside, None, None)
    if spatial:
        checks.extend(spatial_checks(ds, marker, final, spatial))
    failed = [c["name"] for c in checks if c["ok"] is False]
    return {"marker": marker, "final": float(final), "gmm": float(gmm),
            "n_positive": n_final, "fraction": pf, "checks": checks,
            "ok": not failed, "failed": failed}


def _tercile_gap(tile_value, residual, ok):
    """Mean residual of the top tercile of tiles by `tile_value` minus the
    bottom tercile's, over tiles `ok` with a finite value; (gap, n per side)
    or (None, n) with fewer than `min_agreement_tiles` a side."""
    from plexora.plugins.gating.server.autogate.schemas import SPATIAL_RULES

    usable = ok & np.isfinite(tile_value) & np.isfinite(residual)
    values, res = tile_value[usable], residual[usable]
    n = int(values.size // 3)
    if n < SPATIAL_RULES["min_agreement_tiles"]:
        return None, n
    order = np.argsort(values, kind="stable")
    low, high = res[order[:n]], res[order[-n:]]
    return float(high.mean() - low.mean()), n


def spatial_checks(ds, marker, final, spatial) -> list:
    """Two checks of WHERE the gate's positives are, beside the
    intensity-adjusted tile residuals (`_tile_regression`):

    - `density_agreement`: dense tiles hold no more positives than their
      intensity explains, compared with sparse ones (top minus bottom
      density tercile of tiles, at most `density_residual_max`);
    - `snr_agreement`: likewise low-SNR tiles against high-SNR ones
      (`snr_residual_max`); not run without the image's background.

    Spatial checks: they may hold or reject a gate, never move it
    (`engine.SPATIAL_CHECKS`)."""
    from plexora.plugins.gating.server.autogate import sampler
    from plexora.plugins.gating.server.autogate.schemas import SPATIAL_RULES

    feats, gate = sampler._spatial_features(ds, marker, final, None, spatial)
    tile, ok, residual, n_tiles, _positive, _valid = _tile_regression(ds, marker, final)
    out = []
    rows = np.flatnonzero(feats.eligible & (tile >= 0))
    counts = np.bincount(tile[rows], minlength=n_tiles).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        density = np.bincount(tile[rows], weights=feats.density_count[rows].astype(np.float64),
                              minlength=n_tiles) / counts
    gap, n = _tercile_gap(density, residual, ok)
    out.append({"name": "density_agreement",
                "ok": None if gap is None else bool(gap <= SPATIAL_RULES["density_residual_max"]),
                "value": gap, "limit": SPATIAL_RULES["density_residual_max"],
                "note": f"dense minus sparse tiles' residual positive fraction ({n} tiles a "
                        "side)" if gap is not None else "too few tiles"})
    if not gate.snr.get("available"):
        out.append({"name": "snr_agreement", "ok": None, "value": None,
                    "limit": SPATIAL_RULES["snr_residual_max"],
                    "note": gate.snr.get("reason") or "no background map"})
        return out
    known = rows[np.isfinite(gate.env_snr[rows])]
    with np.errstate(invalid="ignore", divide="ignore"):
        snr = np.bincount(tile[known], weights=gate.env_snr[known], minlength=n_tiles) / \
            np.bincount(tile[known], minlength=n_tiles)
    # Low SNR on top: the gap is low-SNR tiles' residual minus high-SNR ones'.
    gap, n = _tercile_gap(-snr, residual, ok)
    out.append({"name": "snr_agreement",
                "ok": None if gap is None else bool(gap <= SPATIAL_RULES["snr_residual_max"]),
                "value": gap, "limit": SPATIAL_RULES["snr_residual_max"],
                "note": f"low- minus high-SNR tiles' residual positive fraction ({n} tiles a "
                        "side)" if gap is not None else "too few tiles"})
    return out
