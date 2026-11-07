"""Projects whose cell ids pass 2^24 (the envelope's mask-id cell).

float32's last exact integer is 16,777,216, so an id that passes through a
float32 anywhere -- a label tile, the cell table, a pick, a gate -- comes back
as its neighbour. These are small projects (768 x 768, 256 cells) whose ids
are all shifted: `hi24` straddles 2^24, `hi32` sits near 2^32 (uint32's top).
Each folder holds what tests/browser/high_ids.mjs imports:

- image.ome.tif  2 channels (DNA, CD3), uint16, tiled 256, two sub-resolutions
- mask.tif       uint32 ids, the same tiling and levels
- cells.csv      CellID, X_centroid, Y_centroid, Area, DNA, CD3

    python tools/bench_envelope/high_ids.py OUT_DIR
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import tifffile

OFFSETS = {"hi24": (1 << 24) - 128, "hi32": 4_000_000_000}
SIDE = 768
CELL = 48
TILE = 256
LEVELS = 3


def _levels(plane: np.ndarray) -> list[np.ndarray]:
    return [plane[..., ::1 << level, ::1 << level] for level in range(LEVELS)]


def _write(path: Path, levels: list[np.ndarray], **extra) -> None:
    with tifffile.TiffWriter(path) as writer:
        writer.write(levels[0], tile=(TILE, TILE), compression="zlib", subifds=len(levels) - 1, **extra)
        for level in levels[1:]:
            writer.write(level, tile=(TILE, TILE), compression="zlib", subfiletype=1)


def make(folder: Path, offset: int) -> dict:
    """The project for ids offset + 1 .. offset + 256 in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    per_side = SIDE // CELL
    mask = np.zeros((SIDE, SIDE), dtype=np.uint32)
    image = rng.integers(40, 120, (2, SIDE, SIDE), dtype=np.uint16)
    rows = ["CellID,X_centroid,Y_centroid,Area,DNA,CD3"]
    yy, xx = np.mgrid[0:CELL, 0:CELL]
    disc = (yy - CELL / 2 + 0.5) ** 2 + (xx - CELL / 2 + 0.5) ** 2 <= (CELL / 2 - 4) ** 2
    for k in range(per_side * per_side):
        y, x = divmod(k, per_side)
        cell = offset + k + 1
        block = (slice(y * CELL, (y + 1) * CELL), slice(x * CELL, (x + 1) * CELL))
        mask[block][disc] = cell
        dna, cd3 = float(rng.uniform(2000, 3000)), float(rng.choice([rng.uniform(40, 120), rng.uniform(800, 1600)]))
        image[0][block][disc] = int(dna)
        image[1][block][disc] = int(cd3)
        rows.append(f"{cell},{x * CELL + CELL / 2 - 0.5:.3f},{y * CELL + CELL / 2 - 0.5:.3f},"
                    f"{int(disc.sum())},{dna:.4f},{cd3:.4f}")
    _write(folder / "image.ome.tif", _levels(image), photometric="minisblack",
           metadata={"axes": "CYX", "Channel": {"Name": ["DNA", "CD3"]}})
    _write(folder / "mask.tif", _levels(mask), photometric="minisblack", metadata=None)
    (folder / "cells.csv").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    ids = mask[mask > 0]
    return {"offset": offset, "ids": [int(ids.min()), int(ids.max())], "cells": per_side * per_side}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps({name: make(args.out / name, offset) for name, offset in OFFSETS.items()}))


if __name__ == "__main__":
    main()
