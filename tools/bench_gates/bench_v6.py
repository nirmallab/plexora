"""VERSION 5 against VERSION 6 on synthetic scenes, paired, with replicates (no model).

    PLEXORA_TESTING=1 PYTHONPATH=. python tools/bench_gates/bench_v6.py \
        --scenes easy,overlap,dense_spill --agent noisy:0.1:0.3:0.15 --repeats 3 \
        --seed 1 --out runs.json [--arms v5,v6]

Each scene is built once (seed `--seed` + its index, as `plexora ai bench
gating` does) in a temporary data root; each repeat r gates it once per arm
with a scripted reader seeded `--seed + 1000 r + index` (the same reader
seed for both arms; their streams part as soon as their packets differ) and
the session seed r, so cells drawn differ between repeats as on live runs.
An arm is a set of `hypotheses.PARAMS` overrides: `v5` is
`hypotheses.V5_RULES`, `v6` the defaults, and `v6-<abl>` drops one VERSION 6
part (`novoi`, `noelig`, `nostrata`, `noearly`, as in `ablate.py`) or tries
one change (`resolving`: `leader="resolving"`).

Per (scene, marker, arm, repeat): the gate written (or proposed), its gap
to the truth's best single cut in log1p units, F1 against the per-cell
truth, the state and the hypotheses status, and the panels shown. `--out`
keeps the rows; the summary per arm is printed: mean |gap|, the share
within 0.2 and 0.4 or more off, the run-to-run spread per marker (log1p),
markers on one gate, accepted vs review mean |gap|, panels per marker, the
statuses, and every (scene, marker) more than 0.30 worse than the first
arm (mean over repeats). Readers `noisy:P:U:L` (`bench.reader_style`).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import tempfile
from collections import Counter, defaultdict

import numpy as np

ABLATIONS = {"novoi": {"sampling": "survey_refine"}, "noelig": {"added_min_reads": 0},
             "nostrata": {"strata": None}, "noearly": {"voi_epsilon": 0.0},
             "resolving": {"leader": "resolving"}}


def arm_params(name):
    from plexora.plugins.gating.server.autogate import hypotheses

    if name == "v5":
        return dict(hypotheses.V5_RULES)
    out = {}
    for part in name.split("-")[1:]:
        out.update(ABLATIONS[part])
    return out


def f1(values, truth, gate):
    called = np.asarray(values) > gate
    truth = np.asarray(truth, dtype=bool)
    tp = int((called & truth).sum())
    fp = int((called & ~truth).sum())
    fn = int((~called & truth).sum())
    return 2 * tp / max(1, 2 * tp + fp + fn)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenes", required=True)
    parser.add_argument("--agent", default="noisy:0.1:0.3:0.15")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--arms", default="v5,v6")
    parser.add_argument("--markers", default="CD3,CD8,CD20,CD4,FOXP3")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    scenes = [s for s in args.scenes.split(",") if s]
    arms = [a for a in args.arms.split(",") if a]
    markers = tuple(m for m in args.markers.split(",") if m)

    root = tempfile.mkdtemp(prefix="bench-v6-")
    os.environ["PLEXORA_DATA_PATH"] = root
    from plexora import paths

    paths.reset()
    from plexora.agent import AgentSession, jobs, registry
    from plexora.ai import bench
    from plexora.plugins.gating.server.autogate import hypotheses

    base = dict(hypotheses.PARAMS)
    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    rows = []
    try:
        for index, scenario in enumerate(scenes):
            name = f"bench_{scenario}"
            made = bench._register_scenario(root, name, scenario, grid=24, size=1024,
                                            seed=args.seed + index, markers=markers)
            probe = bench.TruthAgent(made["values"], made["truth"], "oracle", seed=0,
                                     ids=made.get("ids"))
            for repeat in range(args.repeats):
                for arm in arms:
                    hypotheses.PARAMS.clear()
                    hypotheses.PARAMS.update(base, **arm_params(arm))
                    agent = bench.TruthAgent(made["values"], made["truth"], args.agent,
                                             seed=args.seed + 1000 * repeat + index,
                                             ids=made.get("ids"))
                    _units, _status, _s, sid = bench.run_session(
                        session, name, markers, agent, mode="propose",
                        options={"engine": "candidates", "seed": repeat,
                                 "agent": f"bench:{arm}#{repeat}", "reuse_answers": False})
                    stored = {u["marker"]: u for u in bench._session_units(sid)}
                    for marker in markers:
                        unit = stored.get(marker) or {}
                        gate = bench._gate_of(unit)
                        h = unit.get("hypotheses") or {}
                        cut, _ = probe.best(marker)
                        rows.append({
                            "scene": scenario, "marker": marker, "arm": arm, "repeat": repeat,
                            "gate": gate, "state": unit.get("state"),
                            "status": h.get("status"), "panel": bool(h.get("observations")),
                            "looks": len(h.get("observations") or ()),
                            "isolated_only": bool(h.get("isolated_only")),
                            "gap": (None if gate is None else
                                    float(np.log1p(max(gate, 0)) - np.log1p(max(cut, 0)))),
                            "f1": None if gate is None else f1(made["values"][marker],
                                                               made["truth"][marker], gate)})
                    print(f"{scenario} r{repeat} {arm}: "
                          + " ".join(f"{r['marker']}:{r['gap']:+.2f}"
                                     if r["gap"] is not None else f"{r['marker']}:-"
                                     for r in rows[-len(markers):]), flush=True)
    finally:
        hypotheses.PARAMS.clear()
        hypotheses.PARAMS.update(base)
        session.close()
        jobs.drain(120)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"agent": args.agent, "seed": args.seed, "repeats": args.repeats,
                       "scenes": scenes, "arms": arms, "rows": rows}, fh, indent=1)
    print(summarise(rows, arms))
    return 0


ACCEPTED = ("accepted", "accepted_t1")


def summarise(rows, arms) -> str:
    """The table `main` prints, from rows (several files' rows may be joined)."""
    lines = ["| arm | panel markers | MAE | within 0.2 | >=0.4 off | spread | one gate | "
             "accepted (MAE) | review (MAE) | looks/marker | F1 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    statuses = {}
    means = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm and r["gap"] is not None]
        panel = [r for r in mine if r["panel"]]
        gaps = [abs(r["gap"]) for r in panel]
        acc = [abs(r["gap"]) for r in panel if r["state"] in ACCEPTED]
        rev = [abs(r["gap"]) for r in panel if r["state"] not in ACCEPTED]
        by = defaultdict(list)
        for r in panel:
            by[(r["scene"], r["marker"])].append(r["gap"])
        spreads = [max(v) - min(v) for v in by.values() if len(v) > 1]
        means[arm] = {k: st.fmean(abs(g) for g in v) for k, v in by.items()}
        fmt = (lambda v: "-" if v is None else f"{v:.3f}")
        mean = (lambda v: st.fmean(v) if v else None)
        lines.append(
            f"| {arm} | {len(by)} | {fmt(mean(gaps))} | "
            f"{fmt(mean([g <= 0.2 + 1e-9 for g in gaps]))} | "
            f"{fmt(mean([g >= 0.4 - 1e-9 for g in gaps]))} | {fmt(mean(spreads))} | "
            f"{sum(1 for s in spreads if s < 1e-9)} | {len(acc)} ({fmt(mean(acc))}) | "
            f"{len(rev)} ({fmt(mean(rev))}) | {fmt(mean([r['looks'] for r in panel]))} | "
            f"{fmt(mean([r['f1'] for r in panel]))} |")
        statuses[arm] = Counter(r["status"] for r in panel)
    out = "\n".join(lines) + "\n"
    for arm, counts in statuses.items():
        out += f"\n{arm} statuses: {dict(counts)}"
    first = arms[0]
    for arm in arms[1:]:
        shared = sorted(set(means[arm]) & set(means[first]))
        worse = [(k, means[arm][k] - means[first][k]) for k in shared
                 if means[arm][k] - means[first][k] > 0.30 + 1e-9]
        worst = max(((k, means[arm][k] - means[first][k]) for k in shared),
                    key=lambda kv: kv[1], default=None)
        out += (f"\n{arm} vs {first}: worst " +
                (f"{worst[0][0]} {worst[0][1]} {worst[1]:+.3f}" if worst else "-") +
                "; worse by more than 0.30: " +
                (", ".join(f"{k[0]} {k[1]} {d:+.3f}" for k, d in worse) or "none"))
    return out + "\n"


if __name__ == "__main__":
    sys.exit(main())
