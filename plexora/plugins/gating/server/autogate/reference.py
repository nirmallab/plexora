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
