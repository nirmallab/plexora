"""L2's comparison: openslide-js (bench/l2.mjs) against openslide-python
(bench/truth_l2.py), slide by slide.

    python -I bench/compare_l2.py <truth_dir> <js_dir> [--write record.json]

The rules (L2.md):
- metadata exactly: level count, every level's dimensions, every downsample
  (to 1e-9), and the property set -- every key and value, except those the two
  builds may differ in by version (`openslide.version`-like keys are listed,
  not failed);
- pixels: identical bytes where both libraries decode with the same codec
  build; otherwise within a stated tolerance -- here, max |difference| <= 2
  per sample and PSNR >= 45 dB, reported per tile -- and the verdict says
  which rule a slide met.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

#: Properties whose value names the library build, not the slide.
VERSIONED = ("openslide.quickhash-1",)


def psnr(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return math.inf if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)


def main():
    truth_dir, js_dir = Path(sys.argv[1]), Path(sys.argv[2])
    write = sys.argv[sys.argv.index("--write") + 1] if "--write" in sys.argv else None
    truth = json.loads((truth_dir / "truth.json").read_text())
    js = json.loads((js_dir / "l2.json").read_text())
    out = {"openslide_python": truth["openslide_python"], "openslide_native": truth["openslide"],
           "openslide_js": js.get("openslideJs"), "chrome": js.get("chrome"), "init": js.get("init"),
           "slides": {}}
    for name, t in truth["slides"].items():
        j = js["slides"].get(name)
        row = {"path": t["path"]}
        out["slides"][name] = row
        if j is None:
            row["status"] = "not run"
            continue
        if "error" in t or "error" in j:
            row["native_error"] = t.get("error")
            row["js_error"] = j.get("error")
            row["status"] = "both fail" if "error" in t and "error" in j else "one fails"
            continue
        o = j["open"]
        meta = {
            "levels": o["levels"] == t["level_count"],
            "dims": o["dims"] == t["level_dimensions"],
            "downsamples": len(o["downsamples"]) == len(t["level_downsamples"]) and all(
                abs(a - b) < 1e-9 for a, b in zip(o["downsamples"], t["level_downsamples"])),
        }
        tp, jp = t["properties"], o["properties"]
        missing = sorted(set(tp) - set(jp))
        extra = sorted(set(jp) - set(tp))
        differ = sorted(k for k in set(tp) & set(jp) if tp[k] != jp[k])
        meta["properties"] = not missing and not extra and not [k for k in differ if k not in VERSIONED]
        row["metadata"] = meta
        row["properties"] = {"count": len(tp), "missing_in_js": missing, "extra_in_js": extra,
                             "differ": {k: [tp[k], jp[k]] for k in differ}}
        row["associated"] = {"native": t["associated"], "js": o["associated"]}
        tiles = []
        for tile in t["tiles"]:
            i = tile["i"]
            a = np.load(truth_dir / name / f"{i:03d}.npy")
            raw = (js_dir / "js" / name / f"{i:03d}.rgba").read_bytes()
            b = np.frombuffer(raw, dtype=np.uint8).reshape(a.shape)
            diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
            tiles.append({"i": i, "level": tile["level"], "identical": bool(np.array_equal(a, b)),
                          "max": int(diff.max()), "differing": float(np.count_nonzero(diff.any(axis=-1)) / diff[..., 0].size),
                          "psnr": psnr(a, b), "alpha_identical": bool(np.array_equal(a[..., 3], b[..., 3]))})
        identical = sum(t_["identical"] for t_ in tiles)
        worst = max((t_["max"] for t_ in tiles), default=0)
        low = min((t_["psnr"] for t_ in tiles), default=math.inf)
        row["pixels"] = {"tiles": len(tiles), "identical": identical, "max_abs": worst,
                         "min_psnr": None if math.isinf(low) else round(low, 2),
                         "alpha_identical": all(t_["alpha_identical"] for t_ in tiles)}
        row["tiles"] = tiles
        row["timing"] = {
            "native_open_ms": round(t["open_ms"], 1), "js_open_ms": round(o["ms"], 1),
            "native_tile_p50": round(t["tile_ms_p50"], 2), "native_tile_p95": round(t["tile_ms_p95"], 2),
            "js_tile_p50": round(j["tileMs"]["p50"], 2), "js_tile_p95": round(j["tileMs"]["p95"], 2),
            "js_warm_p50": round(j["tileMsWarm"]["p50"], 2), "js_warm_p95": round(j["tileMsWarm"]["p95"], 2),
            "js_parallel8_ms_per_tile": round(j["parallel8"]["ms"] / max(1, j["parallel8"]["tiles"]), 2),
        }
        if "tour_first" in t and "tourFirst" in j:
            row["timing"]["tour"] = {
                "tiles": t["tour_first"]["tiles"],
                "native_first_p50": round(t["tour_first"]["p50"], 1), "native_first_p95": round(t["tour_first"]["p95"], 1),
                "native_again_p50": round(t["tour_again"]["p50"], 1), "native_again_p95": round(t["tour_again"]["p95"], 1),
                "js_first_p50": round(j["tourFirst"]["p50"], 1), "js_first_p95": round(j["tourFirst"]["p95"], 1),
                "js_again_p50": round(j["tourAgain"]["p50"], 1), "js_again_p95": round(j["tourAgain"]["p95"], 1),
                "js_spread_ms_per_tile": round(j["tourSpread"]["msPerTile"], 1), "js_spread_opens": j["tourSpread"]["slides"],
                "js_open_more_ms": round(j["openMany"]["ms"], 1),
            }
        if identical == len(tiles) and all(meta.values()):
            row["status"] = "identical"
        elif all(meta.values()) and worst <= 2 and (math.isinf(low) or low >= 45):
            row["status"] = "within tolerance"
        else:
            row["status"] = "differs"
        print(f"{name:12s} {row['status']:17s} meta={all(meta.values())} tiles {identical}/{len(tiles)} identical, "
              f"max {worst}, psnr>={row['pixels']['min_psnr']}; open {row['timing']['native_open_ms']}/"
              f"{row['timing']['js_open_ms']} ms; tile p50 {row['timing']['native_tile_p50']}/{row['timing']['js_tile_p50']}"
              f" p95 {row['timing']['native_tile_p95']}/{row['timing']['js_tile_p95']}")
        tour = row["timing"].get("tour")
        if tour:
            print(f"{'':12s} tour {tour['tiles']} tiles 1024^2: native p50/p95 {tour['native_first_p50']}/{tour['native_first_p95']}"
                  f" (again {tour['native_again_p50']}), js {tour['js_first_p50']}/{tour['js_first_p95']}"
                  f" (again {tour['js_again_p50']}), js over {tour['js_spread_opens']} opens {tour['js_spread_ms_per_tile']} ms/tile")
        if missing or extra or differ:
            print("   properties: missing", missing[:6], "extra", extra[:6], "differ", differ[:6])
    for name, row in out["slides"].items():
        if row.get("status") in ("both fail", "one fails"):
            print(f"{name:12s} {row['status']}: native={row.get('native_error')!r:.90} js={row.get('js_error')!r:.90}")
    if write:
        Path(write).write_text(json.dumps(out, indent=1), newline="\n")


if __name__ == "__main__":
    main()
