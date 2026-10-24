"""Window errors and tolerances (numpy only; shared by train and evaluate).

Errors are in log canonical space with a small floor, so a black point of
0.001 vs 0.002 (both "almost zero" on screen) is not scored like 0.1 vs 0.2.
"""

from __future__ import annotations

import numpy as np

FLOOR = 1e-3
TOL_MAX = np.log(1.25)  # white point within 25 %
TOL_MIN = np.log(1.5)   # black point within 50 % (above the floor)


def log_err(pred, target, floor=FLOOR):
    return np.abs(np.log((np.asarray(pred, float) + floor) / (np.asarray(target, float) + floor)))


def errors(pmin, pmax, tmin, tmax) -> dict:
    pmin, pmax, tmin, tmax = (np.asarray(v, float) for v in (pmin, pmax, tmin, tmax))
    return {"min": log_err(pmin, tmin), "max": log_err(pmax, tmax),
            "span": log_err(pmax - pmin, tmax - tmin)}


def within(pmin, pmax, tmin, tmax):
    e = errors(pmin, pmax, tmin, tmax)
    return (e["max"] < TOL_MAX) & (e["min"] < TOL_MIN)


def summary(pmin, pmax, tmin, tmax) -> dict:
    e = errors(pmin, pmax, tmin, tmax)
    ok = within(pmin, pmax, tmin, tmax)
    out = {"n": int(len(ok)), "within_tolerance": float(ok.mean()) if len(ok) else None}
    for k, v in e.items():
        out[f"{k}_median_log_err"] = float(np.median(v)) if len(v) else None
        out[f"{k}_mean_log_err"] = float(np.mean(v)) if len(v) else None
    # the white point as a ratio, which is how a person reads it
    out["max_median_ratio_err"] = float(np.exp(np.median(e["max"])) - 1) if len(ok) else None
    return out


def ece(confidence, correct, bins=10) -> float | None:
    confidence, correct = np.asarray(confidence, float), np.asarray(correct, float)
    if not len(confidence):
        return None
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (confidence >= lo) & (confidence < hi if hi < 1 else confidence <= hi)
        if sel.any():
            total += sel.mean() * abs(confidence[sel].mean() - correct[sel].mean())
    return float(total)
