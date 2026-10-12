"""Threshold suggestions for an isosurface, and the histogram they sit on.

Deterministic, and only suggestions: Otsu (the default the slider starts
at), the triangle method (for a small bright population on a large dim
background) and the split of a two-component Gaussian mixture of the log
intensities. All three are in the channel's raw units (after any background
subtraction the surface will use), never a display window's.
"""

from __future__ import annotations

import math

import numpy as np

#: Voxels the statistics are computed from (a strided sample above this).
SAMPLE = 2_000_000
#: Values the mixture is fitted to: EM is iterative, and a split this coarse
#: is as well placed from a hundred thousand values as from millions.
GMM_SAMPLE = 100_000
BINS = 256


def _sample(block) -> np.ndarray:
    values = np.asarray(block, dtype=np.float32).ravel()
    if values.size > SAMPLE:
        values = values[:: int(math.ceil(values.size / SAMPLE))]
    return values[np.isfinite(values)]


def _gmm_split(values) -> float | None:
    """Where a two-component Gaussian mixture of log(1 + v) changes component,
    by EM from the 20th and 99th percentiles."""
    if values.size > GMM_SAMPLE:
        values = values[:: int(math.ceil(values.size / GMM_SAMPLE))]
    v = np.log1p(np.clip(values.astype(np.float64), 0, None))
    if v.size < 100 or float(v.std()) == 0.0:
        return None
    mu = np.array([np.percentile(v, 20), np.percentile(v, 99)])
    if mu[1] - mu[0] < 1e-6:
        return None
    sd = np.array([v.std(), v.std()]) / 2 + 1e-6
    weight = np.array([0.8, 0.2])
    for _ in range(60):
        dens = weight / (sd * math.sqrt(2 * math.pi)) * np.exp(
            -0.5 * ((v[:, None] - mu) / sd) ** 2)
        total = dens.sum(axis=1, keepdims=True)
        total[total == 0] = 1e-300
        resp = dens / total
        n = resp.sum(axis=0) + 1e-12
        mu_new = (resp * v[:, None]).sum(axis=0) / n
        sd = np.sqrt((resp * (v[:, None] - mu_new) ** 2).sum(axis=0) / n) + 1e-6
        weight = n / n.sum()
        if np.allclose(mu_new, mu, atol=1e-6):
            mu = mu_new
            break
        mu = mu_new
    lo, hi = (0, 1) if mu[0] < mu[1] else (1, 0)
    grid = np.linspace(mu[lo], mu[hi], 512)
    a = weight[lo] / sd[lo] * np.exp(-0.5 * ((grid - mu[lo]) / sd[lo]) ** 2)
    b = weight[hi] / sd[hi] * np.exp(-0.5 * ((grid - mu[hi]) / sd[hi]) ** 2)
    crossing = np.nonzero(b >= a)[0]
    if not len(crossing):
        return None
    return float(np.expm1(grid[crossing[0]]))


def suggest(block) -> dict:
    """`{range, histogram: {lo, hi, counts, log}, thresholds: {otsu, triangle,
    gmm}, default}` over `block`."""
    from skimage import filters

    values = _sample(block)
    if not values.size:
        return {"range": None, "histogram": None, "thresholds": {}, "default": None}
    lo, hi = float(values.min()), float(values.max())
    top = float(np.percentile(values, 99.99))
    span_hi = top if top > lo else hi
    counts, _ = np.histogram(values, bins=BINS, range=(lo, span_hi if span_hi > lo else lo + 1))
    thresholds = {}
    if hi > lo:
        try:
            thresholds["otsu"] = float(filters.threshold_otsu(values))
        except ValueError:
            pass
        try:
            thresholds["triangle"] = float(filters.threshold_triangle(values))
        except ValueError:
            pass
        split = _gmm_split(values)
        if split is not None and lo < split < hi:
            thresholds["gmm"] = split
    default = thresholds.get("otsu", (lo + hi) / 2 if hi > lo else None)
    return {
        "range": [lo, hi],
        "histogram": {"lo": lo, "hi": span_hi if span_hi > lo else lo + 1,
                      "counts": counts.astype(int).tolist(), "log": True},
        "thresholds": thresholds,
        "default": default,
        "sampled": int(values.size),
    }
