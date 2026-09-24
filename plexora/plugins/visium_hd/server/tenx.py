"""Reading a Space Ranger bin matrix into the blocks core's bin store takes.

The vendor half of a Visium HD layer, and the only place that knows how Space
Ranger names a square. Core's `bin_tiles` takes a CSR block of barcodes with a
grid row and column each; this turns `filtered_feature_bc_matrix.h5` into
those blocks, and knows nothing about tiles.

**The matrix is stored barcode-major.** 10x writes CSC with barcodes as the
columns, which is byte for byte a CSR matrix of barcodes x genes -- so a block
of barcodes is one contiguous slice of `indices` and `data`, and the whole
2 micron matrix (870 million entries) streams through in blocks of a few
hundred megabytes without ever being held.

**The grid position is in the barcode.** `s_002um_00852_00467-1` is row 852,
column 467 of the 2 micron grid. Parsed from the fixed-width bytes in one
vectorised pass, which for 20 million barcodes is the difference between a
second and a minute; the positions parquet agrees with it by construction and
is not read here.

h5py is imported inside the functions: the boundary tests pin that building
the app imports neither h5py nor anndata.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

#: Barcodes per block. ~11 million matrix entries at 2 microns, about 130 MB
#: of indices and data in flight.
BLOCK_BARCODES = 250_000

_BIN_BARCODE = re.compile(rb"^s_(\d{3})um_(\d+)_(\d+)-\d+$")


def read_scalefactors(level_dir):
    path = Path(level_dir) / "spatial" / "scalefactors_json.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def matrix_path(level_dir):
    """The filtered matrix of one `square_XXXum` folder."""
    return Path(level_dir) / "filtered_feature_bc_matrix.h5"


def parse_barcodes(barcodes):
    """`(rows, cols)` int64 from `s_002um_RRRRR_CCCCC-1` barcodes.

    Core's parser (`tenx_matrix.parse_bin_barcodes`) -- the conversion of the
    table reads the same barcodes, and two parsers of one format is one too
    many.
    """
    from plexora.server.utils import tenx_matrix

    return tenx_matrix.parse_bin_barcodes(barcodes)


def bin_size_of(barcode):
    """`2` for `s_002um_...`, or None."""
    match = _BIN_BARCODE.match(bytes(barcode).rstrip(b"\0"))
    return int(match.group(1)) if match else None


def read_features(h5_path):
    """`(names, ids)` in matrix order, as str lists."""
    import h5py

    with h5py.File(h5_path, "r") as handle:
        features = handle["matrix"]["features"]
        names = [n.decode() for n in features["name"][:]]
        ids = [n.decode() for n in features["id"][:]]
    return names, ids


def matrix_shape(h5_path):
    """`(genes, barcodes)` as 10x states it."""
    import h5py

    with h5py.File(h5_path, "r") as handle:
        genes, barcodes = (int(v) for v in handle["matrix"]["shape"][:])
    return genes, barcodes


def block_count(h5_path, block=BLOCK_BARCODES):
    _, barcodes = matrix_shape(h5_path)
    return max(1, -(-barcodes // block))


def iter_blocks(h5_path, block=BLOCK_BARCODES):
    """Yield `(rows, cols, indptr, indices, data)` per block of barcodes."""
    import h5py

    with h5py.File(h5_path, "r") as handle:
        matrix = handle["matrix"]
        indptr = matrix["indptr"][:]
        barcodes = matrix["barcodes"]
        indices = matrix["indices"]
        data = matrix["data"]
        total = len(indptr) - 1
        for start in range(0, total, block):
            stop = min(start + block, total)
            lo, hi = int(indptr[start]), int(indptr[stop])
            rows, cols = parse_barcodes(barcodes[start:stop])
            yield (rows, cols, indptr[start:stop + 1] - lo,
                   indices[lo:hi], data[lo:hi])


def refit_transform(level_dir):
    """The grid -> full-res similarity over EVERY square, and its residual.

    Detection fits one record batch, which is enough to register the layer;
    the build has the time to fit all of them and to say how well they agree,
    which is what the manifest's `fit_residual_px` reports.
    """
    import pyarrow.parquet as pq

    from plexora.server.utils import spatial_scene

    path = Path(level_dir) / "spatial" / "tissue_positions.parquet"
    table = pq.read_table(path, columns=["array_row", "array_col",
                                         "pxl_row_in_fullres",
                                         "pxl_col_in_fullres"])
    row = table.column("array_row").to_numpy().astype(np.float64)
    col = table.column("array_col").to_numpy().astype(np.float64)
    src = np.column_stack((col + 0.5, row + 0.5))
    dst = np.column_stack((table.column("pxl_col_in_fullres").to_numpy(),
                           table.column("pxl_row_in_fullres").to_numpy()))
    return spatial_scene.fit_similarity(src, dst)
