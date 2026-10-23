"""Write the classifier check's trust records from the classifier bench.

    PYTHONPATH=. python tools/bench_gates/classifier_trust.py --arm ARM OUT1.json [OUT2.json ...] \
        --out plexora/plugins/gating/server/autogate/hypotheses/classifier_trust.json

Reads `tools/bench_classifier_wand/bench_wand.py` results (the user's
labelled cells on each image) and keeps, per image and marker, the balanced
accuracy of the classifier arm `--arm` (averaged over its seeds) beside the
Auto gate's on the same cells. The candidates engine's classifier check
(`hypotheses.classifier_check.trusted`) consults the classifier on a marker
only where a record from ANOTHER image shows it beating the gate by
`TRUST_MARGIN` on at least `TRUST_MIN_CELLS` cells; without one it runs
exploratory. Without `--out` the records are printed only. Commit a written
table with the bench results it came from named in
docs/internal/bench/CANDIDATES_PLAN.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np


def balanced(y, p) -> float | None:
    y, p = np.asarray(y), np.asarray(p)
    parts = [float(np.mean(p[y == c] == c)) for c in (0, 1) if (y == c).any()]
    return float(np.mean(parts)) if parts else None


def records(paths, arm) -> list:
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            result = json.load(fh)
        for marker, entry in (result.get("markers") or {}).items():
            y = entry["y"]
            if entry.get("gate_call") is None or arm not in (entry.get("arms") or {}):
                continue
            seeds = [s for s in entry["arms"][arm]["seeds"] if "error" not in s]
            scores = [balanced(y, s["pred"]) for s in seeds]
            scores = [v for v in scores if v is not None]
            gate = balanced(y, entry["gate_call"])
            if not scores or gate is None:
                continue
            out.append({"project": result["project"], "marker": marker, "arm": arm,
                        "n": len(y), "classifier": round(float(np.mean(scores)), 4),
                        "gate": round(gate, 4), "seeds": len(scores),
                        "source": os.path.basename(path)})
    return out


def main(argv=None) -> int:
    from plexora.plugins.gating.server.autogate.hypotheses import classifier_check

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="+", help="bench_wand.py result files")
    parser.add_argument("--arm", required=True, help="the classifier arm the check runs")
    parser.add_argument("--out", help="write the trust table here")
    args = parser.parse_args(argv)
    rows = records(args.results, args.arm)
    print("| image | marker | n | classifier | gate | trusts |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        trusts = r["n"] >= classifier_check.TRUST_MIN_CELLS and \
            r["classifier"] >= r["gate"] + classifier_check.TRUST_MARGIN
        print(f"| {r['project']} | {r['marker']} | {r['n']} | {r['classifier']:.3f} | "
              f"{r['gate']:.3f} | {'yes' if trusts else 'no'} |")
    if args.out:
        table = {"version": classifier_check.TRUST_VERSION, "arm": args.arm,
                 "written": dt.date.today().isoformat(), "records": rows}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=1)
        print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
