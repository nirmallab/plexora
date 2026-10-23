"""Trace one synthetic scene's candidates sessions, full vs nogrow.

    ABL=full|nogrow PLEXORA_TESTING=1 PYTHONPATH=. python tools/bench_gates/trace_scene.py \
        SCENARIO INDEX AGENT SEED MARKER[,MARKER]

INDEX is the scene's position in the bench's scenario list (its seed is
SEED + INDEX, as in bench.run_synthetic; a scene's reader stream also depends
on the packets earlier markers took, so two arms diverge after any change).
Built in a temporary data root, never the user's. Prints, per marker, the points, the
looks (plan kind, intervals, own/not/other per interval) and the end.
"""
import json
import os
import sys
import tempfile

from plexora.plugins.gating.server.autogate import hypotheses

abl = os.environ.get("ABL", "full")
if abl == "nogrow":
    hypotheses.PARAMS.update(edge_steps=0, split_width=0, unreadable_looks=1)

scenario, index, style, seed, want = sys.argv[1], int(sys.argv[2]), sys.argv[3], \
    int(sys.argv[4]), sys.argv[5].split(",")

root = tempfile.mkdtemp(prefix="trace-", dir=os.environ.get("TRACE_DIR"))
os.environ["PLEXORA_DATA_PATH"] = root
from plexora import paths  # noqa: E402
paths.reset()
from plexora.agent import AgentSession, jobs, registry  # noqa: E402
from plexora.ai import bench  # noqa: E402

registry.discover(["gating"])
session = AgentSession(table_limit=4)
markers = ("CD3", "CD8", "CD20", "CD4", "FOXP3")
name = f"bench_{scenario}"
made = bench._register_scenario(root, name, scenario, grid=24, size=1024,
                                seed=seed + index, markers=markers)
agent = bench.TruthAgent(made["values"], made["truth"], style, seed=seed + index,
                         ids=made.get("ids"))
units, status, _s, sid = bench.run_session(
    session, name, markers, agent, mode="propose",
    options={"engine": "v2", "agent": "bench:candidates", "reuse_answers": False})
import numpy as np
stored = {u["marker"]: u for u in bench._session_units(sid)}
def f1(m, g):
    v, t = np.asarray(made["values"][m]), np.asarray(made["truth"][m], bool)
    p = v > g
    tp = int((p & t).sum()); fp = int((p & ~t).sum()); fn = int((~p & t).sum())
    return round(2 * tp / max(1, 2 * tp + fp + fn), 3)
for m in want:
    u = stored.get(m) or {}
    h = u.get("hypotheses") or {}
    print(f"== {abl} {m}: final {u.get('final')} state {u.get('state')} status "
          f"{h.get('status')} prior {h.get('prior')} window {h.get('window')}")
    print("   points", [(round(p["low"], 1), p["n_positive"], f1(m, p["low"])) for p in h.get("points") or []])
    print("   final F1", f1(m, u.get("final")) if u.get("final") is not None else None,
          "truth positives", int(np.asarray(made["truth"][m]).sum()), "of", len(made["truth"][m]))
    print("   added", h.get("added_points"), "grown", h.get("grown"), "split", h.get("split"))
    for i, o in enumerate(h.get("observations") or []):
        plan = o.get("plan") or {}
        cells = o.get("cells") or []
        by = {}
        for c in cells:
            if c.get("sentinel"):
                continue
            key = c.get("interval")
            d = by.setdefault(str(key), {"own": 0, "not": 0, "other": 0, "vals": []})
            lab = c.get("label")
            d["own" if lab == "own_stain" else "not" if lab == "not_own_stain" else "other"] += 1
            d["vals"].append(round(float(c.get("value", 0)), 1))
        print(f"   look {i + 1} {plan.get('kind')} valid={o.get('valid', True)}",
              {k: (v["own"], v["not"], v["other"], min(v["vals"]), max(v["vals"]))
               for k, v in by.items()})
    sup = h.get("support") or {}
    print("   leader", h.get("leader"), "fractions", sup.get("fractions"))
    print("   reason", h.get("reason"))
session.close()
jobs.drain(120)
