"""The candidate gates a marker's looks choose among: competing hypotheses.

They are the points of the unit's frozen lattice (`lattice.build`): the
mixture's split, KDE, Otsu, the onset and ceiling of the background, the
scored proposal, partner-derived and negative-control points, and the steps
around the start, merged and snapped to the sidebar's grid. On the held-out
images the best lattice point lay within 0.2 of the user's gate for 93 % of
markers (`tools/bench_gates/score_gap.py --oracle`): the right gate is
almost always among them, and the looks' job is to say which.

Estimators that read the same column are not independent votes. They are
grouped into clusters (single linkage within the tolerance) to describe how
far they disagree, never averaged. The range the looks survey runs from the
lowest to the highest estimator, plus one lattice point beyond each end, so
the extremes can be refuted too; the cells between neighbouring points are
the disputed intervals.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import lattice as latmod

#: Lattice families that estimate a gate (the rest are steps and edges).
ESTIMATORS = ("gmm", "kde", "otsu", "gmm2", "onset", "ceiling", "score", "bio", "ctrl",
              "within")


def _families(point) -> set:
    return {latmod.family(s) for s in point.get("sources") or ()}


def is_estimate(point) -> bool:
    return bool(_families(point) & set(ESTIMATORS))


def clusters(points, to_fit, tolerance) -> list:
    """Estimator points grouped by single linkage within `tolerance` (fit
    units): [{lows, centre, sources}] in rising order."""
    est = [p for p in points if is_estimate(p)]
    groups = []
    for p in est:
        if groups and float(to_fit(p["low"])) - float(to_fit(groups[-1][-1]["low"])) \
                <= tolerance + 1e-9:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for group in groups:
        lows = [float(p["low"]) for p in group]
        out.append({"lows": lows, "centre": lows[len(lows) // 2],
                    "sources": sorted(s for p in group for s in p["sources"])})
    return out


def thin(points, to_fit, min_gap) -> list:
    """`points` (sorted by value) with neighbours closer than `min_gap` (fit
    units) merged into one: the estimator among them when there is one,
    else the first. A survey over slivers a few hundredths apart spends its
    cells on intervals no reader can tell apart."""
    kept = []
    for p in points:
        if kept and float(to_fit(p["low"])) - float(to_fit(kept[-1]["low"])) < min_gap:
            if is_estimate(p) and not is_estimate(kept[-1]):
                kept[-1] = p
            continue
        kept.append(p)
    return kept


def build(lattice, start, to_fit, tolerance, min_gap=0.0) -> dict:
    """The candidates (`bounds`, sorted table values, with their lattice ids
    and positive counts), the cells in each interval between them, the
    estimator clusters, and the index of the start (the prior a tie goes to).
    """
    points = sorted(lattice.get("points") or [], key=lambda p: p["low"])
    if min_gap:
        points = thin(points, to_fit, min_gap)
    if not points:
        return {"bounds": [], "n_cells": [], "clusters": [], "prior": None, "window": None}
    est = [i for i, p in enumerate(points) if is_estimate(p)]
    if start is not None:
        nearest = min(range(len(points)), key=lambda i: abs(points[i]["low"] - float(start)))
        est.append(nearest)
    if not est:
        est = list(range(len(points)))
    lo, hi = max(0, min(est) - 1), min(len(points) - 1, max(est) + 1)
    if hi == lo:
        lo, hi = max(0, lo - 1), min(len(points) - 1, hi + 1)
    chosen = points[lo:hi + 1]
    bounds = [float(p["low"]) for p in chosen]
    n_pos = [int(p["n_positive"]) for p in chosen]
    n_cells = [max(0, a - b) for a, b in zip(n_pos[:-1], n_pos[1:])]
    prior = lo
    if start is not None:
        prior = lo + min(range(len(bounds)), key=lambda i: abs(bounds[i] - float(start)))
    return {"bounds": bounds, "n_cells": n_cells, "window": [lo, hi],
            "ids": [p["id"] for p in chosen],
            "sources": [list(p["sources"]) for p in chosen],
            "n_positive": n_pos, "prior": prior,
            "clusters": clusters(chosen, to_fit, tolerance),
            "fingerprint": lattice.get("fingerprint")}


def spread(cands, to_fit) -> float | None:
    """How far the estimator clusters lie apart (fit units): 0 for one."""
    centres = [float(to_fit(c["centre"])) for c in cands.get("clusters") or ()]
    if not centres:
        return None
    return max(centres) - min(centres)
