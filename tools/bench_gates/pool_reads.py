"""Pool the reads of several stored candidates sessions per marker (no model, no data).

    PYTHONPATH=. python tools/bench_gates/pool_reads.py TRUTH_JSON IMAGE SESSION_JSON SESSION_JSON [SESSION_JSON ...]

Each session read different cells with a fresh reader; pooling two runs'
observations is what a second reader on a marker would add. Per marker: the
leader on each run's own reads, on every pair pooled, and on all pooled; the
spread over runs vs over pairs; gaps to the person's gates. Points: the union
of the runs' points (the same lattice, plus each run's steps and splits);
reads are binned by value, so pooling needs no interval bookkeeping.
"""
import copy
import itertools
import json
import statistics as st
import sys

from plexora.plugins.gating.server.autogate import hypotheses
from plexora.plugins.gating.server.autogate.hypotheses import controller

truth = json.load(open(sys.argv[1]))[sys.argv[2]]
sessions = [json.load(open(p)) for p in sys.argv[3:]]
units = [{u["marker"]: u for u in s["units"].values()} for s in sessions]
IDENTITY = float


def pooled(states):
    points = {}
    for st_ in states:
        for p in st_["points"]:
            points.setdefault(round(p["low"], 6), p)
    merged = [points[k] for k in sorted(points)]
    base = copy.deepcopy(states[0])
    prior_low = states[0]["points"][int(states[0]["prior"])]["low"]
    base["points"] = merged
    base["window"] = [0, len(merged) - 1]
    base["prior"] = min(range(len(merged)), key=lambda i: abs(merged[i]["low"] - prior_low))
    base["observations"] = [o for st_ in states for o in st_["observations"]]
    base["params"] = {**hypotheses.PARAMS, **base["params"]}
    s = controller.support(base, IDENTITY, base["params"])
    return None if s is None else (s["low"], s["resolved"])


rows = []
for m in sorted(truth):
    states = [u[m].get("hypotheses") for u in units if m in u]
    if len(states) != len(units) or any(not h or not h.get("observations") for h in states):
        continue
    ref = truth[m]
    single = [float(u[m]["final"]) for u in units]
    pairs = [pooled(list(c)) for c in itertools.combinations(states, 2)]
    allp = pooled(states)
    rows.append((m, ref, single, [p[0] for p in pairs], allp))
    print(f"{m:6} user {ref:.2f} | runs {' '.join(f'{g - ref:+.2f}' for g in single)} "
          f"(spread {max(single) - min(single):.2f}) | pairs "
          f"{' '.join(f'{p[0] - ref:+.2f}' for p in pairs)} "
          f"(spread {max(p[0] for p in pairs) - min(p[0] for p in pairs):.2f}) | all three "
          f"{allp[0] - ref:+.2f}{' resolved' if allp[1] else ''}")
single_spread = st.mean(max(r[2]) - min(r[2]) for r in rows)
pair_spread = st.mean(max(r[3]) - min(r[3]) for r in rows)
print(f"\nmarkers {len(rows)}: mean spread single runs {single_spread:.3f}, pooled pairs "
      f"{pair_spread:.3f}")
print(f"MAE single runs {st.mean(abs(g - r[1]) for r in rows for g in r[2]):.3f}, pooled pairs "
      f"{st.mean(abs(g - r[1]) for r in rows for g in r[3]):.3f}, all three "
      f"{st.mean(abs(r[4][0] - r[1]) for r in rows):.3f}")
