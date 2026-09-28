"""The mixture maths gating's manual half shares with automatic gating.

A marker's intensity is fitted as a Gaussian mixture (model.fit_for); these
turn a fit into its background and positive POPULATIONS -- moment-matched
pools of the components either side of the split -- and the GUARD BAND a
gate should stay inside.

In gating core rather than under autogate/, because the Free `adjust_gate`
steps a gate with them, and gating's manual workflow must never depend on the
automatic-gating (Paid) package: that is what lets the AI half be split out
into a package of its own later without touching the manual half. autogate/
re-exports every name here (profile.py, candidates.py), so its modules and
their tests are unchanged.
"""

from __future__ import annotations

import math

import numpy as np

def _pool(means, sds, weights):
    total = float(weights.sum())
    if total <= 0:
        return float(means[0]), float(sds[0]), 0.0
    mu = float((weights * means).sum() / total)
    second = float((weights * (sds ** 2 + means ** 2)).sum() / total)
    return mu, math.sqrt(max(second - mu * mu, 1e-12)), total


def _pair_d(m1, s1, m2, s2):
    return abs(m2 - m1) * math.sqrt(2.0 / max(s1 * s1 + s2 * s2, 1e-12))


#: [cal] when the top two components are one population: not clearly apart
#: themselves, and far closer to each other than the lower one is to them.
SPLIT_SAME_D = 2.5
SPLIT_RATIO = 3.0


def split_index(fitted):
    """How many of the (ascending) components are background.

    The Auto gate always takes the brightest component alone as the positive
    population. That is right when the background needs two components, and
    wrong when the POSITIVES do: a marker on two T-cell subsets at slightly
    different levels fits as background + two bright components, and the
    top-versus-rest crossover then lands inside the positives, calling half of
    them negative. So when the top two components are not clearly apart
    (Ashman D < SPLIT_SAME_D) and the gap below them is SPLIT_RATIO times
    wider, they are pooled as the positive population. Conservative on
    purpose: a zero-inflated background (a spike at zero, a background, a
    positive population) has a clear gap between its top two and keeps the
    default split.
    """
    means, sds, _weights = (np.asarray(a, dtype=np.float64) for a in fitted)
    k = means.shape[0]
    if k >= 3:
        top = _pair_d(means[-2], sds[-2], means[-1], sds[-1])
        below = _pair_d(means[-3], sds[-3], means[-2], sds[-2])
        if top < SPLIT_SAME_D and below >= SPLIT_RATIO * max(top, 1e-9):
            return k - 2
    return k - 1


def pools(fitted) -> dict:
    """Background and positive populations of a fit, moment-matched pools of
    the components either side of `split_index`, and the gate between them."""
    if isinstance(fitted, dict):
        fitted = (fitted["means"], fitted["sds"], fitted["weights"])
    means, sds, weights = (np.asarray(a, dtype=np.float64) for a in fitted)
    split = split_index((means, sds, weights))
    mu_bg, sd_bg, w_bg = _pool(means[:split], sds[:split], weights[:split])
    mu_pos, sd_pos, w_pos = _pool(means[split:], sds[split:], weights[split:])
    x = np.linspace(means[split - 1], means[split], 2000)
    from scipy.stats import norm

    background = sum(norm(means[i], sds[i]).pdf(x) * weights[i] for i in range(split))
    positive = sum(norm(means[i], sds[i]).pdf(x) * weights[i]
                   for i in range(split, means.shape[0]))
    above = np.flatnonzero(positive > background)
    gate = float(x[above[0]]) if above.size else float(means[split])
    return {"split": int(split), "default_split": int(means.shape[0] - 1),
            "mu_bg": mu_bg, "sd_bg": sd_bg, "w_bg": w_bg,
            "mu_pos": mu_pos, "sd_pos": sd_pos, "w_pos": w_pos, "gate": gate,
            # The components either side of the boundary: what the borderline
            # band is measured in (`capabilities.fit_band`'s rule, generalised).
            "sd_edge": float(min(sds[split - 1], sds[split]))}


#: [cal] the guard band, in each population's own sds.
GUARD_BG_SD = 1.0
GUARD_POS_SD = 0.5


def guard_band(fit):
    """(low, high) in fit units, or None without a fit."""
    if fit is None:
        return None
    p = pools(fit)
    low = p["mu_bg"] + GUARD_BG_SD * p["sd_bg"]
    high = p["mu_pos"] + GUARD_POS_SD * p["sd_pos"]
    if high <= low:
        high = low + 1e-6
    return float(low), float(high)
