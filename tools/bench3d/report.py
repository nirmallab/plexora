"""Before/after tables from two benchmark runs.

    python tools/bench3d/report.py BEFORE.json AFTER.json [--md PERF.md] [--regressions-only]
                                   [--metrics REGEX]
    python tools/bench3d/report.py RUN.json            # one run against the targets

For every dataset and metric: n, median, P95 (nearest rank) and P99, the
change in the median, and PASS/FAIL against targets.json (a value ending in F is
a multiple of that run's measured display period). Runs with different
renderers or one headed and one headless are not compared. `--metrics` keeps
the metrics whose name the regular expression finds (`re.search`), e.g.
`"^(step|jump|drag|pan)_"`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pct(values, p):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return None
    rank = max(1, math.ceil(p * len(values)))
    return values[rank - 1]


def stat(values, which):
    values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not values:
        return None
    return {"p50": pct(values, 0.5), "p95": pct(values, 0.95), "p99": pct(values, 0.99),
            "max": max(values), "min": min(values), "mean": sum(values) / len(values)}[which]


def metrics_of(run):
    """{dataset: {metric: [values]}} across scenarios."""
    out = {}
    for dataset, scenarios in run.get("results", {}).items():
        merged = out.setdefault(dataset, {})
        for scenario in scenarios.values():
            for name, values in (scenario.get("metrics") or {}).items():
                merged.setdefault(name, []).extend(values)
    return out


def period(run, dataset):
    return (run.get("F") or {}).get(dataset) or 16.7


def target_value(target, run, dataset):
    value = target["value"]
    if isinstance(value, str) and value.endswith("F"):
        return float(value[:-1] or 1) * period(run, dataset)
    return float(value)


def check(target, values, run, dataset):
    got = stat(values, target["stat"])
    if got is None:
        return None, None
    want = target_value(target, run, dataset)
    ok = got <= want + 1e-9 if target["op"] == "<=" else got >= want - 1e-9
    return ok, want


def targets_for(dataset, metric, targets):
    return [t for t in targets if t["metric"] == metric
            and (not t.get("datasets") or dataset in t["datasets"])]


def fmt(value):
    if value is None:
        return "–"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def table(before, after, targets, regressions_only=False, pattern=None):
    lines = []
    bm = metrics_of(before) if before else {}
    am = metrics_of(after)
    for dataset in am:
        rows = []
        for metric in sorted(am[dataset]):
            if pattern is not None and not pattern.search(metric):
                continue
            a = am[dataset][metric]
            b = bm.get(dataset, {}).get(metric, [])
            a50, b50 = stat(a, "p50"), stat(b, "p50")
            change = ""
            if a50 is not None and b50:
                change = f"{100 * (a50 - b50) / abs(b50):+.0f} %"
            verdicts = []
            for target in targets_for(dataset, metric, targets):
                ok, want = check(target, a, after, dataset)
                if ok is None:
                    continue
                verdicts.append(f"{'PASS' if ok else 'FAIL'} {target['stat']}{target['op']}{fmt(want)}")
            if regressions_only:
                worse = b50 is not None and a50 is not None and a50 > b50 * 1.1 and a50 - b50 > 2
                failed = any(v.startswith("FAIL") for v in verdicts)
                if not (worse or failed):
                    continue
            rows.append(f"| {metric} | {len(a)} | {fmt(b50)} | {fmt(a50)} | {fmt(stat(a, 'p95'))} | "
                        f"{fmt(stat(a, 'p99'))} | {change} | {'; '.join(verdicts)} |")
        if not rows:
            continue
        lines.append(f"\n#### {dataset} (F = {period(after, dataset):.1f} ms)\n")
        lines.append("| metric | n | before p50 | after p50 | after p95 | after p99 | Δ p50 | target |")
        lines.append("|---|---|---|---|---|---|---|---|")
        lines.extend(rows)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--md", help="append the tables to this markdown file")
    parser.add_argument("--regressions-only", action="store_true")
    parser.add_argument("--metrics", default=None, metavar="REGEX",
                        help="only the metrics whose name this regular expression finds")
    args = parser.parse_args(argv)
    # The tables carry "Δ" and "—": a Windows console's code page has neither.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pattern = re.compile(args.metrics) if args.metrics else None
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.runs]
    before, after = (runs[0], runs[1]) if len(runs) > 1 else (None, runs[0])
    if before is not None:
        same = lambda run: ("SwiftShader" in str(run.get("renderer")), bool(run.get("headed")))  # noqa: E731
        if same(before) != same(after):
            raise SystemExit("the runs differ in renderer family or headed mode; not comparable")
    targets = json.loads((HERE / "targets.json").read_text(encoding="utf-8"))["targets"]
    title = (f"### {after['label']} ({after['sha']}{'-dirty' if after.get('dirty') else ''})"
             + (f" vs {before['label']} ({before['sha']})" if before else "")
             + f" — {after['date']}{' — headed' if after.get('headed') else ''}")
    body = table(before, after, targets, args.regressions_only, pattern)
    text = f"{title}\n\nRenderer: {after.get('renderer')}\n{body}\n"
    errors = [f"- {d}/{s}: {r.get('error').splitlines()[0]}" for d, ss in after.get("results", {}).items()
              for s, r in ss.items() if r.get("error")]
    if errors:
        text += "\nErrors:\n" + "\n".join(errors) + "\n"
    if args.md:
        with open(args.md, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + text)
    print(text)


if __name__ == "__main__":
    main()
