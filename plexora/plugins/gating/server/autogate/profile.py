"""A marker's distribution, reduced to the numbers that say how hard it is to gate.

The existing auto gate (`model.fit_for`) answers "where"; this answers "how
sure". Five estimators of the threshold, how far apart they land, how
separated the two populations are, how stable the fit is under resampling, what
shape the distribution has -- and from those a first-tier score that decides,
deterministically, whether the GMM gate can be accepted without anybody
looking at a cell.

Everything is whole-column numpy: one float32 sort per column serves every
quantile and every positive count (`searchsorted`), under exactly the rule the
viewer colours by (`low < v <= high` in float32, `plexora.agent.gate_rule`).
The mixture fits are the gating plugin's own (`model.fit_for`, seeded, fitted
above a spike of unmeasured cells at the minimum), so the profile's gate IS
the Auto button's gate.

Cut-points marked [cal] are first guesses to be calibrated against expert
gates (`plexora ai bench gating`); they live in `THRESHOLDS` so a calibration
changes one dict.
"""

from __future__ import annotations

import math
import time
import warnings

import numpy as np

from plexora.plugins.gating.server import model
from plexora.agent.expression import LOG_CEILING
from plexora.plugins.gating.server.autogate import schemas

#: Quantiles reported for every marker, in percent.
QGRID = (1.0, 5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.0, 99.9)

#: Integer ceilings an intensity column may be clipped at.
DTYPE_CEILINGS = (255.0, 4095.0, 16383.0, 65535.0)

#: Every cut-point the profile decides with. [cal] = to calibrate.
THRESHOLDS = {
    "pileup_fraction": 0.002,          # [cal] cells sitting at the column max
    "pileup_min_cells": 20,
    "saturated_cell_fraction": 0.005,  # [cal] ... and at an integer ceiling
    "degenerate_min_cells": 200,
    "degenerate_min_unique": 20,
    "degenerate_max_zero": 0.95,
    "unimodal_min_decades": 0.5,       # [cal]
    "bimodal_d": 2.5,                  # [cal]
    "bimodal_valley": 0.6,             # [cal]
    "bimodal_pf_spread": 0.03,         # [cal]
    "bimodal_min_pf": 0.005,
    "bimodal_max_pf": 0.95,
    "weak_d": 1.5,                     # [cal]
    "weak_valley": 0.85,               # [cal]
    "bc_bimodal": 5.0 / 9.0,           # the uniform distribution's coefficient
    "tail_skew": 1.0,
    "tail_max_pf": 0.15,
    "agree_moderate": 0.10,            # [cal] cells the estimators disagree about,
    "agree_poor": 0.30,                # [cal]   as a share of the positive calls
    "unstable_gate_bg": 0.3,           # [cal] bootstrap gate sd, in background sds
    "weak_signal_ratio": 2.0,          # [cal] positive / background, linear units
    "sparse_cells": 500,
    "t1_accept": 0.75,                 # [cal]
    "t2_min": 0.5,                     # [cal]
}

#: How big a subsample the resampling statistics use.
BOOT_SIZE = 20_000
BIC_SIZE = 20_000
MOMENT_SIZE = 200_000


# -- the column, prepared once ------------------------------------------------


class Column:
    """One marker column: finite values sorted in float32, and the fit space.

    `sorted32` is what every count uses -- the viewer's float32 rule on a
    sorted array is two `searchsorted` calls. `fit` is the same values in the
    space the mixture is fitted in (log1p for raw non-negative intensities, as
    they stand otherwise -- `model.fit_for`'s rule), ascending.
    """

    __slots__ = ("marker", "n", "n_finite", "sorted32", "fit", "to_log", "log_transformed",
                 "floor_n", "body")

    def __init__(self, marker, values, log_transformed):
        v32 = np.asarray(values, dtype=np.float32)
        finite = np.isfinite(v32)
        self.marker = marker
        self.n = int(v32.size)
        self.n_finite = int(finite.sum())
        self.sorted32 = np.sort(v32[finite])
        self.log_transformed = bool(log_transformed)
        self.to_log = (not self.log_transformed and self.n_finite > 0
                       and float(self.sorted32[0]) >= 0)
        wide = self.sorted32.astype(np.float64)
        self.fit = np.log1p(wide) if self.to_log else wide
        self.floor_n = floor_spike(self.fit)
        # What the mixtures are fitted to: the values above a floor spike.
        self.body = self.fit[self.floor_n:]

    # -- units --

    def to_fit(self, raw):
        raw = np.asarray(raw, dtype=np.float64)
        return np.log1p(np.maximum(raw, 0.0)) if self.to_log else raw

    def from_fit(self, fit):
        fit = np.asarray(fit, dtype=np.float64)
        return np.expm1(fit) if self.to_log else fit

    def linear(self, fit):
        """Intensity-like units for a ratio: raw counts, or expm1 of a table
        that is already log1p'd. None when the values are neither (z-scores)."""
        fit = np.asarray(fit, dtype=np.float64)
        if self.to_log or self.log_transformed:
            return np.expm1(fit)
        return None

    # -- counts --

    def n_positive(self, low, high=None):
        """Cells with low < v <= high, compared in float32 (`gate_rule.passes`)."""
        s = self.sorted32
        if not s.size:
            return 0
        high = float(s[-1]) if high is None else float(high)
        lo = np.searchsorted(s, np.float32(low), side="right")
        hi = np.searchsorted(s, np.float32(high), side="right")
        return int(max(0, hi - lo))

    def n_positive_many(self, lows, high=None):
        s = self.sorted32
        if not s.size:
            return np.zeros(len(lows), dtype=np.int64)
        high = float(s[-1]) if high is None else float(high)
        lo = np.searchsorted(s, np.asarray(lows, dtype=np.float32), side="right")
        hi = np.searchsorted(s, np.float32(high), side="right")
        return np.maximum(0, hi - lo).astype(np.int64)

    def fraction(self, low, high=None):
        return self.n_positive(low, high) / self.n_finite if self.n_finite else None

    def percentile_of(self, raw):
        """Percent of finite cells at or below `raw` (float32 rule)."""
        if not self.n_finite:
            return None
        return float(np.searchsorted(self.sorted32, np.float32(raw), side="right")
                     / self.n_finite * 100.0)

    def quantiles(self, percents, space="raw"):
        data = self.fit if space == "fit" else self.sorted32.astype(np.float64)
        if not data.size:
            return [None] * len(percents)
        return [float(v) for v in _sorted_quantile(data, np.asarray(percents) / 100.0)]


_sorted_quantile = model._sorted_quantile
floor_spike = model.floor_spike


def fit_for(ds, marker):
    """The Auto button's fit (`model.fit_for`), which leaves out a floor spike."""
    return model.fit_for(ds, marker)


def fingerprint(ds) -> str:
    """Which matrix, and whether log1p is applied: part of every cache key over
    the table's values, so a changed expression source never serves stale
    numbers."""
    return getattr(ds.table, "expression_fingerprint", None) or ""


def column(ds, marker) -> Column:
    """The prepared column, cached on the handle set."""
    def compute():
        values = ds.table.columns([marker])[marker]
        return Column(marker, values, ds.table.log_transformed)

    return ds.cached(("autogate.column", marker, fingerprint(ds)), compute)


def _seeded_subsample(values, size, seed):
    if values.shape[0] <= size:
        return values
    rng = np.random.default_rng(seed)
    return values[rng.choice(values.shape[0], size=size, replace=False)]


# -- estimators ----------------------------------------------------------------


# The mixture maths the Free `adjust_gate` shares with automatic gating lives
# in gating core (server/mixture.py), so gating's manual half never imports
# this package. Re-exported here: every autogate module says `profmod.pools`.
from plexora.plugins.gating.server.mixture import (  # noqa: E402,F401
    SPLIT_RATIO, SPLIT_SAME_D, _pair_d, _pool, pools, split_index)


def _background(fitted):
    """(mu, sd, weight) of the background pool (see `pools`)."""
    p = pools(fitted)
    return p["mu_bg"], p["sd_bg"], p["w_bg"]


def _histogram(fit, lo, hi, bins=256, smooth=2.0):
    from scipy.ndimage import gaussian_filter1d

    counts, edges = np.histogram(fit, bins=bins, range=(lo, hi))
    density = counts.astype(np.float64)
    if smooth:
        density = gaussian_filter1d(density, smooth, mode="nearest")
    centres = (edges[:-1] + edges[1:]) / 2
    return centres, density


def _antimode(centres, density, min_separation=8):
    """The deepest point between the two highest separated modes, or None."""
    d = density
    if d.size < 3:
        return None
    interior = (d[1:-1] >= d[:-2]) & (d[1:-1] > d[2:])
    peaks = np.flatnonzero(interior) + 1
    if peaks.size < 2:
        return None
    ranked = peaks[np.argsort(-d[peaks], kind="stable")]
    first = int(ranked[0])
    second = next((int(p) for p in ranked[1:] if abs(int(p) - first) >= min_separation), None)
    if second is None:
        return None
    a, b = sorted((first, second))
    trough = a + int(np.argmin(d[a:b + 1]))
    return float(centres[trough])


def _otsu(centres, counts):
    p = counts / max(counts.sum(), 1e-12)
    w0 = np.cumsum(p)
    mu = np.cumsum(p * centres)
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu_t * w0 - mu) ** 2 / (w0 * (1.0 - w0))
    between[~np.isfinite(between)] = -1
    if between.max() <= 0:
        return None
    return float(centres[int(np.argmax(between))])


def _valley(centres, density, gate):
    """How deep the dip between the two populations is: the lowest density
    between the highest mode on each side of the gate, over the lower of those
    two modes. 1.0 when either side has no mode at all -- a positive
    population that is only a shoulder on the background's tail.

    Modes, not maxima: right of the gate the density's maximum is often the
    background's own tail at the gate itself, and a rare population's hump
    beyond it is the peak that matters.
    """
    d = density
    if gate is None or d.size < 3:
        return 1.0
    gi = int(np.clip(np.searchsorted(centres, gate), 0, d.size - 1))
    interior = (d[1:-1] >= d[:-2]) & (d[1:-1] > d[2:])
    peaks = np.flatnonzero(interior) + 1
    left = peaks[peaks < gi]
    right = peaks[peaks > gi]
    if not left.size or not right.size:
        return 1.0
    lp = int(left[np.argmax(d[left])])
    rp = int(right[np.argmax(d[right])])
    low_peak = min(d[lp], d[rp])
    if low_peak <= 0:
        return 1.0
    return float(min(1.0, d[lp:rp + 1].min() / low_peak))


def _gmm_bic(sample, k):
    from sklearn.mixture import GaussianMixture

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gmm = GaussianMixture(k, max_iter=300, tol=1e-4, random_state=0)
        gmm.fit(sample.reshape(-1, 1))
    return float(gmm.bic(sample.reshape(-1, 1)))


def _fit_light(values, components, *, seed=0, size=50_000):
    """A resampling refit: the Auto fit's model at a looser tolerance.

    The primary gate always comes from `model.fit_for` at the plugin's own
    settings; refits only measure how much that gate would move, and a
    tolerance of 1e-6 there costs seconds per marker for digits nobody reads.
    """
    from sklearn.mixture import GaussianMixture

    values = values[np.isfinite(values)]
    if values.size < components * 10 or np.unique(values[:5000]).size < components:
        return None
    sample = _seeded_subsample(values, size, seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gmm = GaussianMixture(components, max_iter=300, tol=1e-4, random_state=0)
        gmm.fit(sample.reshape(-1, 1))
    order = np.argsort(gmm.means_[:, 0])
    return (gmm.means_[order, 0], np.sqrt(gmm.covariances_[order, 0, 0]),
            gmm.weights_[order])


# -- the profile -----------------------------------------------------------------


def profile_marker(ds, marker, *, seed=0, n_boot=5, boot_size=BOOT_SIZE, high=None,
                   with_cell_qc=True, image_qc=None, compartment=None) -> dict:
    """The whole profile (see the module docstring), JSON-safe. Cached.
    `compartment` (the panel's) decides whether a DNA correlation is bleed."""
    key = ("autogate.profile", marker, schemas.PROFILE_VERSION, int(seed), int(n_boot),
           bool(with_cell_qc), compartment, fingerprint(ds))

    def compute():
        return _profile(ds, marker, seed=seed, n_boot=n_boot, boot_size=boot_size,
                        with_cell_qc=with_cell_qc, compartment=compartment)

    profile = dict(ds.cached(key, compute))
    if image_qc is not None:
        profile["image_qc"] = image_qc
    if high is not None:
        profile = at_gate(ds, profile, profile["fit"]["gate_raw"], high)
    return score(profile)


def _profile(ds, marker, *, seed, n_boot, boot_size, with_cell_qc, compartment=None):
    started = time.perf_counter()
    col = column(ds, marker)
    t = THRESHOLDS
    out = {"version": schemas.PROFILE_VERSION, "project": ds.name, "marker": marker,
           "seed": int(seed), "log_transformed": col.log_transformed,
           "fit_space": "log1p" if col.to_log else "values"}
    s = col.sorted32
    n_unique = int(1 + np.count_nonzero(np.diff(s))) if s.size else 0
    frac_zero = float(np.count_nonzero(s == 0) / s.size) if s.size else 0.0
    col_max = float(s[-1]) if s.size else None
    at_max = int(s.size - np.searchsorted(s, s[-1], side="left")) if s.size else 0
    integer_valued = bool(s.size) and bool(np.all(np.mod(_seeded_subsample(s, 5000, seed), 1)
                                                   == 0))
    ceiling = None
    if integer_valued and col_max is not None:
        ceiling = next((c for c in DTYPE_CEILINGS if 0.98 * c <= col_max <= c), None)
    out["counts"] = {
        "n": col.n, "n_finite": col.n_finite,
        "frac_nan": (1 - col.n_finite / col.n) if col.n else 0.0,
        "frac_zero": frac_zero, "n_unique": n_unique,
        "frac_at_max": (at_max / s.size) if s.size else 0.0,
        "integer_valued": integer_valued, "dtype_ceiling": ceiling,
    }
    out["quantiles"] = {"p": list(QGRID), "raw": col.quantiles(QGRID, "raw"),
                        "fit": col.quantiles(QGRID, "fit")}
    lin = None
    if col.body.size:
        # On the body: a floor spike of unmeasured cells is not dynamic range.
        lin = col.linear(_sorted_quantile(col.body, np.array([0.01, 0.999])))
    out["dynamic_range_decades"] = (float(np.log10((lin[1] + 1) / (lin[0] + 1)))
                                    if lin is not None and lin[0] > -1 else None)
    flags = []
    if (s.size and out["counts"]["frac_at_max"] > t["pileup_fraction"]
            and at_max >= t["pileup_min_cells"]):
        flags.append("pileup")
        if ceiling is not None and out["counts"]["frac_at_max"] > t["saturated_cell_fraction"]:
            flags.append("saturated")
    if col.n_finite < t["sparse_cells"]:
        flags.append("sparse")
    if (not col.log_transformed and col.to_log and s.size
            and out["quantiles"]["raw"][-1] < LOG_CEILING and not integer_valued):
        flags.append("log_ambiguous")
    if not s.size:
        flags.append("no_values")

    # The Auto button's own fit, from its own cache: the profile's gate IS the
    # gate the sidebar would set.
    shared = fit_for(ds, marker) if s.size else None
    if col.floor_n:
        flags.append("floor_spike")
        out["counts"]["floor_excluded"] = int(col.floor_n)
    fitted = ((np.asarray(shared["means"]), np.asarray(shared["sds"]),
               np.asarray(shared["weights"])) if shared else None)
    out["fit"] = None
    if fitted is not None:
        populations = pools(fitted)
        gate_fit = populations["gate"]
        auto_gate_fit = model._crossover(fitted)
        mu_bg, sd_bg, w_bg = populations["mu_bg"], populations["sd_bg"], populations["w_bg"]
        mu_pos, sd_pos, w_pos = (populations["mu_pos"], populations["sd_pos"],
                                 populations["w_pos"])
        if populations["split"] != populations["default_split"]:
            flags.append("positives_split")
        q1f, q999f = (float(v) for v in _sorted_quantile(col.body, np.array([0.01, 0.999])))
        lo_h, hi_h = (q1f, q999f) if q999f > q1f else (q1f - 1.0, q1f + 1.0)
        centres, density = _histogram(col.body, lo_h, hi_h)
        raw_counts, _ = np.histogram(col.body, bins=256, range=(lo_h, hi_h))
        fitted2 = _fit_light(col.body, 2, seed=seed)
        estimators = {
            "gmm3": float(gate_fit),
            "auto": float(auto_gate_fit),
            "gmm2": float(model._crossover(fitted2)) if fitted2 is not None else None,
            "kde": _antimode(centres, density),
            "otsu": _otsu(centres, raw_counts.astype(np.float64)),
            "bg_outlier": float(mu_bg + 3.0 * sd_bg),
        }
        values = [v for k, v in estimators.items() if v is not None and k != "auto"]
        spread = float(max(values) - min(values)) if len(values) > 1 else 0.0
        estimators["agree"] = spread
        estimators["agree_bg"] = spread / sd_bg if sd_bg > 0 else None
        # What the disagreement costs: the calls that change between the GMM
        # gate and the median of the other estimates, as a share of the
        # positive calls. The median, because one estimator wandering into a
        # dip INSIDE the background (a histogram antimode between two tissue
        # compartments; Otsu splitting a lopsided distribution) must not
        # outvote the rest; and counted in cells, because an empty valley lets
        # the estimates sit far apart for free. `spread_share` keeps the
        # lowest-to-highest count for the record.
        n_gmm = max(1, col.n_positive(float(col.from_fit(gate_fit))))
        others = [v for k, v in estimators.items()
                  if v is not None and k not in ("auto", "gmm3", "agree", "agree_bg")]
        consensus = float(np.median(others)) if others else float(gate_fit)
        flips = abs(col.n_positive(float(col.from_fit(consensus))) - n_gmm)
        estimators["consensus"] = consensus
        estimators["disagree_share"] = float(flips / n_gmm)
        estimators["consensus_ratio"] = float(
            col.n_positive(float(col.from_fit(consensus))) / n_gmm)
        spread = (col.n_positive(float(col.from_fit(min(values))))
                  - col.n_positive(float(col.from_fit(max(values))))) if values else 0
        estimators["spread_share"] = float(spread / n_gmm)

        d = abs(mu_pos - mu_bg) * math.sqrt(2.0 / max(sd_pos ** 2 + sd_bg ** 2, 1e-12))
        from scipy.stats import norm

        overlap = float(w_bg * norm.sf(gate_fit, mu_bg, sd_bg)
                        + w_pos * norm.cdf(gate_fit, mu_pos, sd_pos))
        moment = _seeded_subsample(col.body, MOMENT_SIZE, seed)
        centred = moment - moment.mean()
        m2 = float(np.mean(centred ** 2)) or 1e-12
        skew = float(np.mean(centred ** 3) / m2 ** 1.5)
        kurt = float(np.mean(centred ** 4) / m2 ** 2 - 3.0)
        n_m = moment.shape[0]
        bc = ((skew ** 2 + 1) / (kurt + 3 * (n_m - 1) ** 2 / ((n_m - 2) * (n_m - 3)))
              if n_m > 3 else None)
        valley = _valley(centres, density, gate_fit)

        gate_raw = float(col.from_fit(gate_fit))
        n_pos = col.n_positive(gate_raw)
        boot = {"n": 0, "gate_fit_sd": None, "gate_fit_sd_bg": None, "pf_spread": None,
                "skipped": None}
        if d >= 4.0 and valley <= 0.3:
            boot["skipped"] = "unambiguous (D >= 4, deep valley)"
        elif n_boot and col.body.shape[0] >= 100 * max(1, int(n_boot)):
            rng = np.random.default_rng(seed + 1)
            permutation = rng.permutation(col.body.shape[0])
            size = min(boot_size, col.body.shape[0] // max(1, n_boot))
            gates = []
            for i in range(int(n_boot)):
                part = col.body[permutation[i * size:(i + 1) * size]]
                if part.shape[0] < 50:
                    break
                refit = _fit_light(part, 3, seed=seed + i)
                if refit is not None:
                    gates.append(model._crossover(refit))
            if len(gates) >= 2:
                gates = np.asarray(gates)
                pfs = col.n_positive_many(col.from_fit(gates)) / max(1, col.n_finite)
                sd = float(gates.std(ddof=1))
                boot.update(n=int(gates.size), gate_fit_sd=sd,
                            gate_fit_sd_bg=(sd / sd_bg) if sd_bg > 0 else None,
                            pf_spread=float(pfs.max() - pfs.min()))
        bic = {"k1": None, "k2": None, "k3": None, "delta_21": None, "delta_32": None}
        sample = _seeded_subsample(col.body, BIC_SIZE, seed + 2)
        if sample.shape[0] >= 50 and n_unique >= 3:
            b1, b2, b3 = (_gmm_bic(sample, k) for k in (1, 2, 3))
            bic.update(k1=b1, k2=b2, k3=b3, delta_21=b1 - b2, delta_32=b2 - b3)
        linear = col.linear(np.array([mu_bg, mu_pos]))
        s2b = (float(linear[1] / max(linear[0], 1.0)) if linear is not None else None)
        out["fit"] = {
            "means": [float(v) for v in fitted[0]], "sds": [float(v) for v in fitted[1]],
            "weights": [float(v) for v in fitted[2]],
            "gate_fit": float(gate_fit), "gate_raw": gate_raw,
            "auto_gate_raw": float(col.from_fit(auto_gate_fit)),
            "split": populations["split"], "sd_edge": populations["sd_edge"],
            "estimators": estimators, "bic": bic, "boot": boot,
            "background": {"mean": mu_bg, "sd": sd_bg, "weight": w_bg},
            "positive": {"mean": mu_pos, "sd": sd_pos, "weight": w_pos},
            "separation": {"ashman_d": float(d), "overlap_mass": overlap,
                           "valley_ratio": float(valley), "bc": bc, "skew": skew,
                           "kurtosis": kurt},
            "signal_to_background": s2b,
        }
        out["positive_fraction"] = n_pos / col.n_finite if col.n_finite else None
        out["n_positive"] = n_pos
        if s2b is not None and s2b < t["weak_signal_ratio"]:
            flags.append("weak_signal")
        if estimators["disagree_share"] > t["agree_poor"]:
            flags.append("estimators_disagree")
        if (boot["gate_fit_sd_bg"] is not None
                and boot["gate_fit_sd_bg"] > t["unstable_gate_bg"]):
            flags.append("unstable_fit")
    else:
        out["positive_fraction"] = None
        out["n_positive"] = None

    out["cell_qc"] = None
    if with_cell_qc and out["fit"] is not None:
        from plexora.plugins.gating.server.autogate import qc_cells

        out["cell_qc"] = qc_cells.cell_qc(ds, col, out, seed=seed, compartment=compartment)
        flags.extend(out["cell_qc"].get("flags") or [])
    out["profile_flags"] = sorted(set(flags))
    out["image_qc"] = None
    out["cost_ms"] = int((time.perf_counter() - started) * 1000)
    return out


def at_gate(ds, profile, low, high=None):
    """The profile's gate-dependent numbers recomputed for another gate."""
    col = column(ds, profile["marker"])
    out = dict(profile)
    n_pos = col.n_positive(low, high)
    out["positive_fraction"] = n_pos / col.n_finite if col.n_finite else None
    out["n_positive"] = n_pos
    out["evaluated_at"] = {"low": float(low), "high": None if high is None else float(high)}
    return out


# -- class and first-tier score (pure) ------------------------------------------------


def classify(profile) -> str:
    """The distribution class, first match wins (THRESHOLDS)."""
    t = THRESHOLDS
    counts = profile.get("counts") or {}
    fit = profile.get("fit")
    flags = set(profile.get("profile_flags") or ()) | set(
        (profile.get("image_qc") or {}).get("flags") or ())
    if (counts.get("n_finite", 0) < t["degenerate_min_cells"]
            or counts.get("n_unique", 0) < t["degenerate_min_unique"]
            or counts.get("frac_zero", 0) > t["degenerate_max_zero"] or fit is None):
        return "degenerate"
    if "saturated" in flags or "pileup" in flags:
        return "saturated"
    sep = fit["separation"]
    delta = (fit.get("bic") or {}).get("delta_21")
    dr = profile.get("dynamic_range_decades")
    if (delta is not None and delta <= 0) or (dr is not None and dr < t["unimodal_min_decades"]):
        return "unimodal"
    pf = profile.get("positive_fraction") or 0.0
    spread = (fit.get("boot") or {}).get("pf_spread")
    if (sep["ashman_d"] >= t["bimodal_d"] and sep["valley_ratio"] <= t["bimodal_valley"]
            and (spread is None or spread <= t["bimodal_pf_spread"])
            and t["bimodal_min_pf"] <= pf <= t["bimodal_max_pf"]):
        return "bimodal"
    # Separated populations with no dip between them -- a rare positive
    # population is a shoulder on the background's tail -- are weakly bimodal,
    # not continuous: there is a boundary, it is just not visible in the
    # histogram.
    if sep["ashman_d"] >= t["weak_d"] and (
            sep["valley_ratio"] <= t["weak_valley"] or sep["ashman_d"] >= t["bimodal_d"]
            or (sep.get("bc") is not None and sep["bc"] > t["bc_bimodal"])):
        return "weakly_bimodal"
    if sep["ashman_d"] < t["weak_d"] and sep.get("skew", 0) > t["tail_skew"] \
            and pf < t["tail_max_pf"]:
        return "long_tail"
    return "continuous"


def score(profile) -> dict:
    """`profile` with `distribution_class`, `flags` and `t1` (re)computed.

    Pure: a function of the profile's own numbers and whatever image QC has
    been merged into it, so a merge can re-score without refitting.
    """
    t = THRESHOLDS
    out = dict(profile)
    flags = sorted(set(profile.get("profile_flags") or ())
                   | set((profile.get("image_qc") or {}).get("flags") or ()))
    out["flags"] = flags[:16]
    klass = classify(out)
    out["distribution_class"] = klass
    value = 1.0
    reasons = []

    def minus(amount, why):
        nonlocal value
        value -= amount
        reasons.append(why)

    fit = out.get("fit")
    if fit is None:
        minus(1.0, "no mixture to fit")
    else:
        sep = fit["separation"]
        d = sep["ashman_d"]
        if d < t["weak_d"]:
            minus(0.60, f"populations barely separated (D {d:.2f})")
        elif d < t["bimodal_d"]:
            minus(0.35, f"populations moderately separated (D {d:.2f})")
        if sep["valley_ratio"] > t["bimodal_valley"]:
            minus(0.25, f"shallow valley ({sep['valley_ratio']:.2f})")
        spread = (fit.get("boot") or {}).get("pf_spread")
        if spread is not None and spread > 0.08:
            minus(0.40, f"resampling moves the positive fraction by {spread:.1%}")
        elif spread is not None and spread > t["bimodal_pf_spread"]:
            minus(0.20, f"resampling moves the positive fraction by {spread:.1%}")
        if sep["overlap_mass"] > 0.05:
            minus(0.20, f"{sep['overlap_mass']:.1%} of cells in the overlap")
        pf = out.get("positive_fraction") or 0.0
        if pf < t["bimodal_min_pf"] or pf > 0.90:
            minus(0.15, f"extreme positive fraction ({pf:.2%})")
        estimators = fit.get("estimators") or {}
        agree = estimators.get("disagree_share")
        if agree is not None and agree > t["agree_poor"]:
            minus(0.35, _disagreement(agree, estimators.get("consensus_ratio")))
        elif agree is not None and agree > t["agree_moderate"]:
            minus(0.15, _disagreement(agree, estimators.get("consensus_ratio")))
    hard = schemas.hard(flags)
    for flag in hard:
        minus(0.30, f"technical flag: {flag}")
    for flag in schemas.soft(flags):
        if flag in ("sparse", "log_ambiguous", "pileup", "isolated_positives",
                    "size_correlated", "nuclear_bleed", "edge_enriched", "weak_signal"):
            minus(0.10, f"flag: {flag}")
    value = float(max(0.0, min(1.0, value)))
    accept = value >= t["t1_accept"] and klass == "bimodal" and not hard
    dr = out.get("dynamic_range_decades")
    if accept:
        tier = "T1"
    elif hard or klass == "degenerate" or (klass == "unimodal" and dr is not None
                                           and dr < t["unimodal_min_decades"]):
        tier = "QC"
    elif value >= t["t2_min"] and klass in ("bimodal", "weakly_bimodal", "long_tail"):
        tier = "T2"
    else:
        tier = "T3"
    out["t1"] = {"score": round(value, 3), "accept": bool(accept),
                 "recommended_tier": tier, "reasons": reasons[:6]}
    return out


#: A consensus this many times the GMM's count (or its reciprocal) reads as a
#: multiple: "disagree about 295% of the positive calls" says nothing.
RATIO_WORDING = 1.5


def _disagreement(share, ratio) -> str:
    if ratio is not None and ratio >= RATIO_WORDING:
        return f"the other estimators call {ratio:.1f}x as many cells positive as the GMM gate"
    if ratio is not None and 0 < ratio <= 1.0 / RATIO_WORDING:
        return (f"the other estimators call only {ratio:.0%} as many cells positive "
                "as the GMM gate")
    return f"estimators disagree about {share:.0%} of the positive calls"


def compact(profile) -> dict:
    """The few hundred bytes a dataset-wide listing carries per marker."""
    fit = profile.get("fit") or {}
    sep = fit.get("separation") or {}
    return {
        "marker": profile["marker"],
        "class": profile.get("distribution_class"),
        "t1": (profile.get("t1") or {}).get("score"),
        "tier": (profile.get("t1") or {}).get("recommended_tier"),
        "gate": fit.get("gate_raw"),
        "fraction": profile.get("positive_fraction"),
        "d": sep.get("ashman_d"),
        "valley": sep.get("valley_ratio"),
        "flags": (profile.get("flags") or [])[:6],
    }
