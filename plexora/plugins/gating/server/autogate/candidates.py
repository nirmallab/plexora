"""Where a gate could move, decided by code; which of them is right, by eye.

When a look at the cells says "too low", the agent does not type a number.
This proposes a few thresholds in that direction -- steps of half, one and two
standard deviations of the population the gate is moving into, in the fit's
own space -- and the agent picks the one after which the cells that flip look
right. Three guards keep a picture of two dozen cells from dragging a gate
somewhere the whole distribution says it cannot be:

- **The guard band.** Never below one background sd above the background's
  centre (that calls a sixth of the background positive), never above half a
  positive sd past the positive population's centre.
- **Bounded travel.** No candidate more than `MAX_TRAVEL_SD` of the relevant
  population from the GMM gate.
- **Intervals worth looking at.** Two candidates that flip almost no cells
  between them are one candidate; they are merged.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import profile as profmod

#: [cal] step sizes, in sds of the population being moved into.
STEPS = (0.5, 1.0, 2.0)
#: [cal] quantile steps when there is no fit.
QUANTILE_STEPS = (0.02, 0.05, 0.10)
#: [cal] the guard band, in each population's own sds.
GUARD_BG_SD = 1.0
GUARD_POS_SD = 0.5
#: [cal] farthest a candidate may be from the GMM gate.
MAX_TRAVEL_SD = 2.5
#: Intervals flipping fewer cells than this are merged.
MIN_FLIP_CELLS = 20
MIN_FLIP_FRACTION = 0.001


def guard_band(fit):
    """(low, high) in fit units, or None without a fit."""
    if fit is None:
        return None
    p = profmod.pools(fit)
    low = p["mu_bg"] + GUARD_BG_SD * p["sd_bg"]
    high = p["mu_pos"] + GUARD_POS_SD * p["sd_pos"]
    if high <= low:
        high = low + 1e-6
    return float(low), float(high)


def candidate_thresholds(ds, marker, *, current_low, direction, high=None, k=3,
                         gmm_gate=None) -> dict:
    """Up to `k` thresholds in `direction` ("up": the gate is too low; "down":
    too high) from `current_low`, with what each would call positive."""
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    col = profmod.column(ds, marker)
    fit = model.fit_for(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    g = float(col.to_fit(current_low))
    removed = []
    proposals = []
    guard = guard_band(fit)
    sign = 1.0 if direction == "up" else -1.0
    sd_bg, gmm_fit, travel = None, None, None
    if fit is not None:
        p = profmod.pools(fit)
        sd_bg = p["sd_bg"]
        sd_into = p["sd_pos"] if direction == "up" else sd_bg
        gmm_fit = float(col.to_fit(gmm_gate)) if gmm_gate is not None else p["gate"]
        travel = MAX_TRAVEL_SD * sd_into
        proposals = [(f"{step:g}sd", g + sign * step * sd_into) for step in STEPS[:k]]
        space_note = "sd of the population moved into"
    else:
        at = (col.percentile_of(current_low) or 50.0) / 100.0
        for step in QUANTILE_STEPS[:k]:
            q = min(1.0, max(0.0, at + sign * step))
            proposals.append((f"{step:.0%}q",
                              float(profmod._sorted_quantile(col.fit, np.array([q]))[0])))
        space_note = "quantile steps (no mixture fit)"

    # An empty valley: sd steps that flip no cells say nothing, so the steps
    # are taken by count instead -- the thresholds at which a tenth, a quarter
    # and a half of the cells between the gate and the far limit have flipped.
    # Every candidate then changes somebody's call.
    limit = None
    if guard is not None:
        limit = guard[1] if direction == "up" else guard[0]
    viable = sum(1 for _label, target in proposals
                 if abs(col.n_positive(float(col.from_fit(target)), high)
                        - col.n_positive(float(current_low), high)) >= MIN_FLIP_CELLS)
    if viable < 2 and limit is not None and (limit - g) * sign > 0:
        lo_f, hi_f = sorted((g, limit))
        between = col.fit[(col.fit > lo_f) & (col.fit <= hi_f)]
        if between.size >= 3 * MIN_FLIP_CELLS:
            ordered = between if direction == "up" else between[::-1]
            proposals = []
            for share in (0.10, 0.25, 0.50)[:k]:
                index = min(ordered.size - 1, max(0, int(round(share * ordered.size)) - 1))
                proposals.append((f"{share:.0%}flip", float(ordered[index])))
            space_note = "equal-count steps (the sd steps flipped too few cells)"

    # The guards, whichever way the steps were made.
    guarded = []
    for label, target in proposals:
        if guard is not None and not guard[0] <= target <= guard[1]:
            removed.append({"step": label, "reason": "outside the guard band",
                            "fit": float(target)})
            continue
        # Bounded travel away from the GMM gate only: a gate that starts far
        # from it may always move back toward it.
        if (travel is not None and abs(target - gmm_fit) > travel
                and abs(target - gmm_fit) > abs(g - gmm_fit)):
            removed.append({"step": label, "reason": "too far from the GMM gate",
                            "fit": float(target)})
            continue
        guarded.append((label, target))
    proposals = guarded

    # Merge intervals that flip almost nothing: walking outward from the
    # current gate, a candidate is kept only if it flips enough cells past the
    # last one kept.
    minimum = max(MIN_FLIP_CELLS, int(MIN_FLIP_FRACTION * max(1, col.n_finite)))
    kept = []
    last_raw = float(current_low)
    for label, target in proposals:
        raw = float(col.from_fit(target))
        flips = abs(col.n_positive(min(last_raw, raw), high)
                    - col.n_positive(max(last_raw, raw), high))
        if flips < minimum and kept:
            removed.append({"step": label, "reason": f"flips only {flips} cells past the "
                                                     "previous candidate",
                            "fit": float(target)})
            continue
        if flips == 0:
            removed.append({"step": label, "reason": "flips no cells", "fit": float(target)})
            continue
        kept.append((label, target, raw))
        last_raw = raw

    candidates = []
    for index, (label, target, raw) in enumerate(kept, start=1):
        n_pos = col.n_positive(raw, high)
        entry = {"id": f"c{index}", "step": label, "low": raw, "fit": float(target),
                 "n_positive": n_pos,
                 "fraction": n_pos / col.n_finite if col.n_finite else None}
        if gmm_fit is not None:
            entry["delta_fit"] = float(target - gmm_fit)
            entry["delta_bg_sd"] = float((target - gmm_fit) / sd_bg) if sd_bg else None
        candidates.append(entry)
    ordered = sorted([float(current_low)] + [c["low"] for c in candidates])
    intervals = [{"from": lo, "to": hi,
                  "n_flip": col.n_positive(lo, high) - col.n_positive(hi, high)}
                 for lo, hi in zip(ordered[:-1], ordered[1:])]
    return {
        "marker": marker, "direction": direction, "current": float(current_low),
        "current_fit": g, "high": high, "candidates": candidates, "removed": removed,
        "guard": ({"low": float(col.from_fit(guard[0])), "high": float(col.from_fit(guard[1])),
                   "low_fit": guard[0], "high_fit": guard[1]} if guard else None),
        "gmm_gate": float(col.from_fit(gmm_fit)) if gmm_fit is not None else None,
        "intervals": intervals, "steps": space_note,
        "fit_space": "log1p" if col.to_log else "values",
    }


def inside_guard(ds, marker, low) -> bool | None:
    """Whether a threshold is inside the guard band (None without a fit)."""
    col = profmod.column(ds, marker)
    band = guard_band(model.fit_for(ds, marker))
    if band is None:
        return None
    g = float(col.to_fit(low))
    return bool(band[0] - 1e-9 <= g <= band[1] + 1e-9)
