"""2-D parity: a planar project's tile URLs and settled picture, before and after.

    python tools/bench3d/parity2d.py BEFORE.json AFTER.json [--dataset flat]

The URL sets must be identical (the `v=` cache tag aside) and the pictures may
differ by at most 1/255 on average: a 3-D change must not move a 2-D pixel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def load(path, dataset):
    run = json.loads(Path(path).read_text())
    extra = run["results"][dataset]["parity2d"]["extra"]
    png = extra["png"].replace("$PLEXORA_BENCH_HOME",
                               os.environ.get("PLEXORA_BENCH_HOME", str(Path.home() / "plexora-bench")))
    return set(extra["urls"]), np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--dataset", default="flat")
    args = parser.parse_args(argv)
    urls_a, img_a = load(args.before, args.dataset)
    urls_b, img_b = load(args.after, args.dataset)
    ok = True
    if urls_a != urls_b:
        ok = False
        print("tile URLs differ:")
        for url in sorted(urls_a - urls_b):
            print("  -", url)
        for url in sorted(urls_b - urls_a):
            print("  +", url)
    else:
        print(f"tile URLs identical ({len(urls_a)})")
    if img_a.shape != img_b.shape:
        ok = False
        print("screenshots differ in size", img_a.shape, img_b.shape)
    else:
        diff = float(np.abs(img_a - img_b).mean())
        print(f"screenshot mean |diff| = {diff:.3f} / 255")
        ok = ok and diff <= 1.0
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
