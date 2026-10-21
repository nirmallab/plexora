"""Which look comes next, and when to stop: decided by code.

A small closed set of actions, each a look at cells nobody has shown yet:

- `survey` -- the first look: cells spread over every disputed interval of
  the surveyed range, more from the intervals holding more cells.
- `refine` -- cells between the leader and the rival whose dispute holds the
  most cells (the cells whose call that choice changes) among the rivals
  that block resolving: not yet beaten, and farther than `resolve_within`.
- `extend` -- the leader sits at the edge of the surveyed range and the
  lattice has a point beyond it: that interval joins the range and is looked
  at, so a gate past every estimator is reachable, by a lattice point.

Every look is new evidence by construction: a cell is shown once per marker,
and an action whose intervals hold no unshown cell is refused. Stopping is
by rule (`decide`): resolved when the leader beats every rival farther than
`resolve_within`; otherwise review, for a stated reason, at the leader --
when the look budget is spent, when no new cell can separate the leader
from a rival, when the negative sentinels were read stained twice, or when
most cells could not be read. Running out of looks never counts as resolved.

The first look gives every interval between candidates an equal share of
its cells; later looks spread theirs in proportion to the cells in dispute.
"""

from __future__ import annotations

import math

from plexora.plugins.gating.server.autogate.hypotheses import evidence

#: Terminal statuses; all but `resolved` close the marker for review at the
#: leader (or the start, when the reader failed the sentinels).
STATUSES = ("resolved", "budget_exhausted", "unresolved_evidence", "readers_unreliable",
            "unreadable", "no_identifiable_boundary")

REASONS = {
    "budget_exhausted": "the looks allowed were spent before the leading gate beat every "
                        "rival",
    "unresolved_evidence": "no cell left unshown could separate the leading gate from a rival",
    "readers_unreliable": "the reader called clearly negative cells stained, twice",
    "unreadable": "most cells could not be read (uncertain or artifact)",
    "no_identifiable_boundary": "the stain rises with the value without a boundary the "
                                "looks could find (a continuous marker: an operational "
                                "cutoff)",
}


def window(state) -> tuple:
    """(bounds, n_cells) of the surveyed range."""
    pts = state["points"]
    lo, hi = state["window"]
    bounds = [p["low"] for p in pts[lo:hi + 1]]
    n_pos = [p["n_positive"] for p in pts[lo:hi + 1]]
    return bounds, [max(0, a - b) for a, b in zip(n_pos[:-1], n_pos[1:])]


def shown(state) -> list:
    """Cells already shown per interval of the window (sentinels apart)."""
    bounds, n_cells = window(state)
    out = [0] * len(n_cells)
    for obs in state["observations"]:
        for cell in obs.get("cells") or ():
            if cell.get("sentinel"):
                continue
            k = evidence.interval_of(float(cell["value"]), bounds)
            if k is not None:
                out[k] += 1
    return out


def allocate(weights, available, total) -> list:
    """`total` picks over intervals in proportion to `weights`, at most
    `available` each, largest remainders first."""
    share = [float(w) if a > 0 else 0.0 for w, a in zip(weights, available)]
    picks = [0] * len(weights)
    left = total
    while left > 0:
        room = [i for i, a in enumerate(available) if picks[i] < a and share[i] > 0]
        if not room:
            break
        mass = sum(share[i] for i in room)
        want = {i: share[i] / mass * left for i in room}
        placed = 0
        for i in sorted(room, key=lambda i: -(want[i] - math.floor(want[i]))):
            take = min(available[i] - picks[i], max(1 if placed < left else 0,
                                                    int(math.floor(want[i]))))
            take = min(take, left - placed)
            picks[i] += take
            placed += take
            if placed >= left:
                break
        if not placed:
            break
        left -= placed
    return picks


def support(state, to_fit, params) -> dict | None:
    valid = [o for o in state["observations"] if o.get("valid", True)]
    if not valid:
        return None
    bounds, n_cells = window(state)
    lo = state["window"][0]
    prior = min(max(state["prior"] - lo, 0), len(bounds) - 1)
    return evidence.support(bounds, n_cells, valid, prior=prior, to_fit=to_fit,
                            tolerance=params["tolerance"],
                            resolve_within=params.get("resolve_within"),
                            min_between=int(params.get("min_between") or 0))


def _plan(state, intervals, picks, kind, **extra) -> dict | None:
    chosen = [{"interval": k, "n": n} for k, n in zip(intervals, picks) if n > 0]
    if not chosen:
        return None
    bounds, _ = window(state)
    return {"kind": kind, **extra,
            "intervals": [{"interval": c["interval"], "from": bounds[c["interval"]],
                           "to": bounds[c["interval"] + 1], "n": c["n"]} for c in chosen]}


def next_action(state, to_fit, params) -> dict | None:
    """The next look's plan, or None when no new cell could change the
    choice."""
    bounds, n_cells = window(state)
    seen = shown(state)
    available = [max(0, n - s) for n, s in zip(n_cells, seen)]
    total = int(params["panel_cells"])
    s = support(state, to_fit, params)
    if s is None:
        # An equal share for every interval between candidates: by cell
        # count the crowded background takes the look, by width the long
        # bright tail does, and neither is where the stain starts.
        ks = list(range(len(n_cells)))
        return _plan(state, ks, allocate([1.0] * len(ks), available, total), "survey")
    leader = s["leader"]
    lo, hi = state["window"]
    if leader == 0 and lo > 0:
        return {"kind": "extend", "side": "down"}
    if leader == len(bounds) - 1 and hi < len(state["points"]) - 1:
        return {"kind": "extend", "side": "up"}
    # Only a rival that keeps the marker from resolving is worth a look; the
    # cells between it and the leader are drawn in proportion to how many
    # each interval holds, so their pooled share estimates the cells the
    # choice changes.
    for rival in sorted(s["blocking"], key=lambda r: (-r["n_between"], r["distance"])):
        a, b = sorted((leader, rival["index"]))
        ks = list(range(a, b))
        picks = allocate([n_cells[k] for k in ks], [available[k] for k in ks], total)
        plan = _plan(state, ks, picks, "refine", rival=rival["low"])
        if plan is not None:
            return plan
    return None


def extend(state, side) -> dict:
    """Widen the window by one lattice point; the plan samples the new
    interval (as many cells as a look holds)."""
    lo, hi = state["window"]
    if side == "down":
        state["window"] = [lo - 1, hi]
        return {"interval": 0}
    state["window"] = [lo, hi + 1]
    return {"interval": hi + 1 - lo - 1}


def decide(state, to_fit, params, *, continuous=False) -> tuple:
    """(status or None to keep looking, support)."""
    s = support(state, to_fit, params)
    invalid = sum(1 for o in state["observations"] if not o.get("valid", True))
    if invalid >= int(params["invalid_looks"]):
        return "readers_unreliable", s
    if s is None:
        return None, s
    lo, hi = state["window"]
    at_edge = (s["leader"] == 0 and lo > 0) or \
        (s["leader"] == hi - lo and hi < len(state["points"]) - 1)
    if s["resolved"] and not at_edge:
        # A leader at the edge of the surveyed range says the gate is there or
        # beyond it: the next lattice point is looked at first.
        return "resolved", s
    if s["labelled"] >= params["min_labelled"] \
            and (s["other_share"] or 0) > params["max_other_share"]:
        return "unreadable", s
    if len(state["observations"]) >= int(params["looks_max"]):
        return ("no_identifiable_boundary" if continuous else "budget_exhausted"), s
    return None, s
