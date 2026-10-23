"""Replay the split at close on stored live candidates sessions (no model, no data).

    PYTHONPATH=. python tools/bench_gates/replay_close.py TRUTH_JSON IMAGE SESSION_JSON [...]

For every candidates unit: whether the ordered/mixed split would have fired
inside the look loop at an earlier decision (then the stored looks after it
would have differed and the replay below is not what would have happened),
and, for a marker that closed budget_exhausted, the leader after a split at
close on the reads in hand. Counts at the new points are interpolated
linearly between the neighbouring lattice points (fit units = table units on
this image); points are not snapped to the sidebar grid. The person's gates:
`{image: {marker: gate}}` in table units.
"""
import copy
import json
import math
import sys

from plexora.plugins.gating.server.autogate import hypotheses
from plexora.plugins.gating.server.autogate.hypotheses import controller

REF = json.load(open(sys.argv[1]))[sys.argv[2]]
IDENTITY = float


def n_pos_at(points, value):
    pts = sorted(points, key=lambda p: p["low"])
    for a, b in zip(pts, pts[1:]):
        if a["low"] <= value <= b["low"]:
            t = (value - a["low"]) / (b["low"] - a["low"])
            return int(round(a["n_positive"] + t * (b["n_positive"] - a["n_positive"])))
    return pts[-1]["n_positive"]


def split(state, a, b):
    params = state["params"]
    state.setdefault("split", []).append([round(a, 6), round(b, 6)])
    parts = min(int(params["split_parts"]),
                max(2, math.ceil((b - a) / float(params["tolerance"]) - 1e-9)))
    new = [{"id": f"split:{a + (b - a) * k / parts:g}", "low": a + (b - a) * k / parts,
            "sources": ["split"], "n_positive": n_pos_at(state["points"], a + (b - a) * k / parts)}
           for k in range(1, parts)]
    hypotheses._insert(state, new)
    return new


for path in sys.argv[3:]:
    session = json.load(open(path))
    print("##", session.get("session_id") or path)
    for unit in session["units"].values():
        h = unit.get("hypotheses")
        if not h or not h.get("observations"):
            continue
        m = unit["marker"]
        state = copy.deepcopy(h)
        state["params"] = {**hypotheses.PARAMS, **{k: v for k, v in state["params"].items()
                                                   if k not in ("split_order_min", "split_order_p",
                                                                "split_at_close")}}
        for k, v in (("n_finite", 0), ("grown", {"up": 0, "down": 0}), ("split", [])):
            state.setdefault(k, v)
        obs = state["observations"]
        # Would the new trigger have fired at an earlier decision point?
        early = []
        for j in range(1, len(obs)):
            st = dict(state, observations=obs[:j])
            s = controller.support(st, IDENTITY, st["params"])
            if s is None:
                continue
            plan = controller.split_plan(st, s, IDENTITY, st["params"])
            if plan is not None:
                early.append((j, round(plan["from"], 2), round(plan["to"], 2)))
        s = controller.support(state, IDENTITY, state["params"])
        before = s["low"] if s else None
        after, note = before, ""
        if h.get("status") == "budget_exhausted" and s is not None:
            plan = controller.split_plan(state, s, IDENTITY, state["params"], mixed=False)
            if plan is not None:
                own, other = controller.reads_in(state, plan["interval"])
                split(state, plan["from"], plan["to"])
                s2 = controller.support(state, IDENTITY, state["params"])
                after = s2["low"]
                note = (f"split {plan['from']:.2f}-{plan['to']:.2f} "
                        f"(own {len(own)}, not {len(other)}); fractions {s2['fractions']}")
        ref = REF.get(m)
        gap = (lambda g: f"{g - ref:+.2f}") if ref is not None else (lambda g: "?")
        print(f"{m:6} {h.get('status'):24} final {unit.get('final')} leader {before:.2f} "
              f"({gap(before)}) -> {after:.2f} ({gap(after)}) "
              f"early-trigger {early or '-'} {note}")
