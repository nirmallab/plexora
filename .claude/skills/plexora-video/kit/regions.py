"""Find camera fields worth filming for a gated marker, from the cell table.

    python regions.py <cells.csv> MARKER=GATE --width 2000 [--pos 0.25 0.6]
                      [--aspect 1.5] [--ref OTHER=GATE --refpos 0.1 0.5] [--top 3]

A good field has tissue edge to edge (coverage of a 6x4 grid), a clear mix of
gated and ungated cells (positive fraction inside --pos), well separated
populations, few cells within 0.2 of the gate, and dense cells. Prints the
best non-overlapping fields as image-px centres: use [cx, cy, width] as an
imagePoses entry. --aspect is the viewer canvas width/height on screen
(1540/1026 for the default 1920x1080 layout with the sidebar open).
Centroid columns X_centroid / Y_centroid (full-resolution px).
"""
import argparse
import json

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("marker")
ap.add_argument("--width", type=float, required=True)
ap.add_argument("--pos", type=float, nargs=2, default=(0.25, 0.6))
ap.add_argument("--aspect", type=float, default=1540 / 1026)
ap.add_argument("--ref")
ap.add_argument("--refpos", type=float, nargs=2, default=(0.1, 0.5))
ap.add_argument("--step", type=float, default=200)
ap.add_argument("--top", type=int, default=3)
a = ap.parse_args()

m, g = a.marker.split("="); g = float(g)
cols = ["X_centroid", "Y_centroid", m]
if a.ref:
    rm, rg = a.ref.split("="); rg = float(rg); cols.append(rm)
d = pd.read_csv(a.csv, usecols=cols)
X, Y, v = d.X_centroid.to_numpy(), d.Y_centroid.to_numpy(), np.log1p(d[m].to_numpy(float))
rv = np.log1p(d[rm].to_numpy(float)) if a.ref else None
w = a.width; h = w / a.aspect
res = []
for cx in np.arange(w / 2, X.max() - w / 2, a.step):
    selx = (X > cx - w / 2) & (X < cx + w / 2)
    if selx.sum() < 50:
        continue
    for cy in np.arange(h / 2, Y.max() - h / 2, a.step):
        s = selx & (Y > cy - h / 2) & (Y < cy + h / 2)
        n = int(s.sum())
        if n < 150:
            continue
        gx = ((X[s] - (cx - w / 2)) / w * 6).astype(int).clip(0, 5)
        gy = ((Y[s] - (cy - h / 2)) / h * 4).astype(int).clip(0, 3)
        occ = np.bincount(gx * 4 + gy, minlength=24)
        cover = float((occ >= max(3, n / 24 * 0.25)).mean())
        vv = v[s]; pos = vv >= g; pf = float(pos.mean())
        if not (a.pos[0] <= pf <= a.pos[1]) or pos.sum() < 15:
            continue
        if a.ref:
            rf = float((rv[s] >= rg).mean())
            if not (a.refpos[0] <= rf <= a.refpos[1]):
                continue
        amb = float((np.abs(vv - g) < 0.2).mean())
        sep = float(np.median(vv[pos]) - np.median(vv[~pos]))
        score = cover * 2 + sep - amb * 3 + min(n / (w * h) * 1e5, 1) * 0.5
        res.append((score, cx, cy, n, cover, pf, amb, sep))
res.sort(reverse=True)
best = []
for r in res:
    if all(abs(r[1] - o[1]) > w or abs(r[2] - o[2]) > h for o in best):
        best.append(r)
    if len(best) >= a.top:
        break
print(json.dumps([dict(pose=[round(r[1]), round(r[2]), w], cells=r[3], cover=round(r[4], 2), pos_frac=round(r[5], 2),
                       near_gate=round(r[6], 3), separation=round(r[7], 2)) for r in best], indent=1))
