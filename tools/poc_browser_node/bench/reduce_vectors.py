"""Vectors for tests/reduce.test.mjs: Python's own pyramid arithmetic on small blocks.

    python bench/reduce_vectors.py > tests/reduce_vectors.json

For every dtype a flat file can hold and blocks with odd and even sides, the
cascade image_pyramid.reduce2 makes (level k from level k-1) and the
nearest-neighbour levels segmentation_pyramid's FILLED mode takes (level 0's
pixel (y * 2^L, x * 2^L)). Values are drawn to hit the rounding cases: sums
landing on .5 (round half to even), negatives, and the dtype's extremes.
"""

from __future__ import annotations

import json
import sys

import numpy as np

from plexora.server.utils.image_pyramid import reduce2

DTYPES = ["uint8", "uint16", "uint32", "int8", "int16", "int32", "float32"]
SHAPES = [(1, 1), (2, 2), (3, 5), (7, 4), (8, 8), (9, 11), (16, 13)]


def values(rng, dtype, shape):
    dtype = np.dtype(dtype)
    if dtype.kind == "f":
        out = rng.normal(0, 1000, size=shape).astype(dtype)
        out.flat[0] = 1e30
        return out
    info = np.iinfo(dtype)
    out = rng.integers(info.min, int(info.max) + 1, size=shape, dtype=np.int64)
    # Small values make sums of 4 land on every remainder, ties included.
    small = rng.integers(max(info.min, -6), 7, size=shape)
    out = np.where(rng.random(shape) < 0.5, small, out).astype(dtype)
    out.flat[0] = info.max
    if out.size > 1:
        out.flat[1] = info.min
    return out


def main():
    rng = np.random.default_rng(20261105)
    cases = []
    for dtype in DTYPES:
        for shape in SHAPES:
            block = values(rng, dtype, shape)
            cascade = []
            level = block
            while level.shape != (1, 1):
                level = reduce2(level, np.dtype(dtype))
                cascade.append(level.tolist())
            factors = [2 ** k for k in range(1, len(cascade) + 1)]
            nearest = [block[::f, ::f].tolist() for f in factors]
            cases.append({"dtype": dtype, "shape": list(shape), "block": block.tolist(),
                          "cascade": cascade, "factors": factors, "nearest": nearest})
    json.dump({"source": "plexora.server.utils.image_pyramid.reduce2 and [::f, ::f]",
               "cases": cases}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
