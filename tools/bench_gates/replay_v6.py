"""Replay the VERSION 6 decision on stored candidates sessions (no model, no data).

    PYTHONPATH=. python tools/bench_gates/replay_v6.py TRUTH_JSON IMAGE SESSION_JSON [...] \
        [--common-prior] [--json OUT]

For every candidates unit with reads, in every session given (replicate runs
of one image): the leader and status the stored reads give under the
VERSION 5 rule (`hypotheses.V5_RULES`: the Wilson test, as recorded) and
under VERSION 6 (`hypotheses.PARAMS`: the posterior over candidates, its
plateau and the member nearest the start, added points eligible only once
read on both sides, no split at close), beside the person's gate
(`{image: {marker: gate}}`, table units). Points a split at close added
are removed for VERSION 6. The looks are the stored ones: VERSION 6 would
have spent them elsewhere (`sampling="voi"`), so this replays the decision,
not the session; a VERSION 6 status of None means it would have looked
again. `--common-prior` gives every run the lattice point nearest the mean
of the runs' starts as its prior (the start drifts with the partner gates a
run decided first). `--set KEY=VALUE` (repeatable) adds a third rule, `alt`:
VERSION 6 with those `PARAMS` changed (`--set leader=resolving`). A session
VERSION 6 recorded keeps its unit seed, so its replay is its own decision.

Summary per rule: mean |gap|, the run-to-run spread per marker (max - min),
the share 0.4 or more off, and the accepted (resolved) gates' count and mean
|gap| beside the review gates'. Fit units are taken as table units, as on
lsp11385_2 (a log1p table). A sanity check, never a selection: the VERSION
6 rules were explored on lsp11385_2 (CANDIDATES_PLAN.md, "VERSION 6").
"""

from __future__ import annotations

import argparse
import copy
import json
import statistics as st
import sys
import zlib

from plexora.plugins.gating.server.autogate import hypotheses
from plexora.plugins.gating.server.autogate.hypotheses import controller

IDENTITY = float


def _without_close_split(state):
    """The state with the points a split at close added taken out."""
    cut = state.get("split_at_close")
    if not cut:
        return state
    added = set(state.get("added_points") or ())
    points = state["points"]
    lo, hi = state["window"]
    keep_lo, keep_hi = points[lo]["low"], points[hi]["low"]
    keep_prior = points[int(state["prior"])]["low"]
    kept = [p for p in points if not (p["id"] in added and str(p["id"]).startswith("split:")
                                      and cut["from"] < p["low"] < cut["to"])]
    where = {p["low"]: i for i, p in enumerate(kept)}
    state["points"] = kept
    state["window"] = [where[keep_lo], where[keep_hi]]
    state["prior"] = where[keep_prior]
    state["split"] = [s for s in state.get("split") or ()
                      if [round(cut["from"], 6), round(cut["to"], 6)] != s]
    return state


def prepared(h, rules, seed, overrides=None):
    state = copy.deepcopy(h)
    for key, value in (("n_finite", 0), ("grown", {"up": 0, "down": 0}), ("split", [])):
        state.setdefault(key, value)
    stored = dict(state.get("params") or {})
    if rules == "v5":
        params = {**hypotheses.PARAMS, **stored, **hypotheses.V5_RULES}
    else:
        state = _without_close_split(state)
        fresh = {k: v for k, v in hypotheses.PARAMS.items() if k not in stored}
        params = {**stored, **fresh, "split_at_close": False, **(overrides or {})}
        state["seed"] = state.get("seed") or seed
    state["params"] = params
    return state


def _jsonish(value):
    try:
        json.loads(value)
        return True
    except ValueError:
        return False


def decide(state):
    status, s = controller.decide(state, IDENTITY, state["params"])
    return status, s


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("truth")
    parser.add_argument("image")
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--common-prior", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.set:
        key, value = item.split("=", 1)
        overrides[key] = json.loads(value) if _jsonish(value) else value
    names = ("v5", "v6", "alt") if overrides else ("v5", "v6")
    truth = json.load(open(args.truth))[args.image]
    sessions = [json.load(open(p)) for p in args.sessions]
    by_marker = {}
    for run, session in enumerate(sessions):
        seed = int((session.get("options") or {}).get("seed") or 0)
        for unit in session["units"].values():
            h = unit.get("hypotheses")
            if not h or not h.get("observations") or unit.get("project") != args.image:
                continue
            by_marker.setdefault(unit["marker"], {})[run] = (unit, h, seed)
    markers = sorted(m for m, runs in by_marker.items()
                     if len(runs) == len(sessions) and m in truth)
    rows = []
    for marker in markers:
        runs = by_marker[marker]
        starts = [float(h.get("start")) for _u, h, _s in runs.values() if h.get("start")]
        common = st.fmean(starts) if starts else None
        row = {"marker": marker, "user": truth[marker], "runs": []}
        for run in sorted(runs):
            unit, h, seed = runs[run]
            out = {"run": run, "stored": unit.get("final"), "stored_status": h.get("status")}
            for rules in names:
                key = f"{unit['project']}::{marker}".encode()
                state = prepared(h, "v6" if rules == "alt" else rules,
                                 (seed * 100_003 + zlib.crc32(key)) & 0x7FFFFFFF,
                                 overrides if rules == "alt" else None)
                if args.common_prior and common is not None:
                    state["prior"] = min(range(len(state["points"])),
                                         key=lambda i: abs(state["points"][i]["low"] - common))
                status, s = decide(state)
                # Accepted only as the engine would: resolved and nothing (a
                # step, a split, a rare marker's tail) left to look at first.
                out[rules] = {"low": None if s is None else s["low"], "status": status,
                              "resolved": status == "resolved",
                              **({"within": s["posterior"]["within"],
                                  "plateau": [m["low"] for m in s["posterior"]["plateau"]]}
                                 if s and s.get("posterior") else {})}
            row["runs"].append(out)
        rows.append(row)
        print(f"{marker:6} user {row['user']:.2f} | v5 "
              + " ".join(f"{r['v5']['low'] - row['user']:+.2f}{'*' if r['v5']['resolved'] else ''}"
                         for r in row["runs"])
              + "".join(f" | {rules} "
                        + " ".join(f"{r[rules]['low'] - row['user']:+.2f}"
                                   f"{'*' if r[rules]['resolved'] else ''}"
                                   f"({r[rules].get('within', 0):.2f})" for r in row["runs"])
                        for rules in names[1:]))
    print("\n(* resolved; v6 in brackets: P within the tolerance of the leader)")
    summary = {}
    for rules in names:
        gaps = [abs(r[rules]["low"] - row["user"]) for row in rows for r in row["runs"]]
        acc = [abs(r[rules]["low"] - row["user"]) for row in rows for r in row["runs"]
               if r[rules]["resolved"]]
        rev = [abs(r[rules]["low"] - row["user"]) for row in rows for r in row["runs"]
               if not r[rules]["resolved"]]
        spreads = [max(r[rules]["low"] for r in row["runs"])
                   - min(r[rules]["low"] for r in row["runs"]) for row in rows]
        summary[rules] = {
            "mae": st.fmean(gaps), "spread": st.fmean(spreads),
            "far_0.4": sum(g >= 0.4 - 1e-9 for g in gaps) / len(gaps),
            "accepted": len(acc), "accepted_mae": st.fmean(acc) if acc else None,
            "review": len(rev), "review_mae": st.fmean(rev) if rev else None,
            "one_gate": sum(s < 1e-9 for s in spreads)}
    fidelity = sum(1 for row in rows for r in row["runs"]
                   if r["stored"] is not None and abs(r["v5"]["low"] - r["stored"]) > 1e-6
                   and r["stored_status"] != "resolved")
    print(f"\n{len(rows)} markers x {len(sessions)} runs; review leaders differing from the "
          f"stored final under v5: {fidelity}")
    print("| rule | MAE | spread | >=0.4 off | one gate | accepted (MAE) | review (MAE) |")
    print("|---|---|---|---|---|---|---|")
    for rules, s in summary.items():
        fmt = (lambda v: "-" if v is None else f"{v:.3f}")
        print(f"| {rules} | {s['mae']:.3f} | {s['spread']:.3f} | {s['far_0.4']:.0%} | "
              f"{s['one_gate']} | {s['accepted']} ({fmt(s['accepted_mae'])}) | "
              f"{s['review']} ({fmt(s['review_mae'])}) |")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "summary": summary}, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
