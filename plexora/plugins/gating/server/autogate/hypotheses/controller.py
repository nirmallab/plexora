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
- `tail` -- a rare marker (`state["rare"]`) whose leader would resolve:
  cells above the leader, up to the brightest, in proportion to where they
  lie. A rare population is never accepted without its own cells having
  been read as stained (`tail_status`), and never declared absent by
  panels either: read unstained, it closes for review.

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
            "unreadable", "no_identifiable_boundary", "positives_not_confirmed")

REASONS = {
    "budget_exhausted": "the looks allowed were spent before the leading gate beat every "
                        "rival",
    "unresolved_evidence": "no cell left unshown could separate the leading gate from a rival",
    "readers_unreliable": "the reader called clearly negative cells stained, twice",
    "unreadable": "most cells could not be read (uncertain or artifact)",
    "no_identifiable_boundary": "the stain rises with the value without a boundary the "
                                "looks could find (a continuous marker: an operational "
                                "cutoff)",
    "positives_not_confirmed": "a rare marker whose cells above the leading gate were read "
                               "as not carrying the stain: whether this image has a positive "
                               "population is for a person to say",
}


def window(state) -> tuple:
    """(bounds, n_cells) of the surveyed range."""
    pts = state["points"]
    lo, hi = state["window"]
    bounds = [p["low"] for p in pts[lo:hi + 1]]
    n_pos = [p["n_positive"] for p in pts[lo:hi + 1]]
    return bounds, [max(0, a - b) for a, b in zip(n_pos[:-1], n_pos[1:])]


def above(state) -> int:
    """Cells above the surveyed range's highest point."""
    return int(state["points"][state["window"][1]]["n_positive"])


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
    check = state.get("classifier") or {}
    # A classifier check's picks are read per classifier call
    # (`classifier_check`), over the range its strata were counted on.
    stratified = check.get("strata") is not None and check.get("window") == state["window"]
    return evidence.support(bounds, n_cells, valid, prior=prior, to_fit=to_fit,
                            tolerance=params["tolerance"],
                            resolve_within=params.get("resolve_within"),
                            min_between=int(params.get("min_between") or 0),
                            min_between_share=float(params.get("min_between_share") or 0.0),
                            n_above=above(state),
                            calls=check.get("calls") if stratified else None,
                            strata=check.get("strata") if stratified else None)


def tail(state, s) -> dict:
    """The cells above the leader: {own, read} among those read in valid
    looks, and how many are still unshown (`available`)."""
    low = float(s["low"])
    own = read = shown_above = 0
    for obs in state["observations"]:
        for cell in obs.get("cells") or ():
            if cell.get("sentinel") or float(cell["value"]) <= low:
                continue
            shown_above += 1
            if not obs.get("valid", True):
                continue
            if cell.get("label") == evidence.OWN:
                own, read = own + 1, read + 1
            elif cell.get("label") == evidence.NOT_OWN:
                read += 1
    return {"own": own, "read": read,
            "available": max(0, int(s["positives"]) - shown_above)}


def tail_status(state, s) -> str:
    """`confirmed` when the cells above the leader are read stained (a
    Wilson interval above one half, or a majority once every one was
    shown), `refuted` the other way round, `open` while more could be
    read."""
    t = tail(state, s)
    if not t["read"]:
        return "refuted" if not t["available"] else "open"
    low, high = evidence.wilson(t["own"], t["read"])
    if low > 0.5:
        return "confirmed"
    if high < 0.5:
        return "refuted"
    if not t["available"]:
        return "confirmed" if t["own"] * 2 > t["read"] else "refuted"
    return "open"


def _tail_plan(state, s, total) -> dict | None:
    """Cells above the leader, each interval (and the cells above the
    range) given its share of the look by the cells it holds."""
    bounds, n_cells = window(state)
    seen = shown(state)
    leader = s["leader"]
    ks = list(range(leader, len(n_cells)))
    available = [max(0, n_cells[k] - seen[k]) for k in ks]
    top_shown = sum(1 for o in state["observations"] for c in o.get("cells") or ()
                    if not c.get("sentinel") and float(c["value"]) > bounds[-1])
    available.append(max(0, above(state) - top_shown))
    picks = allocate([float(a) for a in available], available, total)
    intervals = [{"interval": k, "from": bounds[k], "to": bounds[k + 1], "n": n}
                 for k, n in zip(ks, picks) if n > 0]
    if picks[-1] > 0:
        intervals.append({"interval": None, "from": bounds[-1], "to": float(state["top"]),
                          "n": picks[-1]})
    return {"kind": "tail", "intervals": intervals} if intervals else None


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
    if state.get("rare") and not s["blocking"] and tail_status(state, s) == "open":
        return _tail_plan(state, s, total)
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
        if not state.get("rare"):
            return "resolved", s
        # A rare marker's leader stands only on its own cells read stained.
        status = tail_status(state, s)
        if status == "confirmed":
            return "resolved", s
        if status == "refuted":
            return "positives_not_confirmed", s
    if s["labelled"] >= params["min_labelled"] \
            and (s["other_share"] or 0) > params["max_other_share"]:
        return "unreadable", s
    if len(state["observations"]) >= int(params["looks_max"]):
        return ("no_identifiable_boundary" if continuous else "budget_exhausted"), s
    return None, s
