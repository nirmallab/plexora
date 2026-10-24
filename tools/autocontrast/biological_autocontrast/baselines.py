"""The heuristics a learned model has to beat, in raw units, on the overview.

- percentile: Plexora's display calibration (calibration.channel_window on
  calibration._stats): p50..p99.5 with high >= 3x low for a marker, p30..p99
  for a nuclear channel. Called from Plexora, so it is the same function.
- gmm: the viewer's Auto button (data_model.channel_gmm_of), transcribed with
  a fixed random state so a proposal is reproducible; the parity test pins it
  to Plexora's function under the same global seed.
- fixed: p1..p99.9 of non-zero pixels.
"""

from __future__ import annotations

import numpy as np


def _finite(plane):
    plane = np.asarray(plane)
    if plane.dtype.kind == "f":
        plane = plane[np.isfinite(plane)]
    return plane.ravel()


def _ordered(low, high):
    low, high = float(low), float(high)
    if not high > low:
        high = low + 1.0
    return [low, high]


def percentile_window(plane, nuclear: bool):
    from plexora.agent.evidence.calibration import _stats, channel_window

    data = _finite(plane)
    if not data.size:
        return None
    return _ordered(*channel_window(_stats(data), "nuclear" if nuclear else "marker"))


#: The GMM is fitted on at most this many pixels (a seeded sample above it):
#: a full 2 Mpx overview costs ~25 s a channel, a 200k sample under 3 s.
GMM_MAX_PIXELS = 200_000


def gmm_window(plane, random_state=0, max_pixels=GMM_MAX_PIXELS):
    """channel_gmm_of's vmin/vmax: a 3-component GMM on log non-zero pixels;
    black at the crossing of the upper two components, white at mean2 + 2 sd2."""
    from scipy.stats import norm
    from sklearn.mixture import GaussianMixture

    data = _finite(plane)
    nonzero = data[data > 0]
    if nonzero.size < 50:
        return None
    if max_pixels and nonzero.size > max_pixels:
        pick = np.random.default_rng(random_state).choice(nonzero.size, max_pixels, replace=False)
        nonzero = nonzero[np.sort(pick)]
    # In the image's own dtype, as channel_gmm_of does (log of uint16 is
    # float32); the fit is sensitive enough that float64 lands elsewhere.
    logged = np.log(nonzero)
    if np.ptp(logged) <= 0:
        return None
    gmm = GaussianMixture(3, max_iter=1000, tol=1e-6, random_state=random_state)
    gmm.fit(logged.reshape((-1, 1)))
    means = gmm.means_[:, 0]
    _i0, i1, i2 = np.argsort(means)
    mean1, mean2 = means[[i1, i2]]
    std1, std2 = gmm.covariances_[[i1, i2], 0, 0] ** 0.5
    x = np.linspace(mean1, mean2, 50)
    y1 = norm(mean1, std1).pdf(x) * gmm.weights_[i1]
    y2 = norm(mean2, std2).pdf(x) * gmm.weights_[i2]
    lmax = mean2 + 2 * std2
    lmin = x[np.argmin(np.abs(y1 - y2))]
    if lmin >= mean2:
        lmin = mean2 - 2 * std2
    vmin = max(np.exp(lmin), data.min(), 0)
    vmax = min(np.exp(lmax), data.max())
    return _ordered(np.rint(vmin), np.rint(vmax))


def fixed_window(plane, low=1.0, high=99.9):
    data = _finite(plane)
    nonzero = data[data > 0]
    data = nonzero if nonzero.size >= 100 else data
    if not data.size:
        return None
    lo, hi = np.percentile(data.astype(np.float64), [low, high])
    return _ordered(lo, hi)


METHODS = {"percentile": percentile_window, "gmm": gmm_window, "fixed": fixed_window}


def all_windows(plane, nuclear: bool) -> dict:
    out = {}
    for name in METHODS:
        try:
            if name == "percentile":
                out[name] = percentile_window(plane, nuclear)
            else:
                out[name] = METHODS[name](plane)
        except Exception as exc:  # a heuristic failing is a recorded fact, not a crash
            out[name] = None
            out[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
    return out
