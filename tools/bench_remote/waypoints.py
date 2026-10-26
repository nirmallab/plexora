"""Waypoint sets for the tour, picked from a survey picture of the image.

    python waypoints.py survey.json --sets 6 --out waypoints.json [--seed 20261026]

A waypoint is the centre of a level-0 view (one image pixel per screen
pixel, so a 1440 x 900 window shows 1440 x 900 image pixels). Candidates are
the cells of a grid of view-sized windows whose survey brightness (the
nuclear channel) is in the top half of the tissue; each set takes three --
`a` (zoomed into twice), `b` (a distant pan from a) and `c` (an unvisited
jump) -- at least `min_gap` pixels apart, and no window is used by two sets,
so no block's level-0 tiles are another block's. Deterministic for a seed.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image

VIEW = (1440, 900)


def tissue_cells(survey: dict, view=VIEW):
    """[(x, y, score)] for every view-sized window of the image that the
    survey picture shows, centred, scored by mean brightness."""
    info = survey["info"]
    picture = np.asarray(Image.open(io.BytesIO(base64.b64decode(survey["png"]))).convert("L"),
                         dtype=np.float32)
    (x0, y0), (x1, y1) = info["topLeft"], info["bottomRight"]
    width, height = info["size"]
    h, w = picture.shape
    sx, sy = w / (x1 - x0), h / (y1 - y0)            # screen px per image px
    cells = []
    nx, ny = int(width // view[0]), int(height // view[1])
    ox = (width - nx * view[0]) / 2
    oy = (height - ny * view[1]) / 2
    for j in range(ny):
        for i in range(nx):
            cx = ox + (i + 0.5) * view[0]
            cy = oy + (j + 0.5) * view[1]
            px0 = int((cx - view[0] / 2 - x0) * sx)
            px1 = int((cx + view[0] / 2 - x0) * sx)
            py0 = int((cy - view[1] / 2 - y0) * sy)
            py1 = int((cy + view[1] / 2 - y0) * sy)
            patch = picture[max(0, py0):max(0, py1), max(0, px0):max(0, px1)]
            score = float(patch.mean()) if patch.size else 0.0
            cells.append((int(cx), int(cy), score))
    return cells


def pick_sets(cells, sets: int, seed: int = 20261026, min_gap: float = 2048.0,
              share: float = 0.7):
    """`sets` triples from the brightest `share` of the cells. A window is
    used once while distinct ones remain; after that (a small image) the
    least-used ones are reused, and the report says how many were."""
    ranked = sorted(cells, key=lambda c: -c[2])
    pool = [c for c in ranked[:max(3, int(len(ranked) * share))] if c[2] > 0]
    rng = random.Random(seed)
    rng.shuffle(pool)
    uses = {(c[0], c[1]): 0 for c in pool}
    out = []
    for _ in range(sets):
        order = sorted(pool, key=lambda c: uses[(c[0], c[1])])   # stable: keeps the shuffle
        chosen = []
        gap = min_gap
        while len(chosen) < 3 and gap > 400:
            chosen = []
            for cell in order:
                if all(math.dist(cell[:2], other[:2]) >= gap for other in chosen):
                    chosen.append(cell)
                    if len(chosen) == 3:
                        break
            if len(chosen) < 3:
                gap *= 0.8
        for cell in chosen:
            uses[(cell[0], cell[1])] += 1
        out.append({"a": list(chosen[0][:2]), "b": list(chosen[1][:2]), "c": list(chosen[2][:2]),
                    "scores": [round(c[2], 1) for c in chosen], "gap": round(gap)})
    reused = sum(max(0, n - 1) for n in uses.values())
    return out, reused


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("survey")
    parser.add_argument("--sets", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    survey = json.loads(Path(args.survey).read_text(encoding="utf-8"))
    cells = tissue_cells(survey)
    sets, reused = pick_sets(cells, args.sets, args.seed)
    Path(args.out).write_text(json.dumps({"seed": args.seed, "view": VIEW, "reused_windows": reused,
                                          "size": survey["info"]["size"], "sets": sets},
                                         indent=1), encoding="utf-8")
    print(json.dumps({"sets": len(sets), "reused_windows": reused}))


if __name__ == "__main__":
    main()
