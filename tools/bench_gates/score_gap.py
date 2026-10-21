"""Score stored gating sessions against a person's gates.

    python tools/bench_gates/score_gap.py --truth user_gates.json \
        classic='~/gatevision/heldout/sessions/HB*.json' \
        candidates='runs/cand/*.json' [--oracle] [--json out.json]

`--truth` is `{project: {marker: gate}}` in table units (a person's gates; a
file whose `kind` is `model_gates` is refused). Each `NAME=GLOB` is one arm:
its session records (`session.json`, or the copies a campaign keeps). An
image recorded more than once in an arm is a replicate run.

Reported per arm: markers scored, mean |gap|, the share within 0.2 and at
least 0.4 off (the user's retest noise is about 0.2), |gap| of accepted vs
review-closed gates, packets per marker, and the run-to-run spread of the
final gate where an image has replicates. With two or more arms, the first
two are compared on the (image, marker) pairs both scored: the mean paired
difference of |gap| with a bootstrap interval over images. `--oracle` adds,
per arm, how close the best point of each unit's frozen lattice came to the
person's gate -- whether the candidates could have reached it at all.

Also per arm (VERSION 6, CANDIDATES_PLAN.md "Metrics"): markers whose
replicates all wrote one gate (`one_gate`); the mean share of panel reads
`uncertain` or `artifact`; the sentinels' miss rates (`fpr`, `fnr`); the
median relative deviation of the positive count at the gate from the
count at the person's gate (`count_dev`, where `run_arms.py` recorded
`positives_at`); and risk-coverage -- the share of units whose
P(within the tolerance) (`hypotheses.support.posterior.within`) reaches
0.9 ... 0.5, and their mean |gap|. `--baseline NAME` names an arm every
other arm is held against marker by marker: per (image, marker), the mean
|gap| over its runs minus the baseline's, and every marker more than
`--worse` (0.30) worse -- the campaign's kill rule.

Gaps are in table units. A gate at the top of the range (`high`) counts as
"no positives": a person's gate above it scores 0, below it the distance to
the top.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics
import sys
from collections import defaultdict

ACCEPTED = ("accepted", "accepted_t1")
REVIEW = ("accepted_low_confidence", "manual_review_recommended", "insufficient_information",
          "not_binary", "technically_failed", "no_positive_population")
WITHIN = 0.2
FAR = 0.4


def gap(final, ref, top):
    if final is None or ref is None:
        return None
    if top is not None and ref > top:
        return 0.0 if final >= top - 1e-6 else float(final - top)
    if top is not None and final >= top - 1e-6:
        return float(top - ref)
    return float(final - ref)


def load_truth(path) -> dict:
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("kind") == "model_gates":
        raise SystemExit(f"{path} holds a model's gates, not a person's")
    return {p: {m: float(v) for m, v in markers.items() if v is not None}
            for p, markers in data.items() if isinstance(markers, dict)}


def rows_for(arm, paths, truth) -> list:
    rows, seen = [], defaultdict(int)
    for path in sorted(paths):
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        units = list((record.get("units") or {}).values())
        projects = sorted({u.get("project") for u in units})
        for project in projects:
            seen[project] += 1
        for unit in units:
            project, marker = unit.get("project"), unit.get("marker")
            ref = (truth.get(project) or {}).get(marker)
            if ref is None:
                continue
            lattice = [p["low"] for p in (unit.get("lattice") or {}).get("points") or []]
            top = unit.get("high")
            g = gap(unit.get("final"), ref, top)
            start = (unit.get("start") or {}).get("low")
            h = unit.get("hypotheses") or {}
            support = h.get("support") or {}
            rows.append({
                "arm": arm, "run": seen[project], "image": project, "marker": marker,
                "state": unit.get("state"), "final": unit.get("final"), "user": ref,
                "gap": g, "start_gap": gap(start, ref, top),
                "packets": len(unit.get("packets") or []),
                "oracle_gap": (min((abs(gap(p, ref, top)) for p in lattice), default=None)
                               if lattice else None),
                "hypotheses": h.get("status"),
                "within": (support.get("posterior") or {}).get("within"),
                "uncertain": support.get("other_share"),
                "fpr": (h.get("reader") or _reader(h)).get("fpr"),
                "fnr": (h.get("reader") or _reader(h)).get("fnr"),
                "positives_at": unit.get("positives_at")})
    return rows


def _reader(h) -> dict:
    """The sentinels' miss rates of a record without `reader` (before
    VERSION 6): clear negatives read stained, clear positives unstained."""
    neg = pos = fp = fn = 0
    for obs in h.get("observations") or ():
        for cell in obs.get("cells") or ():
            if not cell.get("sentinel"):
                continue
            if cell.get("side") == "positive":
                pos, fn = pos + 1, fn + (cell.get("label") == "not_own_stain")
            else:
                neg, fp = neg + 1, fp + (cell.get("label") == "own_stain")
    return {"fpr": fp / neg if neg else None, "fnr": fn / pos if pos else None}


#: Thresholds of P(within the tolerance) the risk-coverage table reports.
COVERAGE_AT = (0.9, 0.8, 0.7, 0.6, 0.5)


def risk_coverage(rows) -> list:
    """[{at, coverage, n, mae}]: the units whose P(within the tolerance)
    reaches `at`, as a share of the scored units with one, and their mean
    |gap|."""
    scored = [r for r in rows if r["gap"] is not None and r["within"] is not None]
    out = []
    for at in COVERAGE_AT:
        kept = [abs(r["gap"]) for r in scored if r["within"] >= at - 1e-9]
        out.append({"at": at, "coverage": len(kept) / len(scored) if scored else None,
                    "n": len(kept), "mae": statistics.fmean(kept) if kept else None})
    return out


def worse_than(rows, base_rows, limit) -> dict:
    """Per (image, marker) both arms scored: mean |gap| over runs minus the
    baseline's; the markers worse by more than `limit`, and the worst."""
    def means(rs):
        out = defaultdict(list)
        for r in rs:
            if r["gap"] is not None:
                out[(r["image"], r["marker"])].append(abs(r["gap"]))
        return {k: statistics.fmean(v) for k, v in out.items()}

    a, b = means(rows), means(base_rows)
    diffs = {k: a[k] - b[k] for k in sorted(set(a) & set(b))}
    worst = max(diffs.items(), key=lambda kv: kv[1]) if diffs else None
    return {"pairs": len(diffs),
            "worse": [{"image": k[0], "marker": k[1], "by": round(d, 3)}
                      for k, d in diffs.items() if d > limit + 1e-9],
            "worst": None if worst is None else {"image": worst[0][0], "marker": worst[0][1],
                                                 "by": round(worst[1], 3)}}


def _share(values, test):
    return sum(1 for v in values if test(v)) / len(values) if values else None


def summary(rows) -> dict:
    gaps = [abs(r["gap"]) for r in rows if r["gap"] is not None]
    acc = [abs(r["gap"]) for r in rows if r["gap"] is not None and r["state"] in ACCEPTED]
    rev = [abs(r["gap"]) for r in rows if r["gap"] is not None and r["state"] in REVIEW]
    starts = [abs(r["start_gap"]) for r in rows if r["start_gap"] is not None]
    oracle = [r["oracle_gap"] for r in rows if r["oracle_gap"] is not None]
    finals = defaultdict(list)
    for r in rows:
        if r["final"] is not None:
            finals[(r["image"], r["marker"])].append(float(r["final"]))
    spreads = [max(v) - min(v) for v in finals.values() if len(v) > 1]
    mean = (lambda v: statistics.fmean(v) if v else None)
    deviations = [abs(r["positives_at"]["final"] - r["positives_at"]["user"])
                  / max(1, r["positives_at"]["user"]) for r in rows if r.get("positives_at")]
    return {
        "n": len(rows), "scored": len(gaps), "mae": mean(gaps),
        "within_0.2": _share(gaps, lambda v: v <= WITHIN + 1e-9),
        "far_0.4": _share(gaps, lambda v: v >= FAR - 1e-9),
        "accepted_mae": mean(acc), "n_accepted": len(acc),
        "review_mae": mean(rev), "n_review": len(rev),
        "start_mae": mean(starts),
        "oracle_mae": mean(oracle),
        "oracle_within_0.2": _share(oracle, lambda v: v <= WITHIN + 1e-9),
        "packets_per_marker": mean([r["packets"] for r in rows]),
        "run_spread": mean(spreads), "n_replicated": len(spreads),
        "one_gate": sum(1 for v in spreads if v < 1e-9) if spreads else None,
        "uncertain_share": mean([r["uncertain"] for r in rows if r.get("uncertain") is not None]),
        "fpr": mean([r["fpr"] for r in rows if r.get("fpr") is not None]),
        "fnr": mean([r["fnr"] for r in rows if r.get("fnr") is not None]),
        "count_dev": statistics.median(deviations) if deviations else None,
        "risk_coverage": risk_coverage(rows),
    }


def paired(rows_a, rows_b, *, draws=2000, seed=0) -> dict | None:
    """Mean of |gap_b| - |gap_a| over the (image, marker) pairs both arms
    scored (replicates averaged first), with a bootstrap interval that
    resamples whole images."""
    def means(rows):
        out = defaultdict(list)
        for r in rows:
            if r["gap"] is not None:
                out[(r["image"], r["marker"])].append(abs(r["gap"]))
        return {k: statistics.fmean(v) for k, v in out.items()}

    a, b = means(rows_a), means(rows_b)
    shared = sorted(set(a) & set(b))
    if not shared:
        return None
    by_image = defaultdict(list)
    for key in shared:
        by_image[key[0]].append(b[key] - a[key])
    images = sorted(by_image)
    diff = statistics.fmean(d for key in images for d in by_image[key])
    rng = random.Random(seed)
    boots = []
    for _ in range(draws):
        pick = [rng.choice(images) for _ in images]
        values = [d for key in pick for d in by_image[key]]
        boots.append(statistics.fmean(values))
    boots.sort()
    return {"pairs": len(shared), "images": len(images), "mean_diff": diff,
            "ci95": [boots[int(0.025 * draws)], boots[int(0.975 * draws) - 1]]}


def _fmt(value):
    if value is None:
        return "-"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("arms", nargs="+", help="NAME=GLOB of session records")
    parser.add_argument("--truth", required=True, help="{project: {marker: gate}} JSON")
    parser.add_argument("--oracle", action="store_true", help="add the lattice oracle")
    parser.add_argument("--json", help="write the rows and summaries here")
    parser.add_argument("--baseline", help="the arm every other arm is held against, marker "
                                           "by marker")
    parser.add_argument("--worse", type=float, default=0.30,
                        help="a marker this much worse than the baseline is reported")
    args = parser.parse_args(argv)
    truth = load_truth(args.truth)
    arms = {}
    for spec in args.arms:
        name, _, pattern = spec.partition("=")
        paths = glob.glob(os.path.expanduser(pattern))
        if not paths:
            raise SystemExit(f"{spec}: no session records")
        arms[name] = rows_for(name, paths, truth)
    keys = ["scored", "mae", "within_0.2", "far_0.4", "accepted_mae", "review_mae",
            "start_mae", "packets_per_marker", "run_spread", "one_gate", "uncertain_share",
            "fpr", "fnr", "count_dev"]
    if args.oracle:
        keys += ["oracle_mae", "oracle_within_0.2"]
    summaries = {name: summary(rows) for name, rows in arms.items()}
    print("| arm | " + " | ".join(keys) + " |")
    print("|---" * (len(keys) + 1) + "|")
    for name, s in summaries.items():
        print(f"| {name} | " + " | ".join(_fmt(s[k]) for k in keys) + " |")
    for name, s_ in summaries.items():
        curve = [c for c in s_["risk_coverage"] if c["n"]]
        if curve:
            print(f"\n{name} risk-coverage (P within >= at: coverage, n, MAE): " + "; ".join(
                f"{c['at']}: {c['coverage']:.0%} {c['n']} {_fmt(c['mae'])}" for c in curve))
    held = {}
    if args.baseline:
        if args.baseline not in arms:
            raise SystemExit(f"--baseline {args.baseline}: not one of the arms")
        for name in arms:
            if name == args.baseline:
                continue
            held[name] = worse_than(arms[name], arms[args.baseline], args.worse)
            h = held[name]
            print(f"\n{name} vs {args.baseline}, marker by marker ({h['pairs']} pairs): worst "
                  + (f"{h['worst']['image']} {h['worst']['marker']} {h['worst']['by']:+.3f}"
                     if h["worst"] else "-")
                  + f"; worse by more than {args.worse}: "
                  + (", ".join(f"{w['image']} {w['marker']} {w['by']:+.3f}" for w in h["worse"])
                     or "none"))
    names = list(arms)
    comparison = None
    if len(names) >= 2:
        comparison = paired(arms[names[0]], arms[names[1]])
        if comparison:
            print(f"\n{names[1]} - {names[0]}: mean paired |gap| difference "
                  f"{comparison['mean_diff']:+.3f} (95% CI {comparison['ci95'][0]:+.3f} "
                  f"to {comparison['ci95'][1]:+.3f}; {comparison['pairs']} pairs, "
                  f"{comparison['images']} images)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"summaries": summaries, "paired": comparison, "held": held,
                       "rows": [r for rows in arms.values() for r in rows]}, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
