"""Run `plexora ai bench gating` with candidates-engine changes switched off.

    ABL=full|norare|noshare|pooled|nogrow PLEXORA_TESTING=1 PLEXORA_DATA_PATH=<scratch> \
        PYTHONPATH=. python tools/bench_gates/ablate.py ai bench gating \
        --synthetic easy,overlap,rare,gradient,dense_spill,low_snr,dim_positive,heterogeneous \
        --agent noisy:0.32 --seed 1 --arms session --variants candidates --out <dir>

ABL names the changes switched off (several may be joined by "-"): v5 (the
VERSION 5 rules, `hypotheses.V5_RULES`: Wilson decision, survey and refine,
split at close, no eligibility, no strata, uncertain dropped), norare (rare
markers need no tail confirmation), noshare (no material share for rivals),
pooled (the leader-vs-rival test pools reads instead of weighting intervals by
their cells), nogrow (no steps, no splits, unreadable after one look). Prints
at exit how often a step, a split and a split at close fired. Compare arms on
the same seeds and the same scenario list: packet counts depend on which
variants share a run. VERSION 6 parts alone: novoi (survey and refine),
noelig (added points may lead at once), nostrata (crowded and isolated
reads pooled), noearly (no `looks_uninformative`).
"""
import os
import sys

from plexora.plugins.gating.server.autogate import hypotheses
from plexora.plugins.gating.server.autogate.hypotheses import controller, evidence

abl = os.environ.get("ABL", "full")
if "v5" in abl.split("-"):
    hypotheses.PARAMS.update(hypotheses.V5_RULES)
for _key in ("novoi", "noelig", "nostrata", "noearly"):
    if _key in abl.split("-"):
        hypotheses.PARAMS.update({"novoi": {"sampling": "survey_refine"},
                                  "noelig": {"added_min_reads": 0},
                                  "nostrata": {"strata": None},
                                  "noearly": {"voi_epsilon": 0.0}}[_key])
if "norare" in abl:
    controller.tail_status = lambda state, s: "confirmed"
if "noshare" in abl:
    hypotheses.PARAMS["min_between_share"] = 0.0
if "pooled" in abl:
    def pooled(table, n_cells, a, b, strata=None, missing="half"):
        own = sum(c["own"] for c in table[a:b])
        read = sum(c["own"] + c["not"] for c in table[a:b])
        return (own / read if read else 0.5), float(read)
    evidence.between = pooled

if "nogrow" in abl:
    hypotheses.PARAMS.update(edge_steps=0, split_width=0, unreadable_looks=1)

import atexit  # noqa: E402
import collections  # noqa: E402

fired = collections.Counter()
for _name in ("_step", "_split"):
    _orig = getattr(hypotheses, _name)

    def _counted(*a, _orig=_orig, _name=_name, **k):
        out = _orig(*a, **k)
        fired[(_name, "added" if out else "empty")] += 1
        return out
    setattr(hypotheses, _name, _counted)
_close = hypotheses._split_at_close


def _counted_close(ds, marker, col, state, s):
    out = _close(ds, marker, col, state, s)
    fired[("_split_at_close", "moved" if out["low"] != s["low"] else "same")] += 1
    return out
hypotheses._split_at_close = _counted_close
_finish = hypotheses.finish


def _counted_finish(engine, unit, status):
    fired[("status", status)] += 1
    if (unit.get("hypotheses") or {}).get("isolated_only"):
        fired[("isolated_only", status)] += 1
    return _finish(engine, unit, status)
hypotheses.finish = _counted_finish
atexit.register(lambda: print("growth fired:", dict(fired), flush=True))

from plexora.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
