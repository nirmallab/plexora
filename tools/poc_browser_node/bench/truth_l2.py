"""openslide-python's view of each fixture: levels, dimensions, downsamples,
properties, associated images, and a fixed set of read_region tiles (raw RGBA,
saved as .npy) with timings. The truth L2's comparisons are held to.

A second set, `tour` (viewer-sized 1024x1024 tiles over the middle of the
scanned area at level 0 and near 4x), is only timed: first touch, then again.

Usage: <venv with openslide-python + openslide-bin>/python -I bench/truth_l2.py <fixtures_dir> <out_dir> <name=path> ...
"""
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import openslide


def regions(slide, count=24, size=512, seed=7):
    """A fixed set of (level, x0, y0, w, h) per slide: the level's centre, its
    corner, and pseudo-random places, on every level."""
    rng = np.random.default_rng(seed)
    out = []
    for level in range(slide.level_count):
        w, h = slide.level_dimensions[level]
        ds = slide.level_downsamples[level]
        tw, th = min(size, w), min(size, h)
        picks = [((w - tw) // 2, (h - th) // 2), (0, 0)]
        for _ in range(max(1, count // slide.level_count)):
            picks.append((int(rng.integers(0, max(1, w - tw + 1))), int(rng.integers(0, max(1, h - th + 1)))))
        for lx, ly in picks:
            out.append((level, int(round(lx * ds)), int(round(ly * ds)), tw, th))
    return out


def tour(slide, size=1024, side=6):
    """The timing set: a side x side grid of viewer-sized tiles around the
    middle of the scanned area (openslide.bounds-*, else the whole level), at
    level 0 and at the level nearest a 4x downsample -- where a viewer reads."""
    props = slide.properties
    bx = int(props.get("openslide.bounds-x", 0))
    by = int(props.get("openslide.bounds-y", 0))
    bw = int(props.get("openslide.bounds-width", slide.dimensions[0]))
    bh = int(props.get("openslide.bounds-height", slide.dimensions[1]))
    cx, cy = bx + bw // 2, by + bh // 2
    out = []
    levels = sorted({0, slide.get_best_level_for_downsample(4.01)})
    for level in levels:
        ds = slide.level_downsamples[level]
        w, h = slide.level_dimensions[level]
        x0 = max(0, min(w - size * side, int(cx / ds) - size * side // 2))
        y0 = max(0, min(h - size * side, int(cy / ds) - size * side // 2))
        for row in range(side):
            for col in range(side):
                lx, ly = x0 + col * size, y0 + row * size
                tw, th = min(size, w - lx), min(size, h - ly)
                if tw > 0 and th > 0:
                    out.append((level, int(round(lx * ds)), int(round(ly * ds)), tw, th))
    return out


def main():
    fixtures = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    record = {"openslide_python": openslide.__version__, "openslide": openslide.__library_version__, "slides": {}}
    for spec in sys.argv[3:]:
        name, rel = spec.split("=", 1)
        path = fixtures / rel
        entry = {"path": rel}
        t0 = time.perf_counter()
        try:
            slide = openslide.OpenSlide(str(path))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            record["slides"][name] = entry
            print(name, "ERROR", entry["error"], flush=True)
            continue
        entry["open_ms"] = (time.perf_counter() - t0) * 1000
        entry["vendor"] = openslide.OpenSlide.detect_format(str(path))
        entry["level_count"] = slide.level_count
        entry["level_dimensions"] = [list(d) for d in slide.level_dimensions]
        entry["level_downsamples"] = list(slide.level_downsamples)
        entry["properties"] = dict(slide.properties)
        entry["associated"] = sorted(slide.associated_images.keys())
        tiles = []
        times = []
        tdir = out / name
        tdir.mkdir(exist_ok=True)
        for i, (level, x, y, w, h) in enumerate(regions(slide)):
            t1 = time.perf_counter()
            img = slide.read_region((x, y), level, (w, h))
            ms = (time.perf_counter() - t1) * 1000
            times.append(ms)
            arr = np.asarray(img)  # RGBA, straight alpha (openslide-python un-premultiplies)
            np.save(tdir / f"{i:03d}.npy", arr)
            tiles.append({"i": i, "level": level, "x": x, "y": y, "w": w, "h": h, "ms": ms,
                          "sha": hashlib.sha256(arr.tobytes()).hexdigest()[:16]})
        entry["tiles"] = tiles
        entry["tile_ms_p50"] = statistics.median(times)
        entry["tile_ms_p95"] = float(np.percentile(times, 95))
        # The timing set, read twice (first touch, then again), one at a time.
        regions_t = tour(slide)
        entry["tour"] = [{"level": lv, "x": x, "y": y, "w": w, "h": h} for lv, x, y, w, h in regions_t]
        for label in ("tour_first", "tour_again"):
            ms_list = []
            for lv, x, y, w, h in regions_t:
                t1 = time.perf_counter()
                slide.read_region((x, y), lv, (w, h))
                ms_list.append((time.perf_counter() - t1) * 1000)
            entry[label] = {"p50": statistics.median(ms_list), "p95": float(np.percentile(ms_list, 95)),
                            "total_ms": sum(ms_list), "tiles": len(ms_list)}
        slide.close()
        record["slides"][name] = entry
        print(name, entry["level_count"], entry["level_dimensions"][0], f"open {entry['open_ms']:.0f} ms",
              f"tile p50 {entry['tile_ms_p50']:.1f} p95 {entry['tile_ms_p95']:.1f}",
              f"tour first p50 {entry['tour_first']['p50']:.1f} p95 {entry['tour_first']['p95']:.1f}",
              f"again p50 {entry['tour_again']['p50']:.1f}", flush=True)
    (out / "truth.json").write_text(json.dumps(record, indent=1), newline="\n")


if __name__ == "__main__":
    main()
