"""Projects with a large cell table (the envelope's table cells).

The website hands a project's table to Python in its worker, whose
WebAssembly memory is at most 4 GiB; client/src/envelope.json's `table`
limit (`worker_values`, cells x columns) says where the website stops
recommending itself. These projects put a table either side of that limit:
a small 2-channel image (1024 x 1024) and a CSV of `rows` cells with
CellID, X_centroid, Y_centroid, Area and `columns - 4` marker columns
(float, four decimals, as a quantification table writes them).

    python tools/bench_envelope/big_table.py OUT_DIR --rows 1200000 --columns 32
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl
import tifffile

SIDE = 1024


def make(folder: Path, rows: int, columns: int, seed: int = 5) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    image = rng.integers(50, 400, (2, SIDE, SIDE), dtype=np.uint16)
    tifffile.imwrite(folder / "image.ome.tif", image, photometric="minisblack", tile=(256, 256),
                     compression="zlib", metadata={"axes": "CYX", "Channel": {"Name": ["DNA", "CD3"]}})
    markers = columns - 4
    data = {
        "CellID": np.arange(1, rows + 1, dtype=np.int64),
        "X_centroid": rng.uniform(0, SIDE, rows).astype(np.float32),
        "Y_centroid": rng.uniform(0, SIDE, rows).astype(np.float32),
        "Area": rng.integers(40, 400, rows).astype(np.int32),
    }
    names = ["DNA", "CD3"] + [f"M{i:02d}" for i in range(markers - 2)]
    for name in names:
        data[name] = rng.lognormal(5, 1, rows).astype(np.float32)
    frame = pl.DataFrame(data)
    frame.write_csv(folder / "cells.csv", float_precision=4)
    return {"rows": rows, "columns": columns, "values": rows * columns,
            "csv_bytes": (folder / "cells.csv").stat().st_size}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--columns", type=int, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(make(args.out, args.rows, args.columns)))


if __name__ == "__main__":
    main()
