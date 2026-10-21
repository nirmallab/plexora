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
- `step` -- the leader sits on the lattice's own highest or lowest point:
  a point `edge_step` beyond it (fit units, snapped like a lattice point)
  joins the range and its cells are looked at, at most `edge_steps` times
  a side, once the leader beats its rivals; a leader is never resolved on
  the lattice's edge while a step could still find cells there
  (lsp11385_2 CD68, 2026-10-21: every cell shown below the top point read
  unstained, and the person's gate lay above it).
- `split` -- an interval beside the leader is wider than `split_width`
  (fit units) and its cells are mixed (an own-stain share inside
  `split_share`, on `split_reads` definite reads) or ordered (its stained
  reads above its unstained ones, `ordered`): it is cut into parts of
  about the tolerance and looked at, once (CD163: no candidate between
  6.70 and 7.69, the person's gate 7.31). A marker out of looks may still
  make that cut on the reads in hand, without a look, before it closes
  for review (`split_at_close`) -- on ordered reads only: with no new look
  to come, a mixed share could be the reader's slips.
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

Under the `posterior` decision (VERSION 6, `evidence.support`) the later
looks are allocated by what they could change (`sampling="voi"`,
`voi_plan`): per interval, the expected rise of the best P(within the
tolerance) after reading more of its cells, with a cell for every interval
never shown and a cap per interval. When a whole look could not raise it
by `voi_epsilon`, the marker closes for review (`looks_uninformative`)
instead of spending its last look. A point a step or a split added may lead
only once read on both sides (`eligible`). Cells read where a brighter
neighbour crowds them are weighed apart from isolated ones by the cells
each kind holds (`crowding`), and a marker most of whose crowded cells
could not be read may still resolve on its isolated cells alone
(`isolated`), flagged so.
"""

from __future__ import annotations

import math

from plexora.plugins.gating.server.autogate.hypotheses import evidence

#: Terminal statuses; all but `resolved` close the marker for review at the
#: leader (or the start, when the reader failed the sentinels).
STATUSES = ("resolved", "budget_exhausted", "unresolved_evidence", "readers_unreliable",
            "unreadable", "no_identifiable_boundary", "positives_not_confirmed",
            "looks_uninformative")

REASONS = {
    "budget_exhausted": "the looks allowed were spent before the leading gate beat every "
                        "rival",
    "unresolved_evidence": "no cell left unshown could separate the leading gate from a rival",
    "looks_uninformative": "another look could not make any candidate gate clearly surer: "
                           "the reads so far leave several gates about as likely",
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


def _valid(state) -> list:
    return [o for o in state["observations"] if o.get("valid", True)]


def eligible(state, params) -> list | None:
    """Per bound of the range: whether that candidate may lead. A point a
    step or a split added (`added_points`) may lead only once each side of
    it inside the range has `added_min_reads` cells read either way, or has
    had every cell shown; until then it is a hypothesis the next look reads.
    lsp11385_2, three runs (2026-10-21): the six leaders on added points,
    decided by two to six reads, carried the worst spreads (CD163 7.19 /
    7.69 / 7.94 against the person's 7.31). None when every candidate may."""
    least = int(params.get("added_min_reads") or 0)
    added = set(state.get("added_points") or ())
    if not least or not added:
        return None
    bounds, n_cells = window(state)
    lo = state["window"][0]
    table = evidence.counts(bounds, _valid(state))
    seen = shown(state)
    out = []
    for j in range(len(bounds)):
        if state["points"][lo + j]["id"] not in added:
            out.append(True)
            continue
        sides = [k for k in (j - 1, j) if 0 <= k < len(n_cells) and n_cells[k] > 0]
        out.append(all(table[k]["own"] + table[k]["not"] >= least or seen[k] >= n_cells[k]
                       for k in sides))
    return out if any(out) else None


def crowding(state, params) -> tuple:
    """(cell field, cells per interval by it) to weigh the reads by -- a
    brighter neighbour crowding the cell (1) or not (0), recorded by the
    look that showed it (`panel.panel_cells`) -- or (None, None): not asked
    for, counted over another range, or either kind read fewer than
    `strata_min_reads` times. Uncertain reads gather on crowded cells
    (lsp11385_2, 2026-10-21: about 40 % uncertain in crowded fields), so
    pooled reads speak for the isolated cells; weighed by kind, each
    speaks for the cells it stands for."""
    info = state.get("crowding")
    if params.get("strata") != "crowding" or not info:
        return None, None
    bounds, _ = window(state)
    if [round(float(b), 6) for b in info.get("bounds") or ()] != \
            [round(float(b), 6) for b in bounds]:
        return None, None
    reads = {"0": 0, "1": 0}
    for obs in _valid(state):
        for cell in obs.get("cells") or ():
            if cell.get("sentinel") or cell.get("crowded") is None or \
                    cell.get("label") not in (evidence.OWN, evidence.NOT_OWN) or \
                    evidence.interval_of(float(cell["value"]), bounds) is None:
                continue
            reads[str(int(cell["crowded"]))] += 1
    if min(reads.values()) < int(params.get("strata_min_reads") or 0):
        return None, None
    return "crowded", info["sizes"]


def support(state, to_fit, params) -> dict | None:
    valid = _valid(state)
    if not valid:
        return None
    bounds, n_cells = window(state)
    lo = state["window"][0]
    prior = min(max(state["prior"] - lo, 0), len(bounds) - 1)
    check = state.get("classifier") or {}
    # A classifier check's picks are read per classifier call
    # (`classifier_check`), over the range its strata were counted on.
    stratified = check.get("strata") is not None and check.get("window") == state["window"]
    calls, strata = (check.get("calls"), check.get("strata")) if stratified else (None, None)
    rule = params.get("decision") or "wilson"
    opts = None
    if rule == "posterior":
        key = None
        if not stratified:
            key, strata = crowding(state, params)
        opts = {"tau": float(params.get("plateau", 0.1)),
                "rho": float(params.get("resolve_p", 0.8)),
                "draws": int(params.get("draws") or evidence.DRAWS),
                "seed": int(state.get("seed") or 0), "eligible": eligible(state, params),
                "key": key}
    s = evidence.support(bounds, n_cells, valid, prior=prior, to_fit=to_fit,
                         tolerance=params["tolerance"],
                         resolve_within=params.get("resolve_within"),
                         min_between=int(params.get("min_between") or 0),
                         min_between_share=float(params.get("min_between_share") or 0.0),
                         n_above=above(state), calls=calls, strata=strata, rule=rule,
                         posterior_opts=opts)
    if rule == "posterior" and s["resolved"]:
        thin = thin_sides(state, s, params)
        if thin:
            s["resolved"] = False
            s["thin"] = thin
    return s


def thin_sides(state, s, params) -> list:
    """The intervals beside the leader read fewer than `added_min_reads`
    times either way while cells there are still unshown. The posterior
    takes two reads at face value: with every interval read once or twice
    and pooled monotone, P(within) can pass `resolve_p` on reads that decide
    nothing (bench, dense_spill CD3 under a reader wrong a third of the
    time, 2026-10-21: two reads below the top point, resolved 1.9 off). A
    leader resolves only once the cells it calls differently from its
    neighbours have been read; until then the looks go there."""
    least = int(params.get("added_min_reads") or 0)
    if not least:
        return []
    _bounds, n_cells = window(state)
    seen = shown(state)
    lead = s["leader"]
    return [k for k in (lead - 1, lead)
            if 0 <= k < len(n_cells) and n_cells[k] > 0
            and s["table"][k]["own"] + s["table"][k]["not"] < least and seen[k] < n_cells[k]]


def _same(state, to_fit, params):
    """Which candidates of the range are one gate in effect
    (`evidence.equivalence`)."""
    bounds, n_cells = window(state)
    n_up = above(state)
    positives = [int(sum(n_cells[j:])) + n_up for j in range(len(bounds))]
    return evidence.equivalence([float(to_fit(b)) for b in bounds], n_cells, positives,
                                float(params["tolerance"]), int(params.get("min_between") or 0),
                                float(params.get("min_between_share") or 0.0))


def voi_plan(state, to_fit, params, s=None) -> dict | None:
    """{picks, gains, best, look_gain} for the next look: first the reads a
    leader still needs beside it (`thin_sides`, of the support `s`), then
    at least `voi_floor` for an interval never shown, then cells by what
    reading them could change (`evidence.look_gains`), at most `voi_cap`
    from one interval for those, the panel's cells in all; and the expected
    rise of the best P(within the tolerance) from that whole look
    (`evidence.look_gain`). The reads are pooled here (no strata): where to
    look, not what was seen. None when no cell is left unshown."""
    bounds, n_cells = window(state)
    seen = shown(state)
    available = [max(0, n - s) for n, s in zip(n_cells, seen)]
    if not any(available):
        return None
    total = int(params["panel_cells"])
    table = evidence.counts(bounds, _valid(state))
    labelled = sum(c["own"] + c["not"] + c["other"] for c in table)
    definite = (sum(c["own"] + c["not"] for c in table) / labelled) if labelled else 1.0
    same = _same(state, to_fit, params)
    elig = eligible(state, params)
    seed = int(state.get("seed") or 0)
    draws = int(params.get("draws") or evidence.DRAWS)
    best, gains = evidence.look_gains(table, n_cells, available, eligible=elig, same=same,
                                      seed=seed, draws=draws,
                                      batch=int(params.get("voi_batch") or 8),
                                      definite=max(definite, 0.2))
    cap = int(params.get("voi_cap") or total)
    room = [min(a, cap) for a in available]
    picks = [0] * len(n_cells)
    least = int(params.get("added_min_reads") or 0)
    for k in (s or {}).get("thin") or ():
        # Enough cells that, at the share read either way so far, the side
        # reaches `added_min_reads`.
        need = least - (table[k]["own"] + table[k]["not"])
        want = math.ceil(need / max(definite, 0.2))
        picks[k] = min(available[k], max(want, 0), total - sum(picks))
        room[k] = max(room[k], picks[k])
    floor = int(params.get("voi_floor") or 0)
    for k in range(len(n_cells)):
        if seen[k] == 0 and room[k] > 0 and not picks[k] and sum(picks) + floor <= total:
            picks[k] = min(floor, room[k])
    left = total - sum(picks)
    if left > 0 and any(g > 0 for g in gains):
        extra = allocate(gains, [r - p for r, p in zip(room, picks)], left)
        picks = [p + e for p, e in zip(picks, extra)]
    gain = evidence.look_gain(table, n_cells, picks, eligible=elig, same=same, seed=seed,
                              draws=draws,
                              simulations=int(params.get("voi_simulations") or 200),
                              definite=max(definite, 0.2)) if any(picks) else 0.0
    return {"picks": picks, "gains": [round(g, 4) for g in gains], "best": round(best, 4),
            "look_gain": round(gain, 4)}


def isolated(state, to_fit, params) -> dict | None:
    """The support of the isolated cells' reads alone, when the crowded
    cells hold most of the reads nobody could make (`uncertain`, `artifact`)
    and those reads resolve a leader that needs no more growth; else None.
    The gate it gives is the isolated cells' boundary, flagged
    `isolated_only` (its confidence capped)."""
    bounds, _ = window(state)
    other = {"0": 0, "1": 0}
    for obs in _valid(state):
        for cell in obs.get("cells") or ():
            if cell.get("sentinel") or cell.get("crowded") is None or \
                    cell.get("label") in (evidence.OWN, evidence.NOT_OWN) or \
                    evidence.interval_of(float(cell["value"]), bounds) is None:
                continue
            other[str(int(cell["crowded"]))] += 1
    if other["1"] <= other["0"]:
        return None
    alone = dict(state, observations=[
        dict(o, cells=[c for c in o.get("cells") or ()
                       if c.get("sentinel") or (c.get("crowded") is not None
                                                and not int(c["crowded"]))])
        for o in state["observations"]])
    s = support(alone, to_fit, dict(params, strata=None))
    if s is None or not s["resolved"] or growth(alone, s, to_fit, params) is not None:
        return None
    s["isolated_only"] = True
    return s


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
    change = growth(state, s, to_fit, params)
    if change is not None:
        return change
    if params.get("sampling") == "voi" and not s["resolved"]:
        v = voi_plan(state, to_fit, params, s)
        if v is not None:
            plan = _plan(state, list(range(len(n_cells))), v["picks"], "voi",
                         look_gain=v["look_gain"])
            if plan is not None:
                return plan
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


def can_step(state, side, params) -> bool:
    """Whether a step beyond the lattice's edge on `side` may still be
    tried: the range already reaches that edge, fewer than `edge_steps`
    were tried, and some cells lie beyond it."""
    if int(((state.get("grown") or {}).get(side)) or 0) >= int(params.get("edge_steps") or 0):
        return False
    lo, hi = state["window"]
    points = state["points"]
    if side == "up":
        return hi == len(points) - 1 and \
            int(points[hi]["n_positive"]) >= evidence.MIN_MATERIAL
    below = int(state.get("n_finite") or 0) - int(points[lo]["n_positive"])
    return lo == 0 and below >= evidence.MIN_MATERIAL


def splittable(state, s, k, to_fit, params, *, mixed=True) -> bool:
    """Whether interval k (of the window) is wide, holds enough cells to
    matter, was not split before, and reads ordered -- or mixed, unless
    `mixed` is False."""
    bounds, n_cells = window(state)
    if not 0 <= k < len(n_cells) or not params.get("split_width"):
        return False
    if float(to_fit(bounds[k + 1])) - float(to_fit(bounds[k])) <= float(params["split_width"]):
        return False
    if [round(bounds[k], 6), round(bounds[k + 1], 6)] in (state.get("split") or []):
        return False
    if n_cells[k] < evidence.material(params.get("min_between") or 0,
                                      params.get("min_between_share") or 0.0,
                                      s["positives"]):
        return False
    share, n = evidence.estimate(s["table"][k])
    low, high = params.get("split_share") or (0.2, 0.8)
    if mixed and share is not None and n >= float(params.get("split_reads") or 0) \
            and low <= share <= high:
        return True
    # A share outside the band can still hide an edge inside the interval
    # when its dim end holds most of the cells (lsp11385_2 CD163,
    # 2026-10-21: 4 of 21 read stained, all four at its top).
    return ordered(*reads_in(state, k), params)


def reads_in(state, k) -> tuple:
    """(values read `own_stain`, values read `not_own_stain`) in interval k
    of the window, over the valid looks (sentinels apart)."""
    bounds, _ = window(state)
    own, other = [], []
    for obs in state["observations"]:
        if not obs.get("valid", True):
            continue
        for cell in obs.get("cells") or ():
            if cell.get("sentinel") or \
                    evidence.interval_of(float(cell["value"]), bounds) != k:
                continue
            if cell.get("label") == evidence.OWN:
                own.append(float(cell["value"]))
            elif cell.get("label") == evidence.NOT_OWN:
                other.append(float(cell["value"]))
    return own, other


def ordered(own, not_own, params) -> bool:
    """Whether an interval's stained reads lie above its unstained ones: at
    least `split_order_min` of each word and a one-sided rank test
    (Mann-Whitney) at `split_order_p`. A reader's slips fall anywhere in an
    interval; an edge inside it puts the stained reads at its top."""
    least = int(params.get("split_order_min") or 0)
    if not least or len(own) < least or len(not_own) < least:
        return False
    from scipy.stats import mannwhitneyu

    p = float(mannwhitneyu(own, not_own, alternative="greater").pvalue)
    return p <= float(params.get("split_order_p") or 0.0)


def split_plan(state, s, to_fit, params, *, mixed=True) -> dict | None:
    """The split of the first interval beside the leader (below, then
    above) that is wide and reads ordered or (with `mixed`) mixed; None."""
    bounds, _ = window(state)
    for k in (s["leader"] - 1, s["leader"]):
        if splittable(state, s, k, to_fit, params, mixed=mixed):
            return {"kind": "split", "interval": k, "from": bounds[k], "to": bounds[k + 1]}
    return None


def growth(state, s, to_fit, params) -> dict | None:
    """The range's next change before the leader can stand: a lattice point
    beyond its edge (`extend`), a step beyond the lattice (`step`), or a
    wide mixed interval beside the leader cut finer (`split`); None."""
    bounds, _ = window(state)
    leader = s["leader"]
    lo, hi = state["window"]
    if leader == 0 and lo > 0:
        return {"kind": "extend", "side": "down"}
    if leader == len(bounds) - 1 and hi < len(state["points"]) - 1:
        return {"kind": "extend", "side": "up"}
    # A step checks a leader that would otherwise stand; while a rival still
    # blocks it, the cells between them are the look's business (a step taken
    # first spent the look a dispute below needed: two synthetic CD4s under
    # a noisy reader, 2026-10-21).
    if s["resolved"] and leader == 0 and can_step(state, "down", params):
        return {"kind": "step", "side": "down"}
    if s["resolved"] and leader == len(bounds) - 1 and can_step(state, "up", params):
        return {"kind": "step", "side": "up"}
    return split_plan(state, s, to_fit, params)


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
    if s["resolved"] and growth(state, s, to_fit, params) is None:
        # A leader at the edge of the surveyed range says the gate is there or
        # beyond it: the next point is looked at first; one beside a wide
        # mixed interval may belong inside it.
        if not state.get("rare"):
            return "resolved", s
        # A rare marker's leader stands only on its own cells read stained.
        status = tail_status(state, s)
        if status == "confirmed":
            return "resolved", s
        if status == "refuted":
            return "positives_not_confirmed", s
    valid = sum(1 for o in state["observations"] if o.get("valid", True))
    # Under the posterior, unreadable only while the reads leave several
    # gates (the plateau wider than the tolerance): a leader the reads
    # already favour, short only of reads beside it, takes its next look
    # (lsp11385_2 live, 2026-10-21: CD11c, Ecad and PDL1 closed unreadable
    # after two looks at P(within) 0.87 to 0.998).
    undecided = (s.get("posterior") or {}).get("within", 0.0) < \
        float(params.get("resolve_p", 0.8)) - 1e-12 if s.get("posterior") else True
    if s["labelled"] >= params["min_labelled"] \
            and valid >= int(params.get("unreadable_looks") or 1) \
            and (s["other_share"] or 0) > params["max_other_share"] and undecided:
        if params.get("uncertain_state") and not state.get("rare"):
            alone = isolated(state, to_fit, params)
            if alone is not None:
                return "resolved", alone
        return "unreadable", s
    if len(state["observations"]) >= int(params["looks_max"]):
        return ("no_identifiable_boundary" if continuous else "budget_exhausted"), s
    if params.get("sampling") == "voi" and not s["resolved"] \
            and float(params.get("voi_epsilon") or 0) > 0 \
            and growth(state, s, to_fit, params) is None:
        # Not before a look has been read either way: one valid look at
        # least (`s` exists), so a first panel of crowds never ends it.
        v = voi_plan(state, to_fit, params, s)
        if v is not None:
            s["voi"] = {k: v[k] for k in ("best", "look_gain")}
            # A leader short of reads beside it is never given up on: the
            # next look reads them, whatever the posterior expects of them.
            if not s.get("thin") and v["look_gain"] < float(params["voi_epsilon"]):
                return "looks_uninformative", s
    return None, s
