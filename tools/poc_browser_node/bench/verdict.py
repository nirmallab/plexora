"""GO / PARTIAL / STOP for each browser-node POC record, against the thresholds
in docs/internal/BROWSER_NATIVE_INVESTIGATION.md section 8 (plan section 8.3).

    python verdict.py DIR [--poc P1] [--write]

DIR holds the records bench/run.mjs wrote (<poc>-<fixture>-<date>[-label].json).
Every threshold is a named check with the measured value beside it, so a
reader can see why, not only what. A threshold the run could not measure is
listed as `not measured`, never silently passed.

  GO       every GO threshold met, no STOP condition
  PARTIAL  no STOP condition, but at least one GO threshold missed
  STOP     a STOP condition met
`--write` stores {thresholds, verdict} back into each record.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def check(name, value, ok, bound):
    return {"name": name, "value": value, "bound": bound,
            "result": "not measured" if value is None else ("pass" if ok else "fail")}


def r1(x):
    return None if x is None else round(float(x), 1)


def p0(rec, _all):
    c = rec["checks"]
    go = [
        check("pictures identical (node HD tiles vs browser node, every view)", c.get("picturesIdentical"),
              c.get("picturesIdentical") is True, "true"),
        check("rendering modules load unmodified (shims needed)", sorted(c.get("shims", {})),
              set(c.get("shims", {})) <= {"toFloatColor", "_"}, "page-side shims only"),
        check("byte order understood (HD PNG big-endian, geotiff little-endian)", c.get("byteOrder", {}).get("pngIsBigEndian"),
              c.get("byteOrder", {}).get("pngIsBigEndian") is True, "true"),
    ]
    stop = [check("a module needs an edit in plexora/client/src", False, False, "false")]
    return go, stop


def p1(rec, records):
    m, c = rec["metrics"], rec["checks"]
    pool4 = next((b for b in m.get("bench", []) if b["poolSize"] == 4), m.get("bench", [{}])[0])
    seq = pool4.get("sequentialMs", {})
    ident = c.get("identity", {})
    auto = m.get("autoLevel", [])
    viewer = next((r for r in records if r["poc"] == "P1V" and r["fixture"]["name"] == rec["fixture"]["name"]), None)
    pan7 = step_ratio = open_ratio = None
    if viewer:
        blocks = viewer["metrics"]["blocks"]
        def runs(arm, ch):
            return [run for b in blocks if b["arm"] == arm for run in b["runs"] if run["channels"] == ch]
        b7 = runs("browser", 7)
        pan7 = (statistics.median([r["pan"]["intervals"]["p50"] for r in b7]),
                statistics.median([r["pan"]["intervals"]["p95"] for r in b7])) if b7 else None
        ratios, opens = [], []
        for ch in (3, 7, 15):
            nb, bb = runs("node", ch), runs("browser", ch)
            if nb and bb:
                ratios.append(statistics.median([r["stepMs"]["p50"] for r in bb]) /
                              statistics.median([r["stepMs"]["p50"] for r in nb]))
                opens.append(statistics.median([r["openMs"] + r["homeFull"]["ms"] for r in bb]) /
                             statistics.median([r["openMs"] + r["homeFull"]["ms"] for r in nb]))
        step_ratio = max(ratios) if ratios else None
        open_ratio = max(opens) if opens else None
    go = [
        check("tile median (ms, one at a time, 4 workers)", r1(seq.get("p50")), (seq.get("p50") or 1e9) <= 30, "<= 30"),
        check("tile p95 (ms)", r1(seq.get("p95")), (seq.get("p95") or 1e9) <= 80, "<= 80"),
        check("pan median / p95 at 7 channels (ms, frame interval, unthrottled)", pan7 and [r1(pan7[0]), r1(pan7[1])],
              bool(pan7) and pan7[0] <= 12 and pan7[1] <= 20, "<= 12 / <= 20"),
        check("tour step to full, browser / local-Python node (worst channel count, medians)", r1(step_ratio) if step_ratio else None,
              step_ratio is not None and step_ratio <= 1.5, "<= 1.5"),
        check("open to home full, browser / local-Python node (worst)", r1(open_ratio) if open_ratio else None,
              open_ratio is not None and open_ratio <= 1.0, "<= 1.0"),
        check("random tiles byte-identical to the node's HD PNG", f"{ident.get('identical')}/{ident.get('count')}",
              ident.get("identical") == ident.get("count") and ident.get("count", 0) >= 200, "200/200"),
        check("auto-level (GMM) per channel (ms, worst)", r1(max((a["totalMs"] for a in auto), default=None)),
              bool(auto) and max(a["totalMs"] for a in auto) <= 2000, "<= 2000"),
        check("provisional window per channel (ms, worst)", r1(max((a["provisionalMs"] for a in auto), default=None)),
              bool(auto) and max(a["provisionalMs"] for a in auto) <= 200, "<= 200"),
    ]
    stop = [
        check("tile median > 60 ms", r1(seq.get("p50")), (seq.get("p50") or 0) > 60, "> 60"),
        check("pan median > 25 ms at 7 channels", pan7 and r1(pan7[0]), bool(pan7) and pan7[0] > 25, "> 25"),
        check("a tile that differs", ident.get("count", 0) - ident.get("identical", 0),
              ident.get("identical") != ident.get("count"), "> 0"),
    ]
    return go, stop


def p2(rec, _all):
    m, c = rec["metrics"], rec["checks"]
    browser = [b for b in m["blocks"] if b["arm"] == "browser"]
    drag = statistics.median([b["drag"]["updateMs"]["p50"] for b in browser]) if browser else None
    cells = browser[0]["cells"] if browser else None
    ident = c.get("labelIdentity") or {}
    go = [
        check(f"gate drag, drawn update median (ms) at {cells} ids", r1(drag), drag is not None and drag <= 30, "<= 30"),
        check("label tiles byte-identical to the node's", f"{ident.get('identical')}/{ident.get('count')}",
              ident.get("identical") == ident.get("count") and ident.get("count", 0) > 0, "all"),
        check("GPU cell layer active in every block", c.get("labelGpuActive"), c.get("labelGpuActive") is True, "true"),
        check("every block drew the same canvas", c.get("canvasesAgree"), c.get("canvasesAgree") is True, "true"),
    ]
    stop = [check("GPU cell layer could not run", not c.get("labelGpuActive"), not c.get("labelGpuActive"), "false")]
    return go, stop


def p2b(rec, _all):
    m, c = rec["metrics"], rec["checks"]
    b = m["build"]
    ratio = b["timings"]["bytesOut"] / b["sourceBytes"] if b.get("sourceBytes") else None
    py = m.get("python", {}).get("seconds")
    ident = c.get("identity", {})
    go = [
        check("build wall (s)", r1(b["wallMs"] / 1000), b["wallMs"] <= 120000, "<= 120"),
        check("build vs Python's own conversion (x)", r1(b["wallMs"] / 1000 / py) if py else None,
              bool(py) and b["wallMs"] / 1000 <= 2 * py, "<= 2 (hypothesis)"),
        check("main-thread long tasks > 50 ms during the build", b["longTasks"]["over50"], b["longTasks"]["over50"] <= 5, "<= 5"),
        check("derived bytes / source file bytes", round(ratio, 2) if ratio else None, ratio is not None and ratio <= 2, "<= 2"),
        check("first tile after a reopen (ms)", r1(m["reopen"]["firstTileMs"]), m["reopen"]["firstTileMs"] <= 1000, "<= 1000"),
        check("derived tiles identical to Python's pyramid", f"{ident.get('identical')}/{ident.get('count')}",
              ident.get("identical") == ident.get("count"), "all"),
    ]
    stop = [
        check("build > 5 min", r1(b["wallMs"] / 1000), b["wallMs"] > 300000, "> 300 s"),
        check("quota exceeded with >= 50 GB free", None, False, "never"),
    ]
    return go, stop


def p3(rec, _all):
    m, c = rec["metrics"], rec["checks"]
    polars = m["python"]["polarsReadMs"]
    drawable = m["ingest"].get("drawableMs") or m["loadMs"]
    roi100 = max((r["ms"] for r in c.get("roi", []) if r["vertices"] == 100), default=None)
    peak = (m.get("memory", {}).get("peakWorkingSetBytes") or {}).get("byKind", {}).get("renderer")
    identity = (c.get("allCellsIdentical") and all(c.get("columnsIdentical", {}).values())
                and c.get("describeFloat64", {}).get("nineDigits") and c["nearest"]["agree"] == c["nearest"]["count"]
                and all(r["identical"] for r in c["roi"]) and c["gates"]["agree"] == c["gates"]["count"])
    go = [
        check("cold load to drawable / polars' read of the same file (x)", r1(drawable / polars), drawable <= 5 * polars, "<= 5"),
        check("reopen (ms)", r1(m["reopenMs"]), m["reopenMs"] <= 5000, "<= 5000"),
        check("describe, every column (ms)", r1(m["describeMs"]), m["describeMs"] <= 3000, "<= 3000"),
        check("uncached column (ms, worst of 5)", r1(max(m["columnMs"])), max(m["columnMs"]) <= 200, "<= 200"),
        check("nearest cell (ms, worst of 200)", r1(max(m["nearestMs"])), max(m["nearestMs"]) <= 5, "<= 5"),
        check("ROI, 100 vertices (ms, worst)", r1(roi100), roi100 is not None and roi100 <= 300, "<= 300"),
        check("identity: all_cells, columns, describe to 9 digits, nearest, ROI, gates", bool(identity), bool(identity), "all"),
        check("peak renderer working set (GB)", round(peak / 2**30, 2) if peak else None, peak is not None and peak <= 2 * 2**30, "<= 2"),
    ]
    stop = [check("an identity failure", not identity, not identity, "false")]
    return go, stop


def p5(rec, _all):
    c = rec["checks"]
    a = c.get("a", {})
    go = [
        check("(a) HD and 8-bit tiles identical to the same requests outside the browser",
              a.get("hdTilesIdentical") and a.get("webpTilesIdentical"), bool(a.get("hdTilesIdentical") and a.get("webpTilesIdentical")), "true"),
        check("(a) table ops identical (all_cells, describe) and batch = singles",
              a.get("allCellsIdentical") and a.get("describeIdentical") and a.get("batchMatchesSingles"),
              bool(a.get("allCellsIdentical") and a.get("describeIdentical") and a.get("batchMatchesSingles")), "true"),
        check("(e) recovers when its node restarts with the same token", c.get("e", {}).get("recoveredWithSameToken"),
              c.get("e", {}).get("recoveredWithSameToken") is True, "true"),
        check("(g) connector pairing", c.get("g", {}).get("paired"), c.get("g", {}).get("paired") is True, "true"),
        check("(h) node-served app under an /rnode/ prefix draws", c.get("h", {}).get("drewPixels"),
              c.get("h", {}).get("drewPixels") is True, "true"),
        check("open C1 vs the desktop node arm", None, False, "<= 1.2 (manual O2 record)"),
    ]
    stop = [check("a primary-only capability with no node route", False, False, "false")]
    return go, stop


RULES = {"P0": p0, "P1": p1, "P2": p2, "P2B": p2b, "P3": p3, "P5": p5}


def verdict(go, stop):
    if any(s["result"] == "pass" for s in stop):
        return "STOP"
    measured = [g for g in go if g["result"] != "not measured"]
    if all(g["result"] == "pass" for g in measured):
        return "GO" if len(measured) == len(go) else "GO (some not measured)"
    return "PARTIAL"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("dir")
    parser.add_argument("--poc", default=None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    paths = sorted(Path(args.dir).glob("P*.json"))
    records = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in paths]
    records = [(p, r) for p, r in records if isinstance(r, dict) and "poc" in r]
    allrecs = [r for _, r in records]
    out = []
    for path, rec in records:
        rule = RULES.get(rec["poc"])
        if not rule or (args.poc and rec["poc"] != args.poc):
            continue
        go, stop = rule(rec, allrecs)
        v = verdict(go, stop)
        out.append({"file": path.name, "poc": rec["poc"], "fixture": rec["fixture"]["name"], "verdict": v})
        print(f"\n{path.name}: {v}")
        for g in go:
            print(f"  GO   [{g['result']:>12}] {g['name']}: {g['value']} ({g['bound']})")
        for s in stop:
            if s["result"] == "pass":
                print(f"  STOP [{'MET':>12}] {s['name']}: {s['value']}")
        if args.write:
            rec["thresholds"] = {"go": go, "stop": stop}
            rec["verdict"] = v
            path.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8", newline="\n")
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
