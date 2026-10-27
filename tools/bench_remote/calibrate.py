"""CPU calibration, run identically on the laptop and on the compute node.

    python calibrate.py [reps] [image]

Times Plexora's own tile encoder on a fixed, seeded 1024 x 1024 uint16 tile
(HD: 16-bit PNG; fast: 8-bit WebP) and one LZ4 decode of a seeded chunk the
size of the image's level-0 chunks, so a per-tile CPU cost measured on one
host can be compared with the other. Prints one JSON line (the last line).
`image` is optional: when given and readable, the first level-0 chunk read
through h5py is timed as well (the node only; the laptop does not have it).
"""

import json
import os
import platform
import statistics
import sys
import time

import numpy as np


def timed(fn, reps):
    fn()                                   # warm: imports, allocations
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(out), 3), "min_ms": round(min(out), 3),
            "reps": reps}


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    image = sys.argv[2] if len(sys.argv) > 2 else None
    from plexora.server.models import data_model

    rng = np.random.default_rng(20261026)
    # A tissue-like tile: smooth background plus sparse bright cells.
    base = rng.gamma(2.0, 300.0, size=(1024, 1024)).astype(np.float32)
    cells = rng.random((1024, 1024)) > 0.995
    base[cells] += 20000
    tile = np.clip(base, 0, 65535).astype(np.uint16)
    out = {"host": platform.node(), "machine": platform.machine(),
           "processor": platform.processor(), "cpus": os.cpu_count(),
           "python": platform.python_version()}
    out["encode_hd"] = timed(lambda: data_model.encode_tile_array(tile, False, "hd"), reps)
    out["encode_fast"] = timed(
        lambda: data_model.encode_tile_array(tile, False, "fast", qmin=0, qmax=8000), reps)
    out["hd_bytes"] = len(data_model.encode_tile_array(tile, False, "hd")[0])
    out["fast_bytes"] = len(data_model.encode_tile_array(tile, False, "fast", qmin=0, qmax=8000)[0])
    try:
        import lz4.block as lz4block

        chunk = np.ascontiguousarray(
            np.repeat(tile[None, :256, :256], 8, axis=0))         # (8, 256, 256) uint16
        packed = lz4block.compress(chunk.tobytes())
        out["lz4_decode_chunk"] = timed(lambda: lz4block.decompress(packed), reps)
    except ImportError:
        out["lz4_decode_chunk"] = None
    if image and os.path.exists(image):
        try:
            import h5py

            with h5py.File(image, "r") as handle:
                data = handle["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/Data"]
                t0 = time.perf_counter()
                data[128:136, 0:256, 0:256]
                out["h5_first_chunk_ms"] = round((time.perf_counter() - t0) * 1000, 3)
                out["h5_chunk"] = timed(lambda: data[128:136, 1024:1280, 1024:1280], max(5, reps // 3))
        except Exception as error:  # noqa: BLE001 -- calibration is best effort
            out["h5_error"] = repr(error)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
