"""The desktop's own reader (brightfield.open_rgb: tifffile + imagecodecs for a
TIFF-based slide) against OpenSlide on the same regions -- whether moving a
format to openslide-js on the website changes what it draws compared with the
desktop app.

    <plexora env python> -I bench/desktop_vs_openslide_l2.py <truth_dir> <fixtures_dir> name [name ...]

Compares each truth region read at a level whose size the desktop's dyadic
chain holds natively (no resampling), as RGB composited over white.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np


def over_white(rgba):
    alpha = rgba[..., 3:4].astype(np.uint16)
    rgb = rgba[..., :3].astype(np.uint16)
    return ((rgb * alpha + 255 * (255 - alpha)) // 255).astype(np.uint8)


def main():
    truth_dir, fixtures = Path(sys.argv[1]), Path(sys.argv[2])
    names = sys.argv[3:]
    truth = json.loads((truth_dir / "truth.json").read_text())
    from plexora.server.utils import brightfield

    out = {}
    for name in names:
        t = truth["slides"][name]
        path = fixtures / t["path"]
        try:
            pyramid = brightfield.open_rgb(path)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            print(name, "desktop reader fails:", out[name]["error"])
            continue
        shapes = pyramid.level_shapes
        row = {"desktop_levels": shapes, "openslide_levels": t["level_dimensions"], "tiles": []}
        for tile in t["tiles"]:
            ds = t["level_downsamples"][tile["level"]]
            ow, oh = t["level_dimensions"][tile["level"]]
            # The dyadic level of the same size, if the chain has one.
            match = [i for i, (h, w) in enumerate(shapes) if (w, h) == (ow, oh)]
            if not match:
                continue
            level = pyramid[match[0]]
            x0, y0 = int(round(tile["x"] / ds)), int(round(tile["y"] / ds))
            mine = level.rgb[y0:y0 + tile["h"], x0:x0 + tile["w"]]
            theirs = over_white(np.load(truth_dir / name / f"{tile['i']:03d}.npy"))
            if mine.shape != theirs.shape:
                row["tiles"].append({"i": tile["i"], "shape": [list(mine.shape), list(theirs.shape)]})
                continue
            diff = np.abs(mine.astype(np.int16) - theirs.astype(np.int16))
            mse = float(np.mean(diff.astype(np.float64) ** 2))
            row["tiles"].append({"i": tile["i"], "level": tile["level"], "identical": bool(not diff.any()),
                                 "max": int(diff.max()), "psnr": None if mse == 0 else round(10 * math.log10(255 ** 2 / mse), 2)})
        compared = [x for x in row["tiles"] if "identical" in x]
        row["summary"] = {"compared": len(compared), "identical": sum(x["identical"] for x in compared),
                          "max": max((x["max"] for x in compared), default=None),
                          "min_psnr": min((x["psnr"] for x in compared if x["psnr"] is not None), default=None)}
        out[name] = row
        print(name, row["summary"], "desktop levels", shapes[:3], "openslide", t["level_dimensions"][:3])
    return out


if __name__ == "__main__":
    main()
