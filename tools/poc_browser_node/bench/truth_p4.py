"""P4's Python side: the reads Plexora makes of an .h5ad and a SpatialData store,
with h5py and zarr-python, timed, and the SHA-256 of every answer's bytes.

    python bench/truth_p4.py --h5ad A.h5ad [B.h5ad ...] --sdata S.zarr --out truth.json [--repeat 3]

Each answer is hashed in the byte layout the browser produces, so the page can
compare without moving arrays: float64/float32/int8 C-order little-endian
bytes; strings joined with "\\n" as UTF-8. The X row block is the first
64 MB of rows (Plexora's adapter reads row blocks of that size); the X column
is feature 5, read whole (the adapter's per-column path); a CSR block or
column is densified to float32. `seconds` is the median of --repeat runs (the
first also warms the OS file cache, as the browser's run then finds it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time

import numpy as np

ROW_BLOCK_BYTES = 64 * 1024 * 1024
COLUMN = 5


def sha(data) -> str:
    if isinstance(data, (list, tuple)):
        data = "\n".join(str(s) for s in data).encode("utf-8")
    elif isinstance(data, np.ndarray):
        data = np.ascontiguousarray(data).tobytes()
    return hashlib.sha256(data).hexdigest()


def strings(values) -> list[str]:
    return [v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in values]


def string_node(node) -> list[str]:
    """A string array as anndata writes it: a dataset (`string-array`), or
    since anndata 0.11 a `nullable-string-array` group of values and mask."""
    if hasattr(node, "shape"):
        return strings(node[:])
    return strings(node["values"][:])


def timed(fn, repeat):
    runs, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        runs.append((time.perf_counter() - t0) * 1000)
    return out, statistics.median(runs)


def h5ad_truth(path: str, repeat: int) -> dict:
    import h5py
    import hdf5plugin  # noqa: F401  (lzf is built in; blosc/zstd come from here)

    out = {"file": path, "ops": {}}
    with h5py.File(path, "r") as f:
        x = f["X"]
        csr = isinstance(x, h5py.Group)
        n_obs = len(string_node(f["obs"][f["obs"].attrs["_index"]]))
        n_vars = len(string_node(f["var"][f["var"].attrs["_index"]]))
        rows = max(1, ROW_BLOCK_BYTES // (n_vars * 4))
        rows = min(rows, n_obs)
        out.update(n_obs=n_obs, n_vars=n_vars, layout="csr" if csr else "dense", rowBlock=rows,
                   codec=(x["data"] if csr else x).compression or "none")

        def obs_names():
            return string_node(f["obs"][f["obs"].attrs["_index"]])

        def obs_numeric():
            return f["obs"]["X_centroid"][:].astype(np.float64)

        def obs_categorical():
            g = f["obs"]["phenotype"]
            return g["codes"][:], string_node(g["categories"])

        def obsm_spatial():
            return f["obsm"]["spatial"][:].astype(np.float64)

        def var_names():
            return string_node(f["var"][f["var"].attrs["_index"]])

        def x_rows():
            if not csr:
                return x[0:rows].astype(np.float32)
            ptr = x["indptr"][0:rows + 1]
            data = x["data"][ptr[0]:ptr[-1]]
            idx = x["indices"][ptr[0]:ptr[-1]]
            dense = np.zeros((rows, n_vars), dtype=np.float32)
            row = np.repeat(np.arange(rows), np.diff(ptr))
            dense[row, idx] = data
            return dense

        def x_column():
            if not csr:
                return x[:, COLUMN].astype(np.float32)
            ptr = x["indptr"][:]
            idx = x["indices"][:]
            data = x["data"][:]
            col = np.zeros(n_obs, dtype=np.float32)
            hit = idx == COLUMN
            row = np.repeat(np.arange(n_obs), np.diff(ptr))
            col[row[hit]] = data[hit]
            return col

        def x_all():
            # All of X in row blocks, as columns (the one pass that would fill a
            # column store): float32, feature-major.
            out = np.empty((n_vars, n_obs), dtype=np.float32)
            ptr = x["indptr"][:] if csr else None
            for r0 in range(0, n_obs, rows):
                r1 = min(n_obs, r0 + rows)
                if not csr:
                    out[:, r0:r1] = x[r0:r1].T
                    continue
                data = x["data"][ptr[r0]:ptr[r1]]
                idx = x["indices"][ptr[r0]:ptr[r1]]
                block = np.zeros((n_vars, r1 - r0), dtype=np.float32)
                row = np.repeat(np.arange(r1 - r0), np.diff(ptr[r0:r1 + 1]))
                block[idx, row] = data
                out[:, r0:r1] = block
            return out

        for name, fn in (("obs_names", obs_names), ("obs_numeric", obs_numeric),
                         ("obs_categorical", obs_categorical), ("obsm_spatial", obsm_spatial),
                         ("var_names", var_names), ("x_rows", x_rows), ("x_column", x_column),
                         ("x_all", x_all)):
            value, ms = timed(fn, repeat)
            if name == "obs_categorical":
                digest = {"codes": sha(value[0].astype(np.int8)), "categories": sha(value[1])}
            else:
                digest = sha(value)
            out["ops"][name] = {"ms": ms, "sha256": digest}
    return out


def sdata_truth(path: str, repeat: int) -> dict:
    import pyarrow.parquet as pq
    import zarr

    root = zarr.open_group(path, mode="r")
    t = root["tables"]["table"]
    out = {"path": path, "ops": {}}

    def table_obs():
        index = string_node(t["obs"][t["obs"].attrs["_index"]])
        inst = t["obs"]["instance_id"][:].astype(np.float64)
        codes = t["obs"]["phenotype"]["codes"][:].astype(np.int8)
        cats = string_node(t["obs"]["phenotype"]["categories"])
        return {"index": sha(index), "instance_id": sha(inst), "codes": sha(codes), "categories": sha(cats)}

    def table_obsm():
        return sha(t["obsm"]["spatial"][:].astype(np.float64))

    def table_x():
        return sha(t["X"][:].astype(np.float32))

    def image_chunk():
        a = root["images"]["he_like"]["s0"]
        return {"shape": list(a.shape), "sha256": sha(a[0:min(3, a.shape[0]), 0:256, 0:256].astype(np.uint16))}

    def labels_region():
        a = root["labels"]["cells_labels"]["s0"]
        return {"shape": list(a.shape), "sha256": sha(a[0:512, 0:512].astype(np.uint32))}

    def multiscales():
        attrs = dict(root["images"]["he_like"].attrs)
        ms = attrs.get("ome", attrs).get("multiscales") or attrs.get("multiscales")
        return [d["path"] for d in ms[0]["datasets"]]

    def shapes():
        tbl = pq.read_table(f"{path}/shapes/cells_circles/shapes.parquet")
        geom = tbl.column("geometry").to_pylist()
        return {"rows": tbl.num_rows, "firstGeometry": hashlib.sha256(geom[0]).hexdigest()}

    def points():
        import glob

        files = sorted(glob.glob(f"{path}/points/transcripts/points.parquet/*.parquet"))
        tbl = pq.read_table(files)
        return {"rows": tbl.num_rows, "x": sha(tbl.column("x").to_numpy().astype(np.float64))}

    for name, fn in (("table_obs", table_obs), ("table_obsm", table_obsm), ("table_x", table_x),
                     ("image_chunk", image_chunk), ("labels_region", labels_region),
                     ("multiscales", multiscales), ("shapes", shapes), ("points", points)):
        value, ms = timed(fn, repeat)
        out["ops"][name] = {"ms": ms, "value": value}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--h5ad", nargs="*", default=[])
    parser.add_argument("--sdata", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    report = {"h5ad": [h5ad_truth(p, args.repeat) for p in args.h5ad]}
    if args.sdata:
        report["sdata"] = sdata_truth(args.sdata, args.repeat)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(json.dumps({"h5ad": len(report["h5ad"]), "sdata": bool(args.sdata), "out": args.out}))


if __name__ == "__main__":
    main()
