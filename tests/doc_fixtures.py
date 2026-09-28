"""Tiny files the documentation examples refer to by name.

A runnable example in a docstring or guide says `plexora.create_project(
"slide.ome.tif", data="cells.csv")`; tests/test_docs_examples.py builds every
name from `FIXTURE_FILES` that appears in the example as a string literal,
in the example's working directory, before running it. Keep the files small:
the point is that the call works, not what it shows.

The builders copy the conventions of tests/test_datasets_api.py (256 px,
because load_datasource walks down to the last pyramid level with every
dimension >= 200) and tests/test_import_entry_points.py (a uint32 label mask).
"""

from pathlib import Path

import numpy as np


def image(path: Path) -> Path:
    import tifffile

    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def mask(path: Path) -> Path:
    import tifffile

    tifffile.imwrite(path, (np.arange(256 * 256).reshape(256, 256) % 90).astype(np.uint32))
    return path


def cells_csv(path: Path) -> Path:
    import polars as pl

    pl.DataFrame({
        "CellID": np.arange(1, 5, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4),
        "Y_centroid": np.linspace(10, 200, 4),
        "CD3": np.linspace(0, 3, 4),
        "CD8": np.linspace(1, 4, 4),
        "area": np.linspace(10, 40, 4),
        "phenotype": ["T cell", "B cell", "T cell", "Tumor"],
    }).write_csv(path)
    return path


def cells_h5ad(path: Path) -> Path:
    import anndata as ad
    import pandas as pd

    obs = pd.DataFrame({"phenotype": ["T cell", "B cell", "T cell", "Tumor"]},
                       index=[str(i) for i in range(1, 5)])
    adata = ad.AnnData(X=np.linspace(0, 1, 8, dtype=np.float32).reshape(4, 2), obs=obs)
    adata.var_names = ["CD3", "CD8"]
    adata.obsm["spatial"] = np.column_stack([np.linspace(10, 200, 4), np.linspace(10, 200, 4)])
    adata.write_h5ad(path)
    return path


#: File name -> builder. A docs example may use any of these names.
FIXTURE_FILES = {
    "slide.ome.tif": image,
    "slide2.ome.tif": image,
    "slide_mask.tif": mask,
    "cells.csv": cells_csv,
    "cells.h5ad": cells_h5ad,
}


def build_referenced(source: str, directory: Path) -> list[str]:
    """Build every fixture whose name appears as a string literal in `source`."""
    built = []
    for name, builder in FIXTURE_FILES.items():
        if f'"{name}"' in source or f"'{name}'" in source:
            builder(directory / name)
            built.append(name)
    return built
