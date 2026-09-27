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
from plexora.ai import vocabulary
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod

#: The vocabulary's relations, and "independent" for a pair it relates not at all.
RELATIONS = (*vocabulary.RELATIONS, "independent")

#: [cal] how far a relation may be off before it is called a contradiction.
THRESHOLDS = {"coexpressed_expected": 0.7, "exclusive_tolerated": 0.15,
              "subset_tolerated": 0.3, "independent_phi": 0.5, "plot_at": 0.5,
              "adjacent_diameters": 1.2, "control_percentile": 99.0,
              "control_min_cells": 50}

#: Which of the partner's populations is a negative control for the marker:
#: the partner's positives for an exclusive pair (CD20+ cells are CD3-), its
#: negatives for a subset or co-expressed one (CD3- cells are CD8-). An
#: independent partner controls nothing.
CONTROL_POPULATION = {"exclusive": "P+", "subset": "P-", "coexpressed": "P-"}


def _axis_range(col):
    """[p0.5, p99.9] of a column's body, in the fit's space: a floor spike of
    unmeasured cells would otherwise squeeze every real cell into a strip."""
    data = col.body if col.body.size else col.fit
    if not data.size:
        return 0.0, 1.0
    lo, hi = (float(v) for v in profmod._sorted_quantile(data, np.array([0.005, 0.999])))
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


def _density_grid(fa, fb, bins, col_a, col_b):
    ok = np.isfinite(fa) & np.isfinite(fb)
    if ok.sum() < 2:
        return None
    lo_a, hi_a = _axis_range(col_a)
    lo_b, hi_b = _axis_range(col_b)
    counts, _edges_a, _edges_b = np.histogram2d(fa[ok], fb[ok], bins=bins,
                                                range=((lo_a, hi_a), (lo_b, hi_b)))
    scaled = np.log1p(counts)
    top = scaled.max() or 1.0
    grid = np.round(scaled / top * 255).astype(np.uint8)
    return {"bins": int(bins), "a_range": [float(lo_a), float(hi_a)],
            "b_range": [float(lo_b), float(hi_b)],
            "spaces": {"a": "log1p" if col_a.to_log else "values",
                       "b": "log1p" if col_b.to_log else "values"},
            "floor_excluded": {"a": int(col_a.floor_n), "b": int(col_b.floor_n)},
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
        out["density"] = _density_grid(fa, fb, bins, col_a, col_b)
    return out


def negative_control(ds, a, gate_a, b, gate_b, *, relation, high_a=None, high_b=None):
    """The FACS negative control: the marker's p99 among cells the partner
    says must be negative for it (`CONTROL_POPULATION`). A gate below it calls
    more than one in a hundred of those cells positive.

    {partner, relation, population, n, p99_fit, p99, fraction_above_current},
    or None (independent partner, or too few control cells)."""
    population = CONTROL_POPULATION.get(relation)
    if population is None:
        return None
    col_a, col_b = profmod.column(ds, a), profmod.column(ds, b)
    va, vb = cellmod.values(ds, a), cellmod.values(ds, b)
    high_b = float(col_b.sorted32[-1]) if high_b is None else float(high_b)
    finite = np.isfinite(va) & np.isfinite(vb)
    pb = gate_rule.passes(vb, gate_b, high_b) & finite
    members = pb if population == "P+" else (~pb & finite)
    if int(members.sum()) < THRESHOLDS["control_min_cells"]:
        return None
    fa = col_a.to_fit(va[members])
    p99_fit = float(np.percentile(fa, THRESHOLDS["control_percentile"]))
    high_a = float(col_a.sorted32[-1]) if high_a is None else float(high_a)
    above = gate_rule.passes(va[members], gate_a, high_a)
    return {"partner": b, "relation": relation, "population": population,
            "n": int(members.sum()), "p99_fit": p99_fit,
            "p99": float(col_a.from_fit(p99_fit)),
            "fraction_above_current": float(above.mean())}


#: [cal] a conditional gate ("positive only within the partner's positives"):
#: how many partner-positive cells a fit needs, and the mixtures tried.
WITHIN = {"min_cells": 200, "components": (3, 2)}


def within_partner(ds, marker, partner, partner_gate, *, current=None, seed=0) -> dict:
    """The marker's gate among the partner's positives only: a subset marker
    whose stain is real inside the partner's population and noise (or another
    lineage's spill) outside it, as CD57 is within CD45+ cells.

    The threshold is the mixture's crossover fitted to the marker's values in
    those cells (`profile.pools`, the Auto gate's rule on the subset), or --
    when that fit does not separate two populations -- the FACS negative
    control (the marker's p99 among partner-negative cells). Reported beside
    it: how many cells it calls within the partner and how many outside, which
    the plain gate at the same threshold would also call.

    {ok, low, method, separation_d, n_partner_positive, n_positive_within,
    n_positive_outside, control, ...}; `ok` False with a `reason` when the
    partner has too few positives."""
    col = profmod.column(ds, marker)
    col_b = profmod.column(ds, partner)
    va, vb = cellmod.values(ds, marker), cellmod.values(ds, partner)
    finite = np.isfinite(va) & np.isfinite(vb)
    inside = gate_rule.passes(vb, float(partner_gate), float(col_b.sorted32[-1])) & finite
    n_in = int(inside.sum())
    out = {"marker": marker, "partner": partner, "partner_gate": float(partner_gate),
           "n_partner_positive": n_in, "current": current}
    if n_in < WITHIN["min_cells"]:
        return {**out, "ok": False,
                "reason": f"only {n_in} {partner}-positive cells; a conditional gate needs "
                          f"{WITHIN['min_cells']}"}
    body = np.sort(col.to_fit(va[inside]))
    body = body[profmod.floor_spike(body):]
    fitted = None
    for k in WITHIN["components"]:
        fitted = profmod._fit_light(body, k, seed=seed)
        if fitted is not None:
            break
    d, gate_fit, method = None, None, None
    if fitted is not None:
        pool = profmod.pools(fitted)
        d = profmod._pair_d(pool["mu_bg"], pool["sd_bg"], pool["mu_pos"], pool["sd_pos"])
        if d >= profmod.THRESHOLDS["weak_d"]:
            gate_fit, method = pool["gate"], "gmm_within"
    control = negative_control(ds, marker, float(current if current is not None else
                                                 col.from_fit(body[len(body) // 2])),
                               partner, float(partner_gate), relation="subset")
    if gate_fit is None:
        if control is None:
            return {**out, "ok": False, "separation_d": d,
                    "reason": f"{marker} does not separate among {partner}-positive cells, "
                              "and there are too few partner-negative cells for a control"}
        gate_fit, method = control["p99_fit"], "control_p99"
    low = float(col.from_fit(gate_fit))
    top = float(col.sorted32[-1])
    called = gate_rule.passes(va, low, top) & finite
    n_within = int((called & inside).sum())
    n_outside = int((called & ~inside).sum())
    return {**out, "ok": True, "low": low, "method": method, "separation_d": d,
            "n_positive_within": n_within, "n_positive_outside": n_outside,
            "fraction_within": n_within / n_in if n_in else None,
            "outside_share": n_outside / max(1, n_within + n_outside), "control": control}


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
