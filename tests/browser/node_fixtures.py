"""Images for tests/browser/node_parity.mjs: the TIFF layouts the website's
browser node may meet, each small enough to compare every tile of.

    python tests/browser/node_fixtures.py OUT_DIR

Writes, if absent:
- pyr16_zlib.ome.tif   3 channels, uint16, 3000 x 2200, tiled 512, deflate with
                       the horizontal predictor, two sub-resolutions
- pyr8_lzw.ome.tif     2 channels, uint8, 2100 x 1500, tiled 256, LZW, one
- pyr16_raw.ome.tif    1 channel, uint16, 1300 x 1100, tiled 256, uncompressed
- pyr8_jpeg.ome.tif    1 channel, uint8, 1800 x 1200, tiled 256, JPEG, one
                       (lossy: the page must leave it to Python)
- pyr16_zstd.ome.tif   2 channels, uint16, 1700 x 1300, tiled 512, Zstandard, one
- pyr8_packbits.ome.tif 1 channel, uint8, 1500 x 1100, tiled 256, PackBits, one
- flat16.tif           2 channels, uint16, 1800 x 1800, stripped, no pyramid
                       (the page must leave it to Python)
Prints the paths as JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import tifffile


def _planes(channels: int, height: int, width: int, dtype) -> np.ndarray:
    """Gradients, rings and noise per channel: a mismatch shows wherever it is."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    top = np.iinfo(dtype).max
    out = np.empty((channels, height, width), dtype=dtype)
    for c in range(channels):
        field = (np.sin(x / (37.0 + 11 * c)) + np.cos(y / (23.0 + 7 * c)) + 2.0) / 4.0
        field += (x + y * (c + 1)) / (width + height * (c + 1)) * 0.5
        field = field / field.max() * 0.9 + rng.random((height, width), dtype=np.float32) * 0.1
        out[c] = (field * top).astype(dtype)
    return out


def _pyramid(path: Path, data: np.ndarray, *, tile: int, levels: int, **options) -> None:
    with tifffile.TiffWriter(path, bigtiff=False, ome=True) as tif:
        tif.write(data, subifds=levels, tile=(tile, tile), metadata={"axes": "CYX"}, **options)
        level = data
        for _ in range(levels):
            level = level[:, ::2, ::2]
            tif.write(level, subfiletype=1, tile=(tile, tile), **options)


def main(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    path = out / "pyr16_zlib.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(3, 2200, 3000, np.uint16), tile=512, levels=2,
                 compression="zlib", predictor=True)
    made["pyr16_zlib"] = path
    path = out / "pyr8_lzw.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(2, 1500, 2100, np.uint8), tile=256, levels=1, compression="lzw")
    made["pyr8_lzw"] = path
    path = out / "pyr16_raw.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1100, 1300, np.uint16), tile=256, levels=1)
    made["pyr16_raw"] = path
    path = out / "pyr8_jpeg.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1200, 1800, np.uint8), tile=256, levels=1,
                 compression="jpeg", compressionargs={"level": 90})
    made["pyr8_jpeg"] = path
    path = out / "pyr16_zstd.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(2, 1300, 1700, np.uint16), tile=512, levels=1, compression="zstd")
    made["pyr16_zstd"] = path
    path = out / "pyr8_packbits.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1100, 1500, np.uint8), tile=256, levels=1, compression="packbits")
    made["pyr8_packbits"] = path
    path = out / "flat16.tif"
    if not path.exists():
        tifffile.imwrite(path, _planes(2, 1800, 1800, np.uint16), metadata={"axes": "CYX"})
    made["flat16"] = path
    return {name: str(p) for name, p in made.items()}


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
