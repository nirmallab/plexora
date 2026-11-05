"""One marker across many images: align, compare, classify the drift.

A gate settled on one image is a starting point for the next, never a copy.
Each image's quantile curve (p5..p95, in the fit's space) is aligned to the
reference image's by least squares -- `q_image = a + b * q_reference` -- which
absorbs a staining or exposure shift (a) and a contrast change (b). The
reference gate carried through that line is the predicted gate for the image;
how well the line fits, and how far a and b are from (0, 1), say whether the
image is `stable`, `smooth_drift`, part of a `batch_effect`, an
`image_specific_shift`, a `distribution_change`, or a `staining_failure`.

Pure functions of the profiles' JSON -- nothing here reads a table.
"""

from __future__ import annotations

import math

import numpy as np

#: The quantiles the alignment is fitted on (percent).
ALIGN_P = (5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0)

#: [cal] in background sds of the reference, so the rules do not depend on the
#: table's units.
THRESHOLDS = {"stable_shift_bg": 0.25, "stable_scale": 0.10, "stable_pf_abs": 0.05,
              "stable_pf_rel": 0.30, "drift_rms_bg": 0.15, "change_rms_bg": 0.40,
              "drift_spearman": 0.7, "batch_gap": 3.0, "per_image_share": 0.30,
              "stable_share": 0.80}

CLASSES = ("stable", "smooth_drift", "batch_effect", "image_specific_shift",
           "distribution_change", "staining_failure")


def to_fit(value, space):
    if value is None:
        return None
    return math.log1p(max(float(value), 0.0)) if space == "log1p" else float(value)


def from_fit(value, space):
    return math.expm1(value) if space == "log1p" else float(value)


def curve(summary, space):
    """(percents, fit-space quantiles) from a compact profile's raw quantiles."""
    raw = summary.get("quantiles_raw") or {}
    xs, ys = [], []
    for p in ALIGN_P:
        value = raw.get(f"p{p:g}")
        if value is not None:
            xs.append(p)
            ys.append(to_fit(value, space))
    return np.asarray(xs), np.asarray(ys, dtype=np.float64)


def align(target_summary, reference_summary, space):
    """{a, b, rms} with target = a + b * reference over the shared quantiles."""
    pt, qt = curve(target_summary, space)
    pr, qr = curve(reference_summary, space)
    shared = sorted(set(pt.tolist()) & set(pr.tolist()))
    if len(shared) < 3:
        return None
    qt = np.array([qt[list(pt).index(p)] for p in shared])
    qr = np.array([qr[list(pr).index(p)] for p in shared])
    design = np.stack([np.ones_like(qr), qr], axis=1)
    (a, b), *_ = np.linalg.lstsq(design, qt, rcond=None)
    residual = qt - (a + b * qr)
    return {"a": float(a), "b": float(b), "rms": float(np.sqrt(np.mean(residual ** 2))),
            "n_quantiles": len(shared)}


def predict(alignment, reference_gate_raw, space):
    g = to_fit(reference_gate_raw, space)
    return from_fit(alignment["a"] + alignment["b"] * g, space)


def classify(alignment, target, reference, *, pf_target=None, pf_reference=None):
    """The drift class of one image for one marker.

    `target` / `reference` carry `class`, `metrics` (`sd_bg`), `summary`.
    """
    t = THRESHOLDS
    t_class, r_class = target.get("class"), reference.get("class")
    dr = (target.get("summary") or {}).get("signal_to_background")
    if t_class in ("degenerate", "unimodal") and r_class not in ("degenerate", "unimodal"):
        return "staining_failure"
    if dr is not None and dr < 1.5 and ((reference.get("summary") or {})
                                        .get("signal_to_background") or 0) >= 2.0:
        return "staining_failure"
    if alignment is None:
        return "distribution_change"
    sd = (reference.get("metrics") or {}).get("sd_bg") or 1.0
    shift = abs(alignment["a"] + (alignment["b"] - 1.0) * ((reference.get("metrics") or {})
                                                          .get("mu_bg") or 0.0)) / sd
    rms = alignment["rms"] / sd
    if rms > t["change_rms_bg"]:
        return "distribution_change"
    pf_ok = True
    if pf_target is not None and pf_reference is not None:
        pf_ok = abs(pf_target - pf_reference) <= max(t["stable_pf_abs"],
                                                     t["stable_pf_rel"] * pf_reference)
    if shift <= t["stable_shift_bg"] and abs(alignment["b"] - 1.0) <= t["stable_scale"] \
            and pf_ok and rms <= t["drift_rms_bg"]:
        return "stable"
    return "image_specific_shift"


def dataset_classes(per_image):
    """Refine per-image classes with the dataset's pattern: a monotone trend
    along acquisition order is `smooth_drift`; two separated groups of shifts
    are a `batch_effect`. `per_image` is [{project, shift, class}] in order."""
    shifts = np.array([p["shift"] for p in per_image if p.get("shift") is not None])
    out = [dict(p) for p in per_image]
    if shifts.size >= 4:
        order = np.arange(shifts.size, dtype=np.float64)
        rank = np.argsort(np.argsort(shifts)).astype(np.float64)
        rho = float(np.corrcoef(order, rank)[0, 1]) if shifts.std() > 0 else 0.0
        if abs(rho) >= THRESHOLDS["drift_spearman"]:
            for p in out:
                if p.get("class") == "image_specific_shift":
                    p["class"] = "smooth_drift"
        else:
            ordered = np.sort(shifts)
            gaps = np.diff(ordered)
            k = int(np.argmax(gaps))
            low, high = ordered[:k + 1], ordered[k + 1:]
            within = max(float(np.concatenate([low - low.mean(), high - high.mean()]).std()),
                         1e-9)
            if low.size >= 2 and high.size >= 2 and gaps[k] > THRESHOLDS["batch_gap"] * within:
                cut = (ordered[k] + ordered[k + 1]) / 2
                for p in out:
                    if p.get("class") in ("image_specific_shift", "stable") \
                            and p.get("shift") is not None:
                        p["batch"] = "high" if p["shift"] > cut else "low"
                        if p.get("class") == "image_specific_shift":
                            p["class"] = "batch_effect"
    return out


def batch_split(shifts):
    """The cut between two separated groups of `shifts`, or None: at least
    four shifts, the largest gap more than `batch_gap` times the spread
    within the two sides, and two or more on each side."""
    shifts = np.asarray([s for s in shifts if s is not None], dtype=np.float64)
    if shifts.size < 4:
        return None
    ordered = np.sort(shifts)
    gaps = np.diff(ordered)
    k = int(np.argmax(gaps))
    low, high = ordered[:k + 1], ordered[k + 1:]
    within = max(float(np.concatenate([low - low.mean(), high - high.mean()]).std()), 1e-9)
    if low.size >= 2 and high.size >= 2 and gaps[k] > THRESHOLDS["batch_gap"] * within:
        return float((ordered[k] + ordered[k + 1]) / 2)
    return None


# -- engine v4: images aligned on their background (`groups`) --------------------

#: The classes whose mixture's background component is the image's
#: background; the others (one mode, or none) are all background, read
#: robustly (median and IQR), where a mixture would split one mode in two.
MIXTURE_BASIS = ("bimodal", "weakly_bimodal", "long_tail", "continuous")
#: [cal] An image's background maps onto another's when the contrast ratio
#: lies within `scale_range` and the mapped background quantiles stay within
#: `change_rms_bg` background sds (`mappable`); it joins a group within
#: `member_shift` sds of the scale image, a carried-without-positives image
#: within `no_positive_shift` and `no_positive_scale`.
GROUP_THRESHOLDS = {"scale_range": (0.5, 2.0), "member_shift": 1.0,
                    "no_positive_shift": 0.5, "no_positive_scale": (0.7, 1.4),
                    "failed_s2b": 1.5, "scale_s2b": 2.0}
#: The hard flags that make an image's stain a failure on its own.
FAILED_FLAGS = ("empty_channel", "no_values", "saturated")
#: v4's classes of one image against its group's scale image
#: (`classify_group`): `member_shifted` joins the group as `stable` does;
#: `no_positives` (one mode, matching the scale image's background) joins and
#: is carried without positives of its own to check.
GROUP_CLASSES = ("stable", "member_shifted", "no_positives", "image_specific_shift",
                 "distribution_change", "staining_failure")
MEMBER_CLASSES = ("stable", "member_shifted", "no_positives")
#: The percents of the profile's quantiles the background fit is checked on.
BG_PERCENTS = (1.0, 5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0)


def background(unit):
    """{mu, sd, basis} of a profiled unit's background in its fit space, or
    None: the mixture's background component for a class with one
    (`MIXTURE_BASIS`), else the median and IQR / 1.349 of all its cells."""
    space = unit.get("fit_space")
    metrics = unit.get("metrics") or {}
    if unit.get("class") in MIXTURE_BASIS and metrics.get("mu_bg") is not None \
            and (metrics.get("sd_bg") or 0) > 0:
        return {"mu": float(metrics["mu_bg"]), "sd": float(metrics["sd_bg"]), "basis": "mixture"}
    q = (unit.get("summary") or {}).get("quantiles_raw") or {}
    p25, p50, p75 = (to_fit(q.get(k), space) for k in ("p25", "p50", "p75"))
    if p25 is None or p50 is None or p75 is None or p75 <= p25:
        return None
    return {"mu": p50, "sd": (p75 - p25) / 1.349, "basis": "robust"}


def _positive_fraction(unit):
    return float((unit.get("summary") or {}).get("positive_fraction")
                 or (unit.get("metrics") or {}).get("fraction") or 0.0)


def _bg_rms(target, scale, a, b, sd):
    """The rms, in the target's background sds, of the target's background
    quantiles against the scale image's carried through `a + b * q`: the
    percents below both images' positives, at least three of them, else None."""
    space_t, space_s = target.get("fit_space"), scale.get("fit_space")
    qt = (target.get("summary") or {}).get("quantiles_raw") or {}
    qs = (scale.get("summary") or {}).get("quantiles_raw") or {}
    top = 100.0 * (1.0 - max(_positive_fraction(target), _positive_fraction(scale))) - 5.0
    residuals = []
    for p in BG_PERCENTS:
        key = f"p{p:g}"
        if p > top or qt.get(key) is None or qs.get(key) is None:
            continue
        residuals.append(to_fit(qt[key], space_t) - (a + b * to_fit(qs[key], space_s)))
    if len(residuals) < 3:
        return None
    return float(np.sqrt(np.mean(np.square(residuals)))) / sd


def align_bg(target, scale):
    """{a, b, shift_bg_sd, rms_bg, basis}: the target's background as
    `a + b * scale's` (`b` the ratio of the background sds), the shift of
    the background means in the scale image's sds, and how well the
    background quantiles follow the line (`_bg_rms`); None when either
    background cannot be read."""
    bt, bs = background(target), background(scale)
    if bt is None or bs is None or bs["sd"] <= 0 or bt["sd"] <= 0:
        return None
    b = bt["sd"] / bs["sd"]
    a = bt["mu"] - b * bs["mu"]
    rms = _bg_rms(target, scale, a, b, bt["sd"])
    return {"a": float(a), "b": float(b), "shift_bg_sd": float((bt["mu"] - bs["mu"]) / bs["sd"]),
            "rms_bg": None if rms is None else float(rms),
            "basis": bt["basis"] if bt["basis"] == bs["basis"] else f"{bt['basis']}/{bs['basis']}"}


def identity_alignment():
    return {"a": 0.0, "b": 1.0, "shift_bg_sd": 0.0, "rms_bg": 0.0, "basis": "self"}


def mappable(al) -> bool:
    """Whether a gate carries through alignment `al` at all: the contrast
    ratio in range, the background quantiles on the line."""
    lo, hi = GROUP_THRESHOLDS["scale_range"]
    return al is not None and lo <= al["b"] <= hi \
        and (al["rms_bg"] is None or al["rms_bg"] <= THRESHOLDS["change_rms_bg"])


def carry(al, gate_raw, space_scale, space_target):
    """The scale image's gate carried into the target through `al`."""
    return from_fit(al["a"] + al["b"] * to_fit(gate_raw, space_scale), space_target)


def carry_back(al, value_raw, space_target, space_scale):
    """A target value carried onto the scale image's axis (the inverse map)."""
    return from_fit((to_fit(value_raw, space_target) - al["a"]) / al["b"], space_scale)


def classify_group(al, target, scale) -> str:
    """One image's class against its group's scale image (`GROUP_CLASSES`).
    A failed stain only on a hard flag, or a weak signal where the scale
    image's is clear and the target still has two modes; one mode on the
    scale image's background is `no_positives`; anything that does not map
    is a `distribution_change`."""
    t, g = THRESHOLDS, GROUP_THRESHOLDS
    if any(f in FAILED_FLAGS for f in target.get("flags") or ()):
        return "staining_failure"
    if target.get("class") in ("degenerate", "unimodal"):
        lo, hi = g["no_positive_scale"]
        if al is not None and abs(al["shift_bg_sd"]) <= g["no_positive_shift"] \
                and lo <= al["b"] <= hi and (al["rms_bg"] is None
                                             or al["rms_bg"] <= t["change_rms_bg"]):
            return "no_positives"
        return "distribution_change"
    s2b = (target.get("summary") or {}).get("signal_to_background")
    s2b_scale = (scale.get("summary") or {}).get("signal_to_background") or 0.0
    if s2b is not None and s2b < g["failed_s2b"] and s2b_scale >= g["scale_s2b"] \
            and target.get("class") in ("bimodal", "weakly_bimodal"):
        return "staining_failure"
    if not mappable(al):
        return "distribution_change"
    rms = al["rms_bg"] or 0.0
    if abs(al["shift_bg_sd"]) <= t["stable_shift_bg"] and abs(al["b"] - 1.0) <= t["stable_scale"] \
            and rms <= t["drift_rms_bg"]:
        return "stable"
    if abs(al["shift_bg_sd"]) <= g["member_shift"]:
        # Mappable already (rms within `change_rms_bg`): on a small image the
        # background quantiles' own noise is past `drift_rms_bg`.
        return "member_shifted"
    return "image_specific_shift"


#: How a marker is gated across a dataset (`strategy`): one aligned gate, a
#: gate per batch, each image informed by the reference, or each on its own.
STRATEGIES = ("global_aligned", "per_batch", "globally_informed_per_image", "per_image")
GLOBAL_ALIGNED, PER_BATCH, INFORMED_PER_IMAGE, PER_IMAGE = STRATEGIES


def strategy(classes):
    """How a marker should be gated across the dataset, from its images' classes."""
    t = THRESHOLDS
    usable = [c for c in classes if c != "staining_failure"]
    if not usable:
        return PER_IMAGE
    share = {c: usable.count(c) / len(usable) for c in set(usable)}
    if share.get("stable", 0) >= t["stable_share"]:
        return GLOBAL_ALIGNED
    if share.get("distribution_change", 0) >= t["per_image_share"]:
        return PER_IMAGE
    if share.get("batch_effect", 0) > 0:
        return PER_BATCH
    if share.get("smooth_drift", 0) + share.get("stable", 0) >= t["stable_share"]:
        return GLOBAL_ALIGNED
    return INFORMED_PER_IMAGE
