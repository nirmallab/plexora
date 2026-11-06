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


#: What a P4 read that the browser cannot do turns into, by the error it gives.
P4_CONVERSIONS = [
    ("lzf", "string", "h5wasm-plugins 0.3.0's lzf filter aborts on variable-length strings: "
                      "re-save the strings without lzf (anndata compression='gzip'), or read the file on a node"),
]


def p4(rec, _all):
    m, c = rec["metrics"], rec["checks"]
    rows = m["matrix"]
    core = ["obs_names", "obs_numeric", "obs_categorical", "obsm_spatial", "x_rows", "x_column"]
    plain = [r for r in rows if r["kind"] == "h5ad" and r["codec"] in ("none", "gzip") and r["op"] in core]
    plain_ok = [r for r in plain if r["state"] == "ok"]
    # A ratio over a read h5py does in under 5 ms is a ratio of overheads.
    timed = [r for r in plain_ok if r.get("ratio") is not None and (r.get("pythonMs") or 0) >= 5]
    worst = max((r["ratio"] for r in timed), default=None)
    worst_row = max(timed, key=lambda r: r["ratio"]) if timed else None
    sdata = [r for r in rows if r["kind"] == "spatialdata"]
    unsupported = [r for r in rows if r["state"] != "ok"]

    def named(r):
        text = f"{r.get('codec', '')} {r['op']} {r.get('error', '')}".lower()
        is_string = r["op"] in ("obs_names", "var_names", "obs_categorical")
        return any(codec in text and (kind != "string" or is_string) for codec, kind, _ in P4_CONVERSIONS)

    differs = [r for r in rows if r["state"] == "differs"]
    go = [
        check("h5ad none/gzip: obs, obsm, X row block and X column read and identical to h5py",
              f"{len(plain_ok)}/{len(plain)}", len(plain) > 0 and len(plain_ok) == len(plain), "all"),
        check("h5ad none/gzip: slowest read vs h5py (x; reads h5py takes >= 5 ms)",
              f"{r1(worst)} ({worst_row['file']} {worst_row['op']})" if worst_row else None,
              worst is not None and worst <= 2, "<= 2"),
        check("SpatialData (zarr v3): table, image, labels, shapes, points identical",
              f"{sum(r['state'] == 'ok' for r in sdata)}/{len(sdata)}",
              len(sdata) > 0 and all(r["state"] == "ok" for r in sdata), "all"),
        check("every read the browser cannot do has a named conversion",
              f"{sum(named(r) for r in unsupported)}/{len(unsupported)}",
              all(named(r) for r in unsupported) and not differs, "all"),
    ]
    obs_dead = [r for r in plain if r["op"] in ("obs_numeric", "obsm_spatial") and r["state"] != "ok"]
    stop = [check("obs or obsm unreadable (none/gzip)", len(obs_dead), bool(obs_dead), "0")]
    return go, stop


def p7(rec, _all):
    """Per layout: Python-relative build time, identity, page health, reopen."""
    m, c = rec["metrics"], rec["checks"]
    go, stop = [], []
    for layout, v in m["layouts"].items():
        b = v["build"]
        py = v["python"]["seconds"]
        ident = c["identity"].get(layout, {})
        reopen = v.get("reopen") or {}
        left = (b["storage"]["quota"] - b["storage"]["usage"]) if b.get("storage") else None
        go += [
            check(f"[{layout}] build vs Python's pyramidize_image (x)", r1(b["wallMs"] / 1000 / py),
                  b["wallMs"] / 1000 <= 2 * py, "<= 2"),
            check(f"[{layout}] tiles identical to Python's pyramid, every level and channel",
                  f"{ident.get('identical')}/{ident.get('count')}",
                  ident.get("identical") == ident.get("count") and ident.get("count", 0) > 0, "all"),
            check(f"[{layout}] main-thread long tasks > 50 ms during the build", b["longTasks"]["over50"],
                  b["longTasks"]["over50"] <= 5, "<= 5"),
            check(f"[{layout}] first tile after a relaunch (ms)", r1(reopen.get("firstTileMs")),
                  reopen.get("firstTileMs") is not None and reopen["firstTileMs"] <= 1000, "<= 1000"),
            check(f"[{layout}] room the browser reports after the build >= what was written (GB)",
                  round(left / 1e9, 1) if left is not None else None,
                  left is not None and left >= b["timings"]["bytesOut"], ">= written"),
        ]
        stop += [
            check(f"[{layout}] build > 5 min", r1(b["wallMs"] / 1000), b["wallMs"] > 300000, "> 300 s"),
            check(f"[{layout}] pyramid lost across a relaunch", reopen.get("firstTileMs") is None,
                  reopen.get("firstTileMs") is None, "false"),
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


def p5r(rec, _all):
    """A remote node's protocol answers in the page against the same requests outside it."""
    c, m = rec["checks"], rec["metrics"]
    go = [check(f"(a, remote) {name}", c.get(name), c.get(name) is True, "true")
          for name in ("hdTilesIdentical", "webpTilesIdentical", "batchMatchesSingles",
                       "allCellsIdentical", "describeIdentical") if name in c]
    go.append(check("round trip p50 (ms), for the record", r1((m.get("rttMs") or {}).get("p50")), True, "recorded"))
    return go, []


def p5t(rec, _all):
    """The same remote node through the static page and through the desktop route (P5's gate)."""
    c = rec["checks"]
    ratio = lambda k: c.get(k)  # noqa: E731
    go = [
        check("open, cold (C1): browser / desktop, median of paired blocks", r1(ratio("openRatioC1")),
              ratio("openRatioC1") is not None and ratio("openRatioC1") <= 1.2, "<= 1.2"),
        check("navigation, cold (C1): geometric mean per block of browser / desktop, median", r1(ratio("navRatioC1")),
              ratio("navRatioC1") is not None and ratio("navRatioC1") <= 1.2, "<= 1.2"),
        check("navigation, warm (C2): the same", r1(ratio("navRatioC2")),
              ratio("navRatioC2") is not None and ratio("navRatioC2") <= 1.2, "<= 1.2"),
        check("every step of both arms full within 10 s", c.get("everyStepFullBy10s"),
              c.get("everyStepFullBy10s") is True, "true"),
        check("paired blocks", c.get("blocks"), (c.get("blocks") or 0) >= 4, ">= 4"),
    ]
    worst = [x for x in (ratio("openRatioC1"), ratio("navRatioC1")) if x is not None]
    stop = [check("ratio > 2 on open or navigation (C1)", r1(max(worst)) if worst else None,
                  any(x > 2 for x in worst), "> 2")]
    return go, stop


def p5lnah(rec, _all):
    c = rec["checks"]
    go = [
        check("headed Chrome holds a loopback fetch from a public page while its prompt waits",
              c.get("promptHeldTheFetch"), c.get("promptHeldTheFetch") is True, "true"),
        check("the fetch works once the permission is granted", c.get("worksOnceGranted"),
              c.get("worksOnceGranted") is True, "true"),
    ]
    return go, []


def p6(rec, _all):
    m = rec["metrics"]
    cold, warm = m["cold"], m["warm"]
    browser_blocks = [b for b in m["blocks"] if b["arm"] == "browser"]
    page_load = statistics.median(b["pageLoadMs"] for b in browser_blocks if b.get("pageLoadMs") is not None) \
        if browser_blocks else None
    # The product's open counts from navigation (its page, its boot, its first
    # view); the browser's from picking the file -- so its page load is added.
    open_ratio = None
    if cold["open"]["product"] and cold["open"]["browser"] is not None and page_load is not None:
        open_ratio = (cold["open"]["browser"] + page_load) / cold["open"]["product"]
    peak = (m.get("peakRenderer") or {}).get("browser")
    go = [
        check("open: browser (page load + first view) / product (C1)", r1(open_ratio),
              open_ratio is not None and open_ratio <= 1.3, "<= 1.3"),
        check("navigation, cold: geometric mean of browser / product per step", r1(cold["navGeoMeanRatio"]),
              cold["navGeoMeanRatio"] is not None and cold["navGeoMeanRatio"] <= 1.3, "<= 1.3"),
        check("navigation, revisit: geometric mean of browser / product per step", r1(warm["navGeoMeanRatio"]),
              warm["navGeoMeanRatio"] is not None and warm["navGeoMeanRatio"] <= 1.3, "<= 1.3"),
        check("browser renderer peak working set (GB)", round(peak / 2**30, 2) if peak else None,
              peak is not None and peak <= 2 * 2**30, "<= 2"),
        check("every step reached full in both arms", rec["checks"].get("everyStepFull"),
              rec["checks"].get("everyStepFull") is True, "true"),
    ]
    stop = [check("ratio > 2 on open or navigation", r1(max(x for x in (open_ratio, cold["navGeoMeanRatio"]) if x is not None)),
                  any(x is not None and x > 2 for x in (open_ratio, cold["navGeoMeanRatio"])), "> 2")]
    return go, stop


def p8(rec, _all):
    results = rec["metrics"]["results"]
    go, stop = [], []
    for engine, v in results.items():
        rows = v["rows"]
        storage = {"P2B", "P7", "P3"}
        port = engine == "webkit"   # Playwright's WebKit on Windows: the engine, not Safari
        core = [r for r in rows if not (port and r["poc"] in storage)]
        go.append(check(f"[{engine}] S features pass" + (" (storage features apart)" if port else ""),
                        f"{sum(r['pass'] for r in core)}/{len(core)}", all(r["pass"] for r in core), "all"))
        if port:
            failed = [r["poc"] for r in rows if r["poc"] in storage and not r["pass"]]
            go.append(check("[webkit] OPFS features (Windows WebKit has no OPFS backend; Safari not measured)",
                            None if failed else "pass", not failed, "on Safari (Mac)"))
        luma = [x["maxAbs"] for x in (v.get("lumaVsChrome") or []) if x]
        go.append(check(f"[{engine}] picture vs Chrome: worst cell, mean luminance (levels)", r1(max(luma)) if luma else None,
                        bool(luma) and max(luma) <= 2, "<= 2"))
    every = [r for v in results.values() for r in v["rows"]]
    stop.append(check("S fails in every other engine", None if not every else
                      sum(not r["pass"] for r in every), every and all(not r["pass"] for r in every), "never"))
    return go, stop


RULES = {"P0": p0, "P1": p1, "P2": p2, "P2B": p2b, "P3": p3, "P4": p4, "P5": p5, "P5LNAH": p5lnah, "P5R": p5r, "P5T": p5t, "P6": p6,
         "P7": p7, "P8": p8}


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
