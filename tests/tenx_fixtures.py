"""Small Space Ranger outputs, written the way Space Ranger writes them.

A Visium HD run's shape without its 40 GB: a `binned_outputs/square_XXXum/`
per bin size with a 10x feature-barcode `.h5` (CSC, barcodes as columns,
barcodes NOT in grid order), `spatial/tissue_positions.parquet` placed by a
known rotated-and-mirrored affine, `scalefactors_json.json`, a hires PNG, and
Space Ranger's `analysis/` CSVs; optionally a `segmented_outputs/` with cell
polygons and a cell matrix. Every value the tests assert against comes back
from the returned `truth` dict, never from re-reading the files.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

#: grid (col + 0.5, row + 0.5) -> full-res pixels: 7.3 px per 2 micron bin,
#: turned 180 degrees and mirrored, the way the pancreas run is.
SCALE = 7.3
GRID_TO_FULLRES = (-SCALE, 0.0, 0.0, SCALE, 64 * SCALE + 20.0, 15.0)
HIRES_SCALEF = 0.25
MICRONS_PER_PIXEL = 2.0 / SCALE


def _apply(t, x, y):
    a, b, c, d, e, f = t
    return a * x + c * y + e, b * x + d * y + f


def write_tenx_h5(path, counts, barcodes, genes, gene_ids=None):
    """`counts` (barcodes x genes) as a 10x `.h5`: CSC, barcodes as columns."""
    import h5py
    from scipy import sparse

    csr = sparse.csr_matrix(counts)            # rows = barcodes
    gene_ids = gene_ids or [f"ENSG{i:011d}" for i in range(len(genes))]
    with h5py.File(path, "w") as handle:
        handle.attrs["filetype"] = "matrix"
        handle.attrs["version"] = 2
        matrix = handle.create_group("matrix")
        matrix.create_dataset("barcodes", data=np.array(
            [b.encode() for b in barcodes], dtype=f"S{max(len(b) for b in barcodes)}"))
        matrix.create_dataset("data", data=csr.data.astype(np.int32))
        matrix.create_dataset("indices", data=csr.indices.astype(np.int64))
        matrix.create_dataset("indptr", data=csr.indptr.astype(np.int64))
        matrix.create_dataset("shape", data=np.array(
            [len(genes), len(barcodes)], dtype=np.int32))
        features = matrix.create_group("features")
        features.create_dataset("name", data=np.array([g.encode() for g in genes]))
        features.create_dataset("id", data=np.array([g.encode() for g in gene_ids]))
        features.create_dataset("feature_type", data=np.array(
            [b"Gene Expression"] * len(genes)))
        features.create_dataset("genome", data=np.array([b"GRCh38"] * len(genes)))


def _write_png(path, width, height):
    from PIL import Image

    rng = np.random.default_rng(3)
    Image.fromarray(rng.integers(150, 255, size=(height, width, 3),
                                 dtype=np.uint8)).save(path)


def write_visium_hd_run(root, *, grid=64, levels=(2, 8), genes=None,
                        density=0.08, segmented=False, seed=5):
    """A Visium HD `outs/` under `root`. Returns `(outs, truth)`."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(seed)
    genes = list(genes or ["INS", "GCG", "PRSS1", "KRT19", "SST", "INS"])
    outs = Path(root) / "outs"
    tissue = np.zeros((grid, grid), dtype=bool)
    tissue[4:grid - 6, 6:grid - 2] = True
    fine = rng.poisson(density, size=(len(genes), grid, grid)).astype(np.int64)
    fine[0, 20:30, 30:40] += 3                      # an islet
    fine *= tissue
    truth = {"genes": genes, "fine": fine, "tissue": tissue, "grid": grid,
             "levels": {}, "outs": outs}

    full_w = int(grid * SCALE + 40)
    for size in levels:
        factor = size // 2
        n = -(-grid // factor)
        level_dir = outs / "binned_outputs" / f"square_{size:03d}um"
        spatial = level_dir / "spatial"
        spatial.mkdir(parents=True, exist_ok=True)
        padded = np.zeros((len(genes), n * factor, n * factor), dtype=np.int64)
        padded[:, :grid, :grid] = fine
        pooled = padded.reshape(len(genes), n, factor, n, factor).sum((2, 4))
        pooled_tissue = np.zeros((n * factor, n * factor), bool)
        pooled_tissue[:grid, :grid] = tissue
        pooled_tissue = pooled_tissue.reshape(n, factor, n, factor).any((1, 3))
        rr, cc = np.nonzero(pooled_tissue)
        order = rng.permutation(len(rr))            # h5 order is not grid order
        rr, cc = rr[order], cc[order]
        barcodes = [f"s_{size:03d}um_{r:05d}_{c:05d}-1" for r, c in zip(rr, cc)]
        write_tenx_h5(level_dir / "filtered_feature_bc_matrix.h5",
                      pooled[:, rr, cc].T, barcodes, genes)
        # Positions for EVERY square of the grid, in or out of tissue.
        all_r, all_c = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        all_r, all_c = all_r.ravel(), all_c.ravel()
        t = GRID_TO_FULLRES
        level_t = (t[0] * factor, t[1] * factor, t[2] * factor, t[3] * factor,
                   t[4], t[5])
        px, py = _apply(level_t, all_c + 0.5, all_r + 0.5)
        pq.write_table(pa.table({
            "barcode": [f"s_{size:03d}um_{r:05d}_{c:05d}-1"
                        for r, c in zip(all_r, all_c)],
            "in_tissue": pooled_tissue[all_r, all_c].astype(np.uint8),
            "array_row": all_r.astype(np.uint32),
            "array_col": all_c.astype(np.uint32),
            "pxl_row_in_fullres": py.astype(np.float64),
            "pxl_col_in_fullres": px.astype(np.float64),
        }), spatial / "tissue_positions.parquet")
        (spatial / "scalefactors_json.json").write_text(json.dumps({
            "spot_diameter_fullres": SCALE * factor,
            "bin_size_um": float(size),
            "microns_per_pixel": MICRONS_PER_PIXEL,
            "regist_target_img_scalef": HIRES_SCALEF,
            "tissue_lowres_scalef": HIRES_SCALEF / 10,
            "fiducial_diameter_fullres": 100.0,
            "tissue_hires_scalef": HIRES_SCALEF,
        }), encoding="utf-8")
        _write_png(spatial / "tissue_hires_image.png",
                   int(full_w * HIRES_SCALEF), int(full_w * HIRES_SCALEF))
        _write_png(spatial / "tissue_lowres_image.png", 12, 12)
        if size != 2:
            clusters = level_dir / "analysis" / "clustering" / \
                "gene_expression_graphclust"
            clusters.mkdir(parents=True, exist_ok=True)
            labels = (rr * 3 // n) + 1
            (clusters / "clusters.csv").write_text(
                "Barcode,Cluster\n" + "".join(
                    f"{b},{l}\n" for b, l in zip(barcodes, labels)),
                encoding="utf-8")
            umap = level_dir / "analysis" / "umap" / "gene_expression_2_components"
            umap.mkdir(parents=True, exist_ok=True)
            (umap / "projection.csv").write_text(
                "Barcode,UMAP-1,UMAP-2\n" + "".join(
                    f"{b},{r * 0.1:.3f},{c * 0.1:.3f}\n"
                    for b, r, c in zip(barcodes, rr, cc)), encoding="utf-8")
        truth["levels"][size] = {
            "dir": level_dir, "pooled": pooled, "tissue": pooled_tissue,
            "barcodes": barcodes, "rows": rr, "cols": cc, "n": n,
            "transform": level_t}

    if segmented:
        truth["cells"] = _write_segmentation(outs, truth, rng)
    return outs, truth


def _write_segmentation(outs, truth, rng):
    """Square cells of 4x4 bins over the tissue, as Space Ranger 4 writes them."""
    seg = outs / "segmented_outputs"
    (seg / "spatial").mkdir(parents=True, exist_ok=True)
    genes = truth["genes"]
    features, ids, centres, counts = [], [], [], []
    cell = 0
    t = GRID_TO_FULLRES
    for r0 in range(8, truth["grid"] - 12, 6):
        for c0 in range(10, truth["grid"] - 8, 6):
            cell += 1
            corners = [_apply(t, c, r) for c, r in (
                (c0, r0), (c0 + 4, r0), (c0 + 4, r0 + 4), (c0, r0 + 4), (c0, r0))]
            cx, cy = _apply(t, c0 + 2, r0 + 2)
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon",
                             "coordinates": [[list(p) for p in corners]]},
                "properties": {"cell_id": cell, "cell_centroid": [cx, cy],
                               "nucleus_centroid": [cx, cy]}})
            ids.append(cell)
            centres.append((cx, cy))
            counts.append(truth["fine"][:, r0:r0 + 4, c0:c0 + 4].sum((1, 2)))
    (seg / "cell_segmentations.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": features}), encoding="utf-8")
    (seg / "nucleus_segmentations.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": features[:3]}),
        encoding="utf-8")
    barcodes = [f"cellid_{i:09d}-1" for i in ids]
    write_tenx_h5(seg / "filtered_feature_cell_matrix.h5",
                  np.array(counts), barcodes, genes)
    (seg / "spatial" / "scalefactors_json.json").write_text(json.dumps({
        "microns_per_pixel": MICRONS_PER_PIXEL,
        "tissue_hires_scalef": HIRES_SCALEF}), encoding="utf-8")
    return {"ids": np.array(ids), "centres": np.array(centres),
            "counts": np.array(counts), "barcodes": barcodes}
