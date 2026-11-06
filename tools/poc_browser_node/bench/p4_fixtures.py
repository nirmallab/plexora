"""P4 fixtures: AnnData files and a SpatialData store, as common writers make them.

    python bench/p4_fixtures.py OUT_DIR --table CSV --image FLAT.ome.tif --mask MASK.ome.tiff
           [--rows N] [--crop Y,X,H,W]

h5ad: the table's cells (1.26 M for Orion CRC02) x 90 float32 features -- its
markers and morphology, then synthetic log-normal ones -- written by anndata
(write_h5ad) as {dense, csr} x {none, gzip, lzf}; obs holds the centroids, the
area, a 12-category `phenotype` and a one-category `imageid`; obsm["spatial"]
the centroids; obs_names the cell ids as strings. CSR keeps values above each
feature's 70th percentile (~30 % dense).

SpatialData (spatialdata.write): an image (3 channels of a flat crop, three
scales), the matching label crop (three scales), circles at the cells'
centroids in the crop, 50,000 random points with a gene column, and a table
annotating the labels. Everything is under OUT_DIR; nothing else is written.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


def h5ad_fixtures(out: Path, table: str, rows: int | None) -> list[dict]:
    import anndata as ad
    import pandas as pd
    import polars as pl
    import scipy.sparse as sp

    df = pl.read_csv(table)
    if rows:
        df = df.head(rows)
    n = df.height
    rng = np.random.default_rng(20261105)
    morph = {"X_centroid", "Y_centroid", "Area", "MajorAxisLength", "MinorAxisLength", "Eccentricity",
             "Solidity", "Extent", "Orientation", "CellID"}
    markers = [c for c in df.columns if c not in morph]
    real = [c for c in markers + ["Area", "MajorAxisLength", "MinorAxisLength", "Eccentricity", "Solidity"]
            if c in df.columns]
    feats = np.empty((n, 90), dtype=np.float32)
    names = []
    for j, c in enumerate(real[:90]):
        feats[:, j] = df[c].cast(pl.Float32).to_numpy()
        names.append(c)
    for j in range(len(names), 90):
        feats[:, j] = rng.lognormal(mean=5, sigma=1.2, size=n).astype(np.float32)
        names.append(f"synthetic_{j:02d}")
    obs = pd.DataFrame({
        "X_centroid": df["X_centroid"].to_numpy(),
        "Y_centroid": df["Y_centroid"].to_numpy(),
        "Area": df["Area"].to_numpy().astype(np.float64),
        "phenotype": pd.Categorical(rng.choice([f"type_{k}" for k in range(12)], size=n)),
        "imageid": pd.Categorical(["CRC02"] * n),
    }, index=df["CellID"].cast(pl.Utf8).to_list())
    var = pd.DataFrame(index=names)
    spatial = np.stack([obs["X_centroid"].to_numpy(), obs["Y_centroid"].to_numpy()], axis=1)
    cut = np.percentile(feats, 70, axis=0)
    sparse = sp.csr_matrix(np.where(feats > cut, feats, 0).astype(np.float32))
    made = []
    for layout, X in (("dense", feats), ("csr", sparse)):
        for codec in ("none", "gzip", "lzf"):
            path = out / f"cells-{layout}-{codec}.h5ad"
            t0 = time.perf_counter()
            adata = ad.AnnData(X=X, obs=obs.copy(), var=var.copy())
            adata.obsm["spatial"] = spatial
            adata.write_h5ad(path, compression=None if codec == "none" else codec)
            made.append({"file": str(path), "layout": layout, "codec": codec, "bytes": path.stat().st_size,
                         "n_obs": n, "n_vars": 90, "seconds": round(time.perf_counter() - t0, 1)})
    return made


def _retrying_replace():
    """zarr writes a key to `<key>.partial` and renames it into place; on this
    Windows machine the rename is refused now and then while the target is
    still held (WinError 5) -- retried for a second before giving up."""
    import pathlib

    original = pathlib.Path.replace

    def replace(self, target):
        for attempt in range(40):
            try:
                return original(self, target)
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.025)

    pathlib.Path.replace = replace


def spatialdata_fixture(out: Path, image: str, mask: str, crop, image_offset=(0, 0)) -> dict:
    _retrying_replace()
    import dask.dataframe as dd
    import geopandas as gpd
    import pandas as pd
    import spatialdata as sd
    import tifffile
    import zarr
    from shapely.geometry import Point
    from spatialdata.models import Image2DModel, Labels2DModel, PointsModel, ShapesModel, TableModel

    y, x, h, w = crop
    iy, ix = image_offset
    with tifffile.TiffFile(image) as tf:
        zi = zarr.open(tf.series[0].aszarr(level=0), mode="r")
        if isinstance(zi, zarr.Group):
            zi = zi["0"]
        img = np.asarray(zi[:, iy:iy + h, ix:ix + w])
    with tifffile.TiffFile(mask) as tf:
        z = zarr.open(tf.series[0].aszarr(level=0), mode="r")
        lab = np.asarray(z[y:y + h, x:x + w]).astype(np.uint32)
    ids, inverse = np.unique(lab, return_inverse=True)
    lab = inverse.reshape(lab.shape).astype(np.uint32)   # 0..k, background stays 0
    k = int(lab.max())
    rng = np.random.default_rng(7)
    # Centroids of the relabelled cells.
    yy, xx = np.nonzero(lab)
    labels_flat = lab[yy, xx]
    counts = np.bincount(labels_flat, minlength=k + 1)
    cx = np.bincount(labels_flat, weights=xx, minlength=k + 1)[1:] / np.maximum(counts[1:], 1)
    cy = np.bincount(labels_flat, weights=yy, minlength=k + 1)[1:] / np.maximum(counts[1:], 1)
    circles = gpd.GeoDataFrame({"radius": np.sqrt(counts[1:] / np.pi)},
                               geometry=[Point(a, b) for a, b in zip(cx, cy)])
    points = pd.DataFrame({"x": rng.uniform(0, w, 50000), "y": rng.uniform(0, h, 50000),
                           "gene": pd.Categorical(rng.choice(["CD3E", "CD8A", "EPCAM", "PTPRC"], 50000))})
    import anndata as ad
    table = ad.AnnData(
        X=rng.lognormal(5, 1, size=(k, 20)).astype(np.float32),
        obs=pd.DataFrame({"region": pd.Categorical(["cells_labels"] * k), "instance_id": np.arange(1, k + 1),
                          "phenotype": pd.Categorical(rng.choice(["T", "B", "tumour", "stroma"], k))},
                         index=[str(i) for i in range(1, k + 1)]),
        var=pd.DataFrame(index=[f"marker_{j}" for j in range(20)]))
    table.obsm["spatial"] = np.stack([cx, cy], axis=1)
    sdata = sd.SpatialData(
        images={"he_like": Image2DModel.parse(img, dims=("c", "y", "x"), scale_factors=[2, 4])},
        labels={"cells_labels": Labels2DModel.parse(lab, dims=("y", "x"), scale_factors=[2, 4])},
        shapes={"cells_circles": ShapesModel.parse(circles)},
        points={"transcripts": PointsModel.parse(dd.from_pandas(points, npartitions=1), coordinates={"x": "x", "y": "y"})},
        tables={"table": TableModel.parse(table, region="cells_labels", region_key="region", instance_key="instance_id")},
    )
    path = out / "sdata.zarr"
    t0 = time.perf_counter()
    sdata.write(path, overwrite=True)
    files = [p for p in path.rglob("*") if p.is_file()]
    return {"path": str(path), "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "cells": k, "image": list(img.shape), "seconds": round(time.perf_counter() - t0, 1)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--table", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--mask", required=True)
    parser.add_argument("--rows", type=int, default=None)
    parser.add_argument("--crop", default="19900,10300,6001,7003")
    parser.add_argument("--image-offset", default="0,0",
                        help="Y,X of the crop in --image (0,0 when --image is already the crop)")
    parser.add_argument("--skip-h5ad", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {}
    if not args.skip_h5ad:
        report["h5ad"] = h5ad_fixtures(out, args.table, args.rows)
    report["spatialdata"] = spatialdata_fixture(out, args.image, args.mask, [int(v) for v in args.crop.split(",")],
                                                [int(v) for v in args.image_offset.split(",")])
    (out / "fixtures.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
