"""Python baseline for P7: Plexora's own pyramid of a flat multichannel image.

    python truth_image.py --image FLAT.ome.tif --out PYRAMID.ome.tiff [--repeat N]

Runs server/utils/image_pyramid.pyramidize_image (what registering a flat
image larger than 4096 px offers to do) on this machine, timed, and prints
{seconds, runs, bytes, levels}. With --repeat the conversion runs N times and
`seconds` is the median (the first run also warms the OS file cache). The
output is what P7 compares the browser's pyramid against, tile by tile. Reads
the flat image; writes only `--out`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)
    from plexora.server.utils import image_pyramid as ip

    runs = []
    for _ in range(max(1, args.repeat)):
        if os.path.exists(args.out):
            os.remove(args.out)
        t0 = time.perf_counter()
        ip.pyramidize_image(args.image, args.out)
        runs.append(time.perf_counter() - t0)
    import tifffile

    with tifffile.TiffFile(args.out) as tf:
        series = tf.series[0]
        levels = [list(level.shape) for level in series.levels]
    print(json.dumps({"seconds": statistics.median(runs), "runs": runs, "bytes": os.path.getsize(args.out),
                      "levels": levels, "workers": ip._workers()}))


if __name__ == "__main__":
    main()
