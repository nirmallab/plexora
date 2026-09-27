"""Where a gate could move, decided by code; which of them is right, by eye.

When a look at the cells says "too low", the agent does not type a number.
This proposes a few thresholds in that direction -- steps of half, one and two
standard deviations of the population the gate is moving into, in the fit's
own space -- and the agent picks the one after which the cells that flip look
right. Three guards keep a picture of two dozen cells from dragging a gate
somewhere the whole distribution says it cannot be:

- **The guard band.** Never below one background sd above the background's
  centre (that calls a sixth of the background positive), never above half a
  positive sd past the positive population's centre. A step that would
  overshoot the band's edge is clipped to the edge, so on overlapping markers
  (where the band is narrow) the edge itself is the last candidate offered.
- **Bounded travel.** No candidate more than `MAX_TRAVEL_SD` of the relevant
  population from the GMM gate.
- **Intervals worth looking at.** Two candidates that flip almost no cells
  between them are one candidate; they are merged.

`contradicts` decides when a direction cannot be followed at all. With the
populations clearly apart (Ashman D >= `profile.THRESHOLDS["bimodal_d"]`) the
mixture's means are a real ceiling: a gate already within half a positive sd
of the positive centre cannot be "too low". With them overlapping, the means
say little, and a direction is refused only when the gate is already at the
guard band's edge -- nothing admissible is left that way.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import profile as profmod

#: [cal] step sizes, in sds of the population being moved into.
STEPS = (0.5, 1.0, 2.0)
#: [cal] shares of the cells between the gate and the far limit at which the
#: equal-count steps sit, when the sd steps flip too few cells.
EQUAL_COUNT_SHARES = (0.10, 0.25, 0.50)
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
                         gmm_gate=None, controls=()) -> dict:
    """Up to `k` thresholds in `direction` ("up": the gate is too low; "down":
    too high) from `current_low`, with what each would call positive.

    `controls` are negative controls (`bivariate.negative_control`): each one
    that lies in the direction of travel is offered too, as the step
    `ctrl:<partner>` (clipped to the guard band's edge)."""
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    col = profmod.column(ds, marker)
    fit = profmod.fit_for(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    g = float(col.to_fit(current_low))
    removed = []
    proposals = []
    guard = guard_band(fit)
    sign = 1.0 if direction == "up" else -1.0
    sd_bg, gmm_fit, travel = None, None, None
    # The band's edge in the direction of travel, when the gate is short of it.
    limit = None
    if guard is not None:
        edge = guard[1] if direction == "up" else guard[0]
        limit = edge if (edge - g) * sign > 0 else None
    if fit is not None:
        p = profmod.pools(fit)
        sd_bg = p["sd_bg"]
        sd_into = p["sd_pos"] if direction == "up" else sd_bg
        gmm_fit = float(col.to_fit(gmm_gate)) if gmm_gate is not None else p["gate"]
        travel = MAX_TRAVEL_SD * sd_into
        targets = g + sign * np.asarray(STEPS[:k], dtype=np.float64) * sd_into
        clipped = np.zeros(targets.shape, dtype=bool)
        if limit is not None:
            clipped = (targets - limit) * sign > 0
            targets = np.where(clipped, limit, targets)
        for step, target, at_edge in zip(STEPS[:k], targets, clipped):
            label = f"{step:g}sd->edge" if at_edge else f"{step:g}sd"
            if at_edge and proposals and proposals[-1][2]:
                removed.append({"step": label, "reason": "clipped to the guard band edge",
                                "fit": float(target)})
                continue
            proposals.append((label, float(target), bool(at_edge)))
        space_note = "sd of the population moved into"
    else:
        at = (col.percentile_of(current_low) or 50.0) / 100.0
        for step in QUANTILE_STEPS[:k]:
            q = min(1.0, max(0.0, at + sign * step))
            proposals.append((f"{step:.0%}q",
                              float(profmod._sorted_quantile(col.fit, np.array([q]))[0]),
                              False))
        space_note = "quantile steps (no mixture fit)"

    def guarded(proposals):
        kept = []
        for label, target, at_edge in proposals:
            if guard is not None and not guard[0] - 1e-12 <= target <= guard[1] + 1e-12:
                removed.append({"step": label, "reason": "outside the guard band",
                                "fit": float(target)})
                continue
            # Bounded travel away from the GMM gate only: a gate that starts
            # far from it may always move back toward it.
            if (travel is not None and abs(target - gmm_fit) > travel
                    and abs(target - gmm_fit) > abs(g - gmm_fit)):
                removed.append({"step": label, "reason": "too far from the GMM gate",
                                "fit": float(target)})
                continue
            kept.append((label, target, at_edge))
        return kept

    def with_controls(proposals):
        extra = []
        for control in controls or ():
            target = control.get("p99_fit")
            if target is None or (float(target) - g) * sign <= 0:
                continue
            target, at_edge = float(target), False
            if limit is not None and (target - limit) * sign > 0:
                target, at_edge = float(limit), True
            extra.append((f"ctrl:{control['partner']}", target, at_edge))
        return sorted(list(proposals) + extra, key=lambda p: sign * p[1])

    control_of = {f"ctrl:{c['partner']}": {"partner": c["partner"],
                                           "population": c.get("population"),
                                           "p99": c.get("p99")}
                  for c in controls or () if c.get("partner")}
    proposals = guarded(with_controls(proposals))
    # An empty valley: sd steps that flip no cells say nothing, so the steps
    # are taken by count instead -- the thresholds at which a tenth, a quarter
    # and a half of the cells between the gate and the band's edge have
    # flipped. Every candidate then changes somebody's call. Counted after the
    # guards: a step the band removed is not a step offered.
    n_now = col.n_positive(float(current_low), high)
    viable = sum(1 for _label, target, _edge in proposals
                 if abs(col.n_positive(float(col.from_fit(target)), high) - n_now)
                 >= MIN_FLIP_CELLS)
    if viable < 2 and limit is not None:
        # As far as the band's edge, or the travel bound when that is nearer:
        # every equal-count step is then admissible by construction.
        far = limit
        if travel is not None:
            reach = gmm_fit + sign * max(travel, abs(g - gmm_fit))
            far = min(limit, reach) if sign > 0 else max(limit, reach)
        lo_f, hi_f = sorted((g, far))
        between = col.fit[(col.fit > lo_f) & (col.fit <= hi_f)]
        if between.size >= 3 * MIN_FLIP_CELLS:
            ordered = between if direction == "up" else between[::-1]
            # When the band's edge is what bounds them, it stays the last
            # step: the farthest an overlapping marker's gate may go.
            at_edge = far == limit
            shares = np.asarray(EQUAL_COUNT_SHARES[:k - 1 if at_edge else k])
            index = np.clip(np.round(shares * ordered.size).astype(np.int64) - 1,
                            0, ordered.size - 1)
            steps = [(f"{share:.0%}flip", float(ordered[i]), False)
                     for share, i in zip(shares, index)]
            if at_edge:
                steps.append(("edge", float(limit), True))
            proposals = guarded(with_controls(steps))
            space_note = "equal-count steps (the sd steps flipped too few cells)"

    # Merge intervals that flip almost nothing: walking outward from the
    # current gate, a candidate is kept only if it flips enough cells past the
    # last one kept.
    minimum = max(MIN_FLIP_CELLS, int(MIN_FLIP_FRACTION * max(1, col.n_finite)))
    kept = []
    merged_controls = {}
    last_raw = float(current_low)
    for label, target, at_edge in proposals:
        raw = float(col.from_fit(target))
        flips = abs(col.n_positive(min(last_raw, raw), high)
                    - col.n_positive(max(last_raw, raw), high))
        if flips < minimum and kept:
            removed.append({"step": label, "reason": f"flips only {flips} cells past the "
                                                     "previous candidate",
                            "fit": float(target)})
            if at_edge:
                # Nothing admissible beyond the candidate it merged into.
                kept[-1] = kept[-1][:3] + (True,)
            if label in control_of:
                # The control sits within a few cells of the candidate kept:
                # that candidate IS the control threshold, near enough.
                merged_controls[len(kept) - 1] = control_of[label]
            continue
        if flips == 0:
            removed.append({"step": label, "reason": "flips no cells", "fit": float(target)})
            continue
        kept.append((label, target, raw, at_edge))
        last_raw = raw

    candidates = []
    for index, (label, target, raw, at_edge) in enumerate(kept, start=1):
        n_pos = col.n_positive(raw, high)
        entry = {"id": f"c{index}", "step": label, "low": raw, "fit": float(target),
                 "n_positive": n_pos, "at_edge": at_edge,
                 "fraction": n_pos / col.n_finite if col.n_finite else None}
        if gmm_fit is not None:
            entry["delta_fit"] = float(target - gmm_fit)
            entry["delta_bg_sd"] = float((target - gmm_fit) / sd_bg) if sd_bg else None
        control = control_of.get(label) or merged_controls.get(index - 1)
        if control:
            entry["control"] = control
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
        "reaches_edge": bool(candidates and candidates[-1]["at_edge"]),
        "fit_space": "log1p" if col.to_log else "values",
    }


#: [cal] with a clean mixture, "too low" is refused within this many positive
#: sds of the positive centre, and "too high" within this many background sds
#: of the background centre.
CONTRADICTION_POS_SD = 0.5
CONTRADICTION_BG_SD = 1.0


def contradicts(fit, g_fit, direction, d) -> bool:
    """Whether the whole distribution rules out moving a gate at `g_fit` (fit
    units) in `direction` -- the two regimes of the module docstring."""
    if fit is None:
        return False
    p = profmod.pools(fit)
    if d is not None and d >= profmod.THRESHOLDS["bimodal_d"]:
        if direction == "up":
            return g_fit >= p["mu_pos"] - CONTRADICTION_POS_SD * p["sd_pos"]
        return g_fit <= p["mu_bg"] + CONTRADICTION_BG_SD * p["sd_bg"]
    band = guard_band(fit)
    tolerance = 1e-9 * max(1.0, abs(g_fit))
    if direction == "up":
        return g_fit >= band[1] - tolerance
    return g_fit <= band[0] + tolerance


def inside_guard(ds, marker, low) -> bool | None:
    """Whether a threshold is inside the guard band (None without a fit)."""
    col = profmod.column(ds, marker)
    band = guard_band(profmod.fit_for(ds, marker))
    if band is None:
        return None
    g = float(col.to_fit(low))
    return bool(band[0] - 1e-9 <= g <= band[1] + 1e-9)
