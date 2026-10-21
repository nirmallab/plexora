"""Fit, or check, the candidates engine's confidence calibration.

    python tools/bench_gates/calibrate.py --truth dev_gates.json 'runs/dev/*.json' \
        --out plexora/plugins/gating/server/autogate/hypotheses/calibration.json
    python tools/bench_gates/calibrate.py --truth heldout_gates.json 'runs/ho/*.json' \
        --check plexora/plugins/gating/server/autogate/hypotheses/calibration.json

Reads stored session records of `engine: candidates` runs, takes every unit
decided by panels -- review units too, since a review gate's error is what
its confidence must say (`--resolved-only`: as before VERSION 6) -- with a
person's gate in `--truth` (`{project: {marker: gate}}`, table units), and
fits P(|gate - person's gate| <= 0.2) on the features
`hypotheses.calibration.features` computes from the stored state
(`--features`, at most five, from `calibration.CANDIDATE_FEATURES`). Fit on
dev records only; `--check` reports reliability and coverage on others.
A fitted table changes confidence the next time the engine runs: commit it
with the calibration's VERSION and the records it was fitted on named in
docs/internal/bench/CANDIDATES_PLAN.md.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from score_gap import gap, load_truth  # noqa: E402

from plexora.plugins.gating.server.autogate.hypotheses import calibration  # noqa: E402


def records(patterns, truth, *, resolved_only=False, names=None):
    rows, outcomes, units = [], [], []
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            for unit in (record.get("units") or {}).values():
                state = unit.get("hypotheses") or {}
                ref = (truth.get(unit.get("project")) or {}).get(unit.get("marker"))
                if ref is None or state.get("status") in (None, "no_candidates") or \
                        (resolved_only and state.get("status") != "resolved"):
                    continue
                row = calibration.features(state, names)
                g = gap(unit.get("final"), ref, unit.get("high"))
                if row is None or g is None:
                    continue
                rows.append(row)
                outcomes.append(1 if abs(g) <= calibration.TOLERANCE + 1e-9 else 0)
                units.append((unit["project"], unit["marker"], round(g, 3)))
    return rows, outcomes, units


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+", help="globs of session records")
    parser.add_argument("--truth", required=True)
    parser.add_argument("--out", help="write the fitted table here")
    parser.add_argument("--check", help="a fitted table to evaluate on these records")
    parser.add_argument("--l2", type=float, default=1.0)
    parser.add_argument("--resolved-only", action="store_true")
    parser.add_argument("--features", default=",".join(calibration.FEATURES),
                        help="comma-separated, from calibration.CANDIDATE_FEATURES")
    args = parser.parse_args(argv)
    names = tuple(n for n in args.features.split(",") if n)
    unknown = [n for n in names if n not in calibration.CANDIDATE_FEATURES]
    if unknown or not 0 < len(names) <= 5:
        raise SystemExit(f"--features: one to five of {calibration.CANDIDATE_FEATURES}")
    rows, outcomes, _units = records(args.sessions, load_truth(args.truth),
                                     resolved_only=args.resolved_only, names=names)
    print(f"{len(rows)} panel units with a person's gate; "
          f"{sum(outcomes)} within {calibration.TOLERANCE}")
    if args.check:
        table = calibration.load(args.check)
        if table is None:
            raise SystemExit(f"{args.check}: no table of version {calibration.VERSION}")
    else:
        table = calibration.fit(rows, outcomes, l2=args.l2, features=names)
    print("| predicted | n | mean predicted | observed |")
    print("|---|---|---|---|")
    for b in calibration.reliability(table, rows, outcomes):
        print(f"| {b['low']:.1f}-{b['high']:.1f} | {b['n']} | {b['predicted']:.2f} | "
              f"{b['observed']:.2f} |")
    for name, at in calibration.LEVELS:
        chosen = [o for r, o in zip(rows, outcomes) if calibration.predict(table, r) >= at]
        if chosen:
            print(f"{name} (p >= {at}): {len(chosen)} units, "
                  f"{sum(chosen) / len(chosen):.0%} within the tolerance")
    if args.out and not args.check:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=1)
        print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
