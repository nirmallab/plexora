"""Every threshold a marker's gate may take, fixed before any look.

An agent's answers choose a path through a session; they must not choose the
number. So each marker gets one lattice, computed from its data and its gated
partners the first time a look needs it and frozen on the unit: the Auto
(mixture) gate, the other estimators' gates, each partner's negative control
and within-partner fit, the scored candidates (`scoring`: the proposal, each
partner curve's point, the background onset and noise ceiling), fixed steps
from the start (the scored proposal when the session started there, else the
Auto gate) in each direction, and the guard band's edges. Points are snapped to the sidebar's grid, merged when
they snap together (the merged point keeps the strongest name, `PRIORITY`),
and sorted. Every gate a session writes after a look is a lattice point, so
two agents that agree about the cells write the same gate to the digit,
whatever route their answers took.

Pure over (data, marker, partners, profile): the same inputs give the same
lattice, byte for byte (`fingerprint`).
"""

from __future__ import annotations

import hashlib
import json

#: Bumped whenever the points a lattice holds change meaning.
VERSION = "2"

#: Which name a merged point keeps, strongest first: a partner's negative
#: control, a within-partner fit and a partner curve stand on another
#: marker's evidence; the scored proposal on all of it; the Auto gate on the
#: mixture; the background's onset and ceiling and the estimators on the
#: distribution; steps and edges on nothing but distance.
PRIORITY = ("ctrl", "within", "bio", "score", "gmm", "ceiling", "onset", "kde", "otsu",
            "gmm2", "up", "down", "edge")

#: Scored candidates that join the lattice (`scoring` families), and are
#: kept even outside the guard band: the band is drawn around the mixture,
#: which is exactly what is wrong on the markers these candidates are for.
SCORED = ("score", "bio", "ceiling", "onset")

#: [cal] steps from the Auto gate, in sds of the population the gate moves
#: into (the positives' going up, the background's going down).
STEPS = (0.5, 1.0, 2.0)

#: Estimators of the profile that become points (`profile` estimators).
ESTIMATORS = ("kde", "otsu", "gmm2")

#: A T4 look shows at most this many points in its direction.
MAX_CHAIN = 4

#: Points that anchor a chain: it never runs past the first of them.
ANCHORS = ("ctrl", "within", "bio")

#: [cal] an anchor this close to the current gate (table units: about one
#: snap step) is where the gate already stands, so it does not end the
#: chain -- a look that offered only it moved the gate by nothing (live run
#: lsp11385: PD1 0.001, CD11c 0.004 from their anchors).
ANCHOR_AT_GATE = 0.01

#: [cal] points nearer the current gate than half a snap step are the gate
#: itself (an unsnapped start against its snapped point: LAG3 5.98999 vs
#: 5.99 showed a row of zero cells).
SAME_POINT = 0.005


def family(point_id: str) -> str:
    return str(point_id).split(":", 1)[0]


def _rank(point_id):
    name = family(point_id)
    return PRIORITY.index(name) if name in PRIORITY else len(PRIORITY)


def build(ds, marker, *, gmm, estimators_raw=None, references=(), high=None,
          seed=0, scoring=None, start=None) -> dict:
    """{version, points: [{id, low, sources, n_positive, fraction}], beyond,
    guard, fingerprint} for one marker. `references` are the unit's gated
    partners (`Engine.references_ready`); `scoring` the unit's scored
    candidates (`scoring.score`, compact), `start` the gate the session
    started from (steps are taken from it; the Auto gate when None)."""
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import bivariate, candidates
    from plexora.plugins.gating.server.autogate import profile as profmod

    col = profmod.column(ds, marker)
    fit = profmod.fit_for(ds, marker)
    desc = model._description(ds).get(marker) or {}
    top = float(col.sorted32[-1]) if high is None and col.n_finite else float(high)
    raw = []
    if gmm is not None:
        raw.append(("gmm", float(gmm)))
    for name in ESTIMATORS:
        value = (estimators_raw or {}).get(name)
        if value is not None:
            raw.append((name, float(value)))
    for ref in references:
        control = bivariate.negative_control(ds, marker, float(gmm if gmm is not None else 0),
                                             ref["marker"], float(ref["gate"]),
                                             relation=ref["relation"])
        if control:
            raw.append((f"ctrl:{ref['marker']}", float(control["p99"])))
        if ref["relation"] == "subset":
            within = bivariate.within_partner(ds, marker, ref["marker"], float(ref["gate"]),
                                              current=gmm, seed=seed)
            if within.get("ok"):
                raw.append((f"within:{ref['marker']}", float(within["low"])))
    scored = []
    for cand in (scoring or {}).get("candidates") or []:
        if family(cand["id"]) in SCORED and cand.get("low") is not None:
            if family(cand["id"]) == "bio" and not cand.get("robust", True):
                continue
            scored.append((cand["id"], float(cand["low"])))
    raw.extend(scored)
    guard = candidates.guard_band(fit)
    if guard is not None:
        raw.append(("edge:low", float(col.from_fit(guard[0]))))
        raw.append(("edge:high", float(col.from_fit(guard[1]))))
    reach = [float(col.to_fit(v)) for _id, v in scored]
    core_sd = ((scoring or {}).get("background") or {}).get("sd_fit")
    if start is not None and core_sd:
        # Started from the scored proposal: the mixture's spreads describe a
        # split that missed, so the steps are the background's core sd.
        g0 = float(col.to_fit(start))
        reach.append(g0)
        for step in STEPS:
            up, down = g0 + step * core_sd, g0 - step * core_sd
            raw.append((f"up:{step:g}sd", float(col.from_fit(up))))
            raw.append((f"down:{step:g}sd", float(col.from_fit(down))))
            reach += [up, down]
    elif fit is not None and gmm is not None:
        pools = profmod.pools(fit)
        g0 = float(col.to_fit(gmm))
        for step in STEPS:
            raw.append((f"up:{step:g}sd", float(col.from_fit(g0 + step * pools["sd_pos"]))))
            raw.append((f"down:{step:g}sd", float(col.from_fit(g0 - step * pools["sd_bg"]))))
    if guard is not None and reach:
        # The band widens to take in the scored candidates and the steps
        # around a scored start, which the mixture's band would throw away.
        guard = (min([guard[0], *reach]), max([guard[1], *reach]))
    merged, beyond = {}, []
    for point_id, value in raw:
        fit_value = float(col.to_fit(value))
        if guard is not None and family(point_id) not in SCORED \
                and not guard[0] - 1e-9 <= fit_value <= guard[1] + 1e-9:
            beyond.append({"id": point_id, "low": value})
            continue
        snapped = model.snap_to_grid(value, top, desc)[0] if desc else value
        entry = merged.setdefault(snapped, {"low": snapped, "sources": []})
        entry["sources"].append(point_id)
    points = []
    for snapped in sorted(merged):
        entry = merged[snapped]
        sources = sorted(entry["sources"], key=lambda p: (_rank(p), p))
        n_positive = col.n_positive(snapped, top)
        points.append({"id": sources[0], "low": float(snapped), "sources": sources,
                       "n_positive": int(n_positive),
                       "fraction": n_positive / col.n_finite if col.n_finite else None})
    out = {"version": VERSION, "marker": marker, "points": points, "beyond": beyond,
           "guard": ([float(col.from_fit(guard[0])), float(col.from_fit(guard[1]))]
                     if guard is not None else None),
           "references": [{"marker": r["marker"], "gate": float(r["gate"]),
                           "relation": r["relation"]} for r in references]}
    out["fingerprint"] = fingerprint(out)
    return out


def fingerprint(lattice) -> str:
    blob = json.dumps({"version": lattice.get("version"),
                       "points": [(p["id"], p["low"]) for p in lattice.get("points") or []]},
                      sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def point(lattice, point_id):
    """The point named `point_id` (its merged name or any of its sources)."""
    for entry in lattice.get("points") or []:
        if entry["id"] == point_id or point_id in entry["sources"]:
            return entry
    return None


def nearest(lattice, value):
    """The lattice point closest to `value` (a start that is not on the
    lattice, such as a carried gate, joins it here)."""
    points = lattice.get("points") or []
    if not points:
        return None
    return min(points, key=lambda p: (abs(p["low"] - float(value)), _rank(p["id"])))


def chain(lattice, current, direction, *, max_points=MAX_CHAIN):
    """The points a T4 look shows, nearest `current` first, in `direction`
    ("up" or "down"): every point beyond the current gate up to and
    including the first anchor (a negative control or within-partner fit --
    the gate never jumps past another marker's evidence in one look), at
    most `max_points` (the anchor kept when the cap bites). An anchor within
    `ANCHOR_AT_GATE` of the current gate is where the gate already is and
    does not end the chain. Without an anchor the cap keeps the nearest
    points: dropping the middle would leave one row spanning tens of
    thousands of cells, which no look can judge (a later round goes on)."""
    current = float(current)
    # A point that snaps onto the current gate flips no cell: an empty row.
    gap = SAME_POINT
    if direction == "up":
        ahead = [p for p in lattice.get("points") or [] if p["low"] > current + gap]
    else:
        ahead = sorted((p for p in lattice.get("points") or [] if p["low"] < current - gap),
                       key=lambda p: -p["low"])
    out, anchored = [], False
    for entry in ahead:
        out.append(entry)
        if any(family(s) in ANCHORS for s in entry["sources"]) \
                and abs(entry["low"] - current) > ANCHOR_AT_GATE:
            anchored = True
            break
    if len(out) > max_points:
        out = out[:max_points - 1] + [out[-1]] if anchored else out[:max_points]
    return out


def place(chain_points, verdicts, direction):
    """(point or None, passed, why): where the rows put the gate.

    Row i is the cells between the gate before it and chain point i. Going
    up, the gate moves past every row judged `mostly_negative`; going down,
    past every row judged `mostly_positive`; it stops at the first row that
    says otherwise. None when it does not move: `why` is `keep` (the first row
    says the current gate was right) or `mixed` (no row boundary separates
    the cells)."""
    moving = "mostly_negative" if direction == "up" else "mostly_positive"
    passed = 0
    for index, _entry in enumerate(chain_points):
        verdict = verdicts.get(f"i{index + 1}")
        if verdict != moving:
            break
        passed += 1
    if passed:
        return chain_points[passed - 1], passed, "moved"
    first = verdicts.get("i1")
    return None, 0, "mixed" if first == "mixed" else "keep"
