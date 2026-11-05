"""Fixtures for the browser-node POCs that do not exist as files already.

    python fixtures.py flat-mask SOURCE OUT [--size N]   # a flat (one-level) label TIFF
    python fixtures.py fingerprint PATH...                # size, mtime, codec, tiles, levels

`flat-mask` copies level 0 of a label pyramid (or a crop of it, N x N from the
top left) into a single-level, 1024-tiled, deflate BigTIFF: what a
segmentation tool hands a user, and what P2b turns back into a pyramid in the
browser. Written strip by strip (1024 rows at a time) so a whole-slide mask is
never held in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import tifffile


def flat_mask(source: str, out: str, size: int | None = None) -> dict:
    t0 = time.perf_counter()
    with tifffile.TiffFile(source) as tf:
        level = tf.series[0].levels[0]
        store = level.aszarr()
        import zarr

        z = zarr.open(store, mode="r")
        if isinstance(z, zarr.Group):
            # A multiscale series opens as a group of levels: level 0 is "0".
            z = z["0"]
        height, width = level.shape[-2:]
        if size:
            height, width = min(height, size), min(width, size)
        # The store's own dtype object: on Windows uint32 has two spellings ("I" and
        # "L") that compare equal and that tifffile's iterator check tells apart.
        dtype = np.asarray(z[0:1, 0:1]).dtype
        tile = 1024

        def tiles():
            for y in range(0, height, tile):
                band = np.asarray(z[y:min(y + tile, height), :width])
                for x in range(0, width, tile):
                    block = band[:, x:x + tile]
                    if block.shape != (tile, tile):
                        padded = np.zeros((tile, tile), dtype=dtype)
                        padded[: block.shape[0], : block.shape[1]] = block
                        block = padded
                    yield np.ascontiguousarray(block)

        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with tifffile.TiffWriter(out, bigtiff=True) as writer:
            writer.write(tiles(), shape=(height, width), dtype=dtype, tile=(tile, tile),
                         compression="zlib", photometric="minisblack", maxworkers=os.cpu_count())
        store.close()
    return {"out": out, "height": height, "width": width, "dtype": str(dtype),
            "bytes": os.path.getsize(out), "rawBytes": int(height) * int(width) * dtype.itemsize,
            "seconds": round(time.perf_counter() - t0, 1)}


def fingerprint(path: str) -> dict:
    s = os.stat(path)
    out = {"name": Path(path).name, "bytes": s.st_size, "mtime": s.st_mtime}
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        out.update(codec=getattr(page.compression, "name", str(page.compression)),
                   predictor=int(page.predictor), tile=[page.tilewidth, page.tilelength],
                   dtype=str(tf.series[0].dtype), shape=list(tf.series[0].shape),
                   levels=len(tf.series[0].levels))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    fm = sub.add_parser("flat-mask")
    fm.add_argument("source")
    fm.add_argument("out")
    fm.add_argument("--size", type=int, default=None)
    fp = sub.add_parser("fingerprint")
    fp.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if args.cmd == "flat-mask":
        print(json.dumps(flat_mask(args.source, args.out, args.size)))
    else:
        print(json.dumps([fingerprint(p) for p in args.paths], indent=1))


if __name__ == "__main__":
    sys.exit(main())
