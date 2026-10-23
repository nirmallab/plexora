"""Confidence as a calibrated probability: P(|gate - person's gate| <= 0.2 | evidence).

Until a table is fitted and frozen here (`TABLE_PATH`), the panel path's
confidence is the rule in `hypotheses.confidence`, tagged `uncalibrated`.
A table is fitted by `tools/bench_gates/calibrate.py` on dev session records
scored against a person's gates, never on the held-out images, and is
read-only at run time: nothing tunes itself.

The model is deliberately small -- logistic regression over at most five
features computed from what a session record already keeps
(`unit["hypotheses"]`), L2-shrunk, standardised -- because there are only
about a hundred dev marker-images with a person's gates and a richer model
would learn the LSP images' convention rather than the evidence. It is
checked on held-out records by reliability (predicted vs observed, by
bins) and coverage (the share accepted, and their error).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

#: Bumped whenever the features or the fit change meaning ("2": the
#: posterior's features, fitted on review units too).
VERSION = "2"

#: The frozen table, when one has been fitted (absent: uncalibrated).
TABLE_PATH = Path(__file__).with_name("calibration.json")

#: The features a table is fitted on by default, in order (at most five:
#: about a hundred dev marker-images carry a person's gates). A table names
#: its own (`fit(features=...)`), chosen from `CANDIDATE_FEATURES` on dev.
FEATURES = ("p_within", "plateau_width", "labelled", "uncertain_share", "looks")

#: Everything `features` computes, for the choice on dev records.
CANDIDATE_FEATURES = ("p_within", "plateau_width", "max_p", "labelled", "uncertain_share",
                      "looks", "fpr", "fnr", "margin", "spread")

#: [cal] probabilities at which a gate is `high` / `moderate`; below,
#: `low`. Pre-registered with the plan, before any table exists.
LEVELS = (("high", 0.8), ("moderate", 0.6))

#: Within this of the person's gate a gate counts as right (their retest
#: noise).
TOLERANCE = 0.2


def features(state, names=None) -> dict | None:
    """The calibration features of one unit's hypotheses state, or None
    when it has no observation (or lacks one of `names`, when given).

    - `p_within`: P(the best gate lies within the tolerance of the gate
      written) under the posterior over candidates; `plateau_width`: the
      plateau's span in tolerance units; `max_p`: the most probable
      candidate's P (posterior records only);
    - `margin`: how clearly the leader beat its closest rival outside
      `resolve_within` (the Wilson bound's distance from one half; negative
      when a rival blocks);
    - `spread`: how far the estimators' clusters lie apart (fit units);
    - `labelled`: cells read either way; `uncertain_share` (also as
      `other_share`): cells read `uncertain` or `artifact`; `looks`: panels
      shown;
    - `fpr` / `fnr`: the sentinels' miss rates (0 when none were shown)."""
    support = state.get("support") or {}
    observations = state.get("observations") or []
    if not observations or not support:
        return None
    params = state.get("params") or {}
    within = params.get("resolve_within", TOLERANCE)
    margins = []
    for rival in support.get("rivals") or ():
        if rival["distance"] <= within + 1e-9:
            continue
        low, high = rival["ci"]
        side = low - 0.5 if rival["low"] > (state.get("leader") or 0.0) else 0.5 - high
        margins.append(side)
    labelled = float(support.get("labelled") or 0)
    other = float(support.get("other_share") or 0.0)
    reader = state.get("reader") or {}
    row = {"margin": min(margins) if margins else 0.5,
           "spread": float(state.get("spread") or 0.0),
           "labelled": labelled, "other_share": other, "uncertain_share": other,
           "looks": float(len(observations)),
           "fpr": float(reader.get("fpr") or 0.0), "fnr": float(reader.get("fnr") or 0.0)}
    posterior = support.get("posterior")
    if posterior:
        fits = [m.get("fit", m["low"]) for m in posterior.get("plateau") or ()]
        tolerance = float(params.get("tolerance") or TOLERANCE)
        width = (max(fits) - min(fits)) if fits else 0.0
        row.update(p_within=float(posterior.get("within") or 0.0),
                   plateau_width=width / tolerance, max_p=float(posterior.get("max_p") or 0.0))
    names = tuple(names or FEATURES)
    return row if all(n in row for n in names) else None


def _matrix(rows, names):
    return [[float(r[name]) for name in names] for r in rows]


def fit(rows, outcomes, *, l2=1.0, iterations=50, features=FEATURES) -> dict:
    """A table from feature rows and 0/1 outcomes (within the tolerance):
    standardised logistic regression by Newton steps with an L2 penalty on
    the slopes."""
    import numpy as np

    names = tuple(features)
    x = np.asarray(_matrix(rows, names), dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if x.shape[0] < len(names) + 2 or len(set(y.tolist())) < 2:
        raise ValueError("too few records, or every outcome the same, to fit a calibration")
    mean, sd = x.mean(axis=0), x.std(axis=0)
    sd[sd == 0] = 1.0
    z = np.hstack([np.ones((x.shape[0], 1)), (x - mean) / sd])
    w = np.zeros(z.shape[1])
    penalty = np.diag([0.0] + [l2] * len(names))
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-z @ w))
        gradient = z.T @ (p - y) + penalty @ w
        hessian = (z * (p * (1 - p))[:, None]).T @ z + penalty
        step = np.linalg.solve(hessian, gradient)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return {"version": VERSION, "features": list(names), "mean": mean.tolist(),
            "sd": sd.tolist(), "weights": w.tolist(), "n": int(x.shape[0]),
            "tolerance": TOLERANCE}


def predict(table, row) -> float:
    z = [1.0] + [(float(row[name]) - m) / s for name, m, s in
                 zip(table["features"], table["mean"], table["sd"])]
    score = sum(a * b for a, b in zip(table["weights"], z))
    return 1.0 / (1.0 + math.exp(-score))


def reliability(table, rows, outcomes, bins=5) -> list:
    """[{low, high, n, predicted, observed}] over equal-width bins of the
    predicted probability."""
    pairs = [(predict(table, r), float(o)) for r, o in zip(rows, outcomes)]
    out = []
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        inside = [(p, o) for p, o in pairs if low <= p < high or (b == bins - 1 and p == 1.0)]
        if inside:
            out.append({"low": low, "high": high, "n": len(inside),
                        "predicted": sum(p for p, _ in inside) / len(inside),
                        "observed": sum(o for _, o in inside) / len(inside)})
    return out


def load(path=None) -> dict | None:
    """The frozen table, or None (uncalibrated)."""
    path = Path(path or TABLE_PATH)
    if not path.exists():
        return None
    table = json.loads(path.read_text(encoding="utf-8"))
    return table if table.get("version") == VERSION else None


def level(probability) -> str:
    for name, at in LEVELS:
        if probability >= at:
            return name
    return "low"
