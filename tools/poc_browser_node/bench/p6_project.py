"""P6's product arm: a Plexora project for the fixture, and three waypoints on tissue.

    python bench/p6_project.py --data DATA_ROOT --image IMG --mask MASK --table CSV [--name lsp11385]

Registers the project with plexora.datasets.create_project in DATA_ROOT (the
bench's scratch root, never the user's) unless it is there already, and prints
{name, waypoints: {a, b, c}} -- the centroids of the cells at 30 %, 60 % and
85 % along X, so every waypoint is on tissue and they are far apart.
"""

from __future__ import annotations

import argparse
import json
import os


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--mask", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--name", default="lsp11385")
    args = parser.parse_args(argv)
    os.environ["PLEXORA_DATA_PATH"] = args.data
    os.environ.setdefault("PLEXORA_TELEMETRY", "off")
    import polars as pl

    from plexora.datasets import create_project

    if not os.path.isdir(os.path.join(args.data, args.name)):
        create_project(args.image, name=args.name, segmentation=args.mask, data=args.table,
                       cell_id="CellID", x="X_centroid", y="Y_centroid", exist_ok=True)
    cells = pl.read_csv(args.table, columns=["X_centroid", "Y_centroid"]).sort("X_centroid")
    pick = lambda q: [round(float(v), 1) for v in cells.row(int(q * (cells.height - 1)))]
    print(json.dumps({"name": args.name, "waypoints": {"a": pick(0.30), "b": pick(0.60), "c": pick(0.85)}}))


if __name__ == "__main__":
    main()
