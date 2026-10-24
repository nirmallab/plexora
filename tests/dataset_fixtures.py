"""Folder layouts a dataset is built from, written small.

The layouts the dataset builder has to group correctly, each as the folder a
lab actually has on disk: role folders (A), a folder per sample with generic
file names (B), the two mixed (C), one table for the whole cohort (D), and an
incomplete, uneven cohort (E), plus an mcmicro run and a tree that is mostly
not data at all. Images are 256 px because registration walks a pyramid down
to its last level of at least 200 px; the pixels are noise, which no detector
mistakes for a mask, and the masks are a few labelled squares.
"""

from __future__ import annotations

import numpy as np
import tifffile

MARKERS = "channel_number,cycle_number,marker_name\n1,1,DNA\n2,1,CD3\n3,1,CD8\n"


def image(path, size=256, channels=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.random.default_rng(0).integers(
        0, 3000, (channels, size, size)).astype(np.uint16), photometric="minisblack")
    return path


def mask(path, size=256):
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = np.zeros((size, size), dtype=np.uint32)
    for index in range(1, 6):
        top = index * (size // 9)
        labels[top:top + 12, top:top + 12] = index
    tifffile.imwrite(path, labels)
    return path


def csv(path, rows=4, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["CellID", "X_centroid", "Y_centroid", "CD3", "Area"] + list(extra or ())
    lines = [",".join(columns)]
    for index in range(rows):
        values = [str(index + 1), str(10 + index), str(20 + index), "5", "30"]
        values += ["A" for _ in extra or ()]
        lines.append(",".join(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cohort_h5ad(path, images, per=20):
    """One AnnData holding every image's cells, keyed by `imageid`."""
    import anndata as ad
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    count = per * len(images)
    rng = np.random.default_rng(1)
    obs = pd.DataFrame({
        "imageid": pd.Categorical(np.repeat(list(images), per)),
        "X_centroid": rng.uniform(0, 200, count),
        "Y_centroid": rng.uniform(0, 200, count),
    }, index=[f"c{i}" for i in range(count)])
    adata = ad.AnnData(X=rng.gamma(2, 100, (count, 3)).astype(np.float32), obs=obs,
                       var=pd.DataFrame(index=["DNA", "CD3", "CD8"]))
    adata.obsm["spatial"] = obs[["X_centroid", "Y_centroid"]].to_numpy()
    adata.write_h5ad(path)
    return path


def layout_a(root):
    """Role folders; every file names its sample. C has no mask."""
    for name in ("A", "B", "C"):
        image(root / "images" / f"{name}.ome.tiff")
        csv(root / "tables" / f"{name}.csv")
    for name in ("A", "B"):
        mask(root / "masks" / f"{name}_mask.ome.tiff")
    return root


def layout_b(root):
    """A folder per sample; every file has the same generic name."""
    for index in (1, 2, 3):
        folder = root / f"Sample0{index}"
        image(folder / "image.ome.tiff")
        csv(folder / "cells.csv")
        if index != 3:
            mask(folder / "mask.ome.tiff")
    return root


def layout_c(root):
    """Per-sample folders with role folders inside, generic and specific names
    mixed, and the tables in a role folder at the top."""
    image(root / "Sample01" / "registered" / "image.ome.tiff")
    mask(root / "Sample01" / "segmentation" / "nuclei_mask.ome.tiff")
    image(root / "Sample02" / "Sample02_registered.ome.tiff")
    mask(root / "Sample02" / "Sample02_cell_mask.ome.tiff")
    csv(root / "quantification" / "Sample01_cells.csv")
    csv(root / "quantification" / "Sample02_cells.csv")
    return root


def layout_d(root):
    """Role folders, and one AnnData for the whole cohort."""
    for name in ("A", "B"):
        image(root / "images" / f"{name}.ome.tiff")
        mask(root / "masks" / f"{name}_mask.ome.tiff")
    cohort_h5ad(root / "data" / "combined.h5ad", ["A", "B"])
    return root


def layout_e(root):
    """Uneven: two masks for one image, an image with nothing, and a generic
    mask at the top that belongs to no one in particular."""
    image(root / "S1.ome.tiff")
    mask(root / "S1_cell.tif")
    mask(root / "S1_nuclei.tif")
    image(root / "S2.ome.tiff")
    mask(root / "other" / "image_mask.ome.tiff")
    return root


def mcmicro(root, samples=("LSP1", "LSP2")):
    """What mcmicro writes, per sample, under one project folder."""
    (root / "markers.csv").parent.mkdir(parents=True, exist_ok=True)
    (root / "markers.csv").write_text(MARKERS, encoding="utf-8")
    for name in samples:
        image(root / "registration" / f"{name}.ome.tif")
        mask(root / "segmentation" / f"unmicst-{name}" / "cell.ome.tif")
        mask(root / "segmentation" / f"unmicst-{name}" / "nuclei.ome.tif")
        csv(root / "quantification" / f"{name}--unmicst_cell.csv")
        image(root / "probability-maps" / "unmicst" / f"{name}_Probabilities_0.tif",
              channels=1)
        (root / "qc").mkdir(exist_ok=True)
        (root / "qc" / f"{name}_params.yml").write_text("a: 1\n", encoding="utf-8")
    return root


def big_tree(root, files=5000, rasters=150):
    """Mostly not data: `files` small text files, `rasters` images among them."""
    root.mkdir(parents=True, exist_ok=True)
    per_folder = 100
    for index in range(files):
        folder = root / f"batch{index // per_folder:03d}"
        folder.mkdir(exist_ok=True)
        (folder / f"log_{index:05d}.log").write_text("x", encoding="utf-8")
    for index in range(rasters):
        image(root / f"batch{index % 50:03d}" / f"S{index:03d}.ome.tiff", size=64,
              channels=1)
    return root
