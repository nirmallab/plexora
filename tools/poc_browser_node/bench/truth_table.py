"""Python ground truth for P3: the answers the server would give, for queries
the page will be asked.

    python truth_table.py --csv PATH --cell-id CellID --x X_centroid --y Y_centroid
                          --out FILE [--seed 1]

- polars' own read of the file, timed (the "local Python" load baseline);
- 200 seeded points -> the nearest row (scipy cKDTree over float64 x, y, as
  server/utils/spatial_index.SpatialIndex.nearest);
- 20 seeded polygons of 10/100/1000 vertices -> the rows strictly within
  (shapely STRtree `within`, as plugins/roi/server/mapping.assign);
- 20 seeded gates -> rows with low < v <= high in float32
  (data_model.apply_range_mask, labelGpu.evaluateGateMask).

Read-only: the CSV is only read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time

import numpy as np


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--x", required=True)
    parser.add_argument("--y", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    import polars as pl
    import shapely
    from scipy.spatial import cKDTree

    t0 = time.perf_counter()
    frame = pl.read_csv(args.csv)
    read_ms = (time.perf_counter() - t0) * 1000.0
    xs = frame[args.x].to_numpy().astype("float64")
    ys = frame[args.y].to_numpy().astype("float64")
    rng = np.random.default_rng(args.seed)

    t0 = time.perf_counter()
    tree = cKDTree(np.column_stack([xs, ys]))
    tree_ms = (time.perf_counter() - t0) * 1000.0
    points = np.column_stack([rng.uniform(xs.min(), xs.max(), 200), rng.uniform(ys.min(), ys.max(), 200)])
    t0 = time.perf_counter()
    distance, row = tree.query(points, k=1)
    nearest_ms = (time.perf_counter() - t0) * 1000.0
    nearest = [{"x": float(p[0]), "y": float(p[1]), "row": int(r), "distance": float(d)}
               for p, r, d in zip(points, row, distance)]

    polygons = []
    sizes = [10] * 7 + [100] * 7 + [1000] * 6
    centres = rng.integers(0, len(xs), len(sizes))
    for vertices, centre in zip(sizes, centres):
        radius = rng.uniform(200, 2000)
        angles = np.sort(rng.uniform(0, 2 * np.pi, vertices))
        radii = radius * rng.uniform(0.5, 1.0, vertices)
        ring = np.column_stack([xs[centre] + radii * np.cos(angles), ys[centre] + radii * np.sin(angles)])
        polygons.append(ring)
    t0 = time.perf_counter()
    tree_p = shapely.STRtree([shapely.Polygon(r) for r in polygons])
    pts = shapely.points(xs, ys)
    point_index, part_index = tree_p.query(pts, predicate="within")
    roi_ms = (time.perf_counter() - t0) * 1000.0
    rois = []
    for i, ring in enumerate(polygons):
        rows = np.sort(point_index[part_index == i])
        rois.append({"vertices": len(ring), "polygon": ring.round(3).tolist(), "count": int(rows.size),
                     "rowsSha256": hashlib.sha256(rows.astype("<u4").tobytes()).hexdigest(),
                     "rows": rows.tolist() if rows.size <= 5000 else None})

    numeric = [c for c, d in frame.schema.items() if d.is_numeric() and c not in (args.cell_id, args.x, args.y)]
    gates = []
    for _ in range(20):
        column = numeric[int(rng.integers(0, len(numeric)))]
        values = frame[column].to_numpy().astype("float32")
        finite = values[np.isfinite(values)]
        low, high = np.sort(rng.choice(np.percentile(finite, [5, 25, 50, 75, 95, 99.9]), 2, replace=False))
        low32, high32 = np.float32(low), np.float32(high)
        count = int(np.count_nonzero((values > low32) & (values <= high32)))
        gates.append({"column": column, "low": float(low), "high": float(high), "count": count})

    out = {
        "csv": args.csv, "rows": frame.height, "columns": frame.width,
        "polarsReadMs": read_ms, "kdtreeBuildMs": tree_ms, "nearest200Ms": nearest_ms, "roi20Ms": roi_ms,
        "nearest": nearest, "rois": rois, "gates": gates,
        "markers": numeric,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(json.dumps({k: out[k] for k in ("rows", "columns", "polarsReadMs", "kdtreeBuildMs", "nearest200Ms", "roi20Ms")}))


if __name__ == "__main__":
    main()
