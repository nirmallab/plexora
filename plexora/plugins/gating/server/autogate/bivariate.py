"""Two gates against each other: the FACS-style check, as numbers first.

A gate is easiest to judge beside a marker whose relation to it is known --
CD8 T cells are CD3+, T cells are CD20-. The quadrant counts of the two gates
say at once whether that relation holds, and the contradiction score says how
badly it does not, before any pixel is drawn: a plot is rendered only when the
numbers are anomalous (see `plexora.agent.evidence.density_plot`).

Two subtleties are separated out, because each is a different fix:

- **Adjacent orphans.** A CD8+ cell that is CD3- but touches a CD3+ cell is
  more often spill-over from the neighbour than a wrong gate.
- **Conditional shift.** The median of A among B+ cells minus among B- cells:
  bleed-through from B into A looks like co-expression that follows B's
  intensity, not A's biology.
"""

from __future__ import annotations

import base64

import numpy as np

from plexora.agent import gate_rule
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod

RELATIONS = ("coexpressed", "subset", "exclusive", "independent")

#: [cal] how far a relation may be off before it is called a contradiction.
THRESHOLDS = {"coexpressed_expected": 0.7, "exclusive_tolerated": 0.15,
              "subset_tolerated": 0.3, "independent_phi": 0.5, "plot_at": 0.5,
              "adjacent_diameters": 1.2}


def _density_grid(fa, fb, bins):
    ok = np.isfinite(fa) & np.isfinite(fb)
    if ok.sum() < 2:
        return None
    lo_a, hi_a = np.percentile(fa[ok], [0.5, 99.9])
    lo_b, hi_b = np.percentile(fb[ok], [0.5, 99.9])
    if not hi_a > lo_a:
        hi_a = lo_a + 1
    if not hi_b > lo_b:
        hi_b = lo_b + 1
    counts, _edges_a, _edges_b = np.histogram2d(fa[ok], fb[ok], bins=bins,
                                                range=((lo_a, hi_a), (lo_b, hi_b)))
    scaled = np.log1p(counts)
    top = scaled.max() or 1.0
    grid = np.round(scaled / top * 255).astype(np.uint8)
    return {"bins": int(bins), "a_range": [float(lo_a), float(hi_a)],
            "b_range": [float(lo_b), float(hi_b)],
            "log_density_u8": base64.b64encode(grid.tobytes()).decode("ascii"),
            "layout": "rows index a, columns index b"}


def bivariate_numbers(ds, a, gate_a, b, gate_b, *, relation="independent", high_a=None,
                      high_b=None, bins=64, seed=0, with_grid=True) -> dict:
    """Quadrant counts, association, relation-specific contradiction score."""
    if relation not in RELATIONS:
        raise ValueError(f"relation is one of {RELATIONS}")
    t = THRESHOLDS
    col_a, col_b = profmod.column(ds, a), profmod.column(ds, b)
    va, vb = cellmod.values(ds, a), cellmod.values(ds, b)
    high_a = float(col_a.sorted32[-1]) if high_a is None else float(high_a)
    high_b = float(col_b.sorted32[-1]) if high_b is None else float(high_b)
    both_finite = np.isfinite(va) & np.isfinite(vb)
    pa = gate_rule.passes(va, gate_a, high_a) & both_finite
    pb = gate_rule.passes(vb, gate_b, high_b) & both_finite
    code = np.where(both_finite, 2 * pa.astype(np.int64) + pb.astype(np.int64), 4)
    counts = np.bincount(code, minlength=5)
    n00, n01, n10, n11 = (int(counts[i]) for i in range(4))
    n = n00 + n01 + n10 + n11
    n_a, n_b = n10 + n11, n01 + n11
    phi = None
    denominator = np.sqrt(float(n_a) * (n - n_a) * n_b * (n - n_b)) if n else 0.0
    if denominator > 0:
        phi = float((n11 * n00 - n10 * n01) / denominator)
    expected11 = n_a * n_b / n if n else 0.0
    chi2 = None
    if n and 0 < n_a < n and 0 < n_b < n:
        observed = np.array([n00, n01, n10, n11], dtype=np.float64)
        expected = np.array([(n - n_a) * (n - n_b), (n - n_a) * n_b, n_a * (n - n_b),
                             n_a * n_b], dtype=np.float64) / n
        chi2 = float(((observed - expected) ** 2 / expected).sum())
    frac_a_in_b = n11 / n_a if n_a else None      # of A+, how many are B+
    frac_b_in_a = n11 / n_b if n_b else None
    union = n_a + n_b - n11
    jaccard = n11 / union if union else None

    fa, fb = col_a.to_fit(va), col_b.to_fit(vb)
    shift = None
    if pb.any() and (~pb & both_finite).any():
        shift = float(np.nanmedian(fa[pb]) - np.nanmedian(fa[~pb & both_finite]))

    score = 0.0
    orphan = None
    adjacent_share = None
    double_fraction = None
    if relation in ("coexpressed", "subset"):
        # A's positives are expected inside B's.
        orphan = (n10 / n_a) if n_a else None
        if relation == "coexpressed":
            inside = frac_a_in_b if frac_a_in_b is not None else 1.0
            score = max(np.clip((t["coexpressed_expected"] - inside)
                                / t["coexpressed_expected"], 0, 1),
                        np.clip(-(phi or 0.0), 0, 1))
        else:
            inside = frac_a_in_b if frac_a_in_b is not None else 1.0
            score = float(np.clip((1 - inside) / t["subset_tolerated"], 0, 1))
        if n10:
            adjacent_share = _adjacent_share(ds, pa & ~pb, pb, seed=seed)
    elif relation == "exclusive":
        double_fraction = n11 / min(n_a, n_b) if min(n_a, n_b) else None
        score = float(np.clip((double_fraction or 0.0) / t["exclusive_tolerated"], 0, 1))
        if n11:
            adjacent_share = _adjacent_share(ds, pa & pb, pb & ~pa, seed=seed)
    else:
        score = float(np.clip(abs(phi or 0.0) / t["independent_phi"], 0, 1))
    out = {
        "a": a, "b": b, "relation": relation,
        "gates": {"a": float(gate_a), "b": float(gate_b)},
        "quadrants": {"neither": n00, "b_only": n01, "a_only": n10, "both": n11},
        "n": n, "n_a": n_a, "n_b": n_b, "phi": phi, "jaccard": jaccard, "chi2": chi2,
        "expected_both": float(expected11),
        "frac_a_in_b": frac_a_in_b, "frac_b_in_a": frac_b_in_a,
        "orphan_fraction": orphan, "double_positive_fraction": double_fraction,
        "adjacent_orphan_share": adjacent_share,
        "conditional_shift_fit": shift,
        "contradiction": float(score),
        "plot_recommended": bool(score >= t["plot_at"]),
    }
    if with_grid:
        out["density"] = _density_grid(fa, fb, bins)
    return out


def _adjacent_share(ds, suspects, neighbours_flag, *, seed=0, sample=5_000):
    """Of the suspect cells, the share with a flagged cell within ~1.2 cell
    diameters -- spill from a neighbour rather than a wrong gate."""
    from plexora.plugins.gating.server.autogate import kernels

    c = cellmod.cells(ds)
    rows = np.flatnonzero(suspects & c.valid)
    if not rows.shape[0]:
        return None
    rows = cellmod.seeded_subset(rows, sample, seed + 11)
    diameter = _cell_diameter(c)
    radius = THRESHOLDS["adjacent_diameters"] * diameter
    grid = cellmod.grid(ds, max(radius, c.radius_px()))
    _n, flagged = kernels.neighbour_counts(grid, query=rows, flags=neighbours_flag & c.valid,
                                           radius=radius)
    return float((flagged > 0).mean())


def _cell_diameter(c):
    if c.area is not None:
        area = c.area[np.isfinite(c.area) & c.valid]
        if area.size:
            return float(2 * np.sqrt(np.median(area) / np.pi))
    return (10.0 / c.pixel_um) if c.pixel_um else 20.0
