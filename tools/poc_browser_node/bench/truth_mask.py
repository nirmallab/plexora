"""Python baseline for P2b: Plexora's own conversion of a flat label mask.

    python truth_mask.py --mask FLAT.tif --out PYRAMID.ome.tiff

Runs server/utils/segmentation_pyramid.pyramidize_segmentation_mask in FILLED
mode (what an import of a filled mask does: data_model.ensure_segmentation's
`outline=mode != MODE_FILLED`) on this machine, timed, and prints
{seconds, bytes, levels}. The output is what a node then serves for P2b's
tile-by-tile identity check. Reads the flat mask; writes only `--out`.
"""

from __future__ import annotations

import argparse
import json
import os
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mask", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    from plexora.server.utils import segmentation_pyramid as sp

    t0 = time.perf_counter()
    sp.pyramidize_segmentation_mask(args.mask, args.out, overwrite=True, outline=False)
    seconds = time.perf_counter() - t0
    import tifffile

    with tifffile.TiffFile(args.out) as tf:
        levels = [list(level.shape) for level in tf.series[0].levels]
    print(json.dumps({"seconds": seconds, "bytes": os.path.getsize(args.out), "levels": levels,
                      "fullReadBudget": sp._full_read_budget()}))


if __name__ == "__main__":
    main()
