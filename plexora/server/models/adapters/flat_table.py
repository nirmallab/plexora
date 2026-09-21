"""A table whose file IS the table.

CSV and Parquet are one format as far as everything above the read is
concerned: the columns on disk are the columns of the cell table, the
marker/metadata line is not drawn by the file (which is the whole reason the
classification screen exists), and there is nothing inside to choose between.
AnnData and SpatialData are the opposite -- containers holding tables, matrices
and annotations, where "which table?" and "which matrix?" are real questions.

That distinction is what the rest of the tree branches on, and it used to be
spelled `data_type == "csv"` in a dozen places. Spelled that way, adding a
second flat format means finding all dozen, and the one that gets missed is a
silent wrong answer rather than an error: `source_layers` would go looking for
`adata.layers` in a parquet, the edit page would offer the read-spec controls
instead of the column classifier. So the question has one name here,
`is_flat_table`, and the read and the write have one implementation each.

Parquet is read by polars directly -- no pyarrow. The `[spatial]` extra is for
the transcript-scale footer reads in `spatial_scene` (row counts and extents
out of a 6 GB file without touching a row); a 4 MB cell table is just read.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

#: The flat formats, as `DataSpec.type` records them. Both are read by the same
#: adapter -- the type names the encoding, not a different kind of table.
FLAT_TABLE_TYPES = ("csv", "parquet")

#: Suffix -> the type of flat table it holds. Folded into `_SUFFIX_TYPES` in
#: this package's `__init__`, so `detect_data_type` and this stay one list.
FLAT_TABLE_SUFFIXES = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
}


def is_flat_table(data_type) -> bool:
    """Whether this data type is a table read straight off its own file.

    The question every "is this a CSV?" branch was actually asking. False for
    None, which is what a project with no table has.
    """
    return data_type in FLAT_TABLE_TYPES


def read_flat_table(path, data_type="csv", *, n_rows=None) -> pl.DataFrame:
    """The file, as polars read it and before anything is done to it.

    `n_rows` reads a prefix -- enough to infer dtypes for the classification
    screen, or one row for a schema -- and None reads all of it.
    """
    path = Path(path)
    if data_type == "parquet":
        return pl.read_parquet(path, n_rows=n_rows)
    return pl.read_csv(path, n_rows=n_rows)


def write_flat_table(frame: pl.DataFrame, path, data_type="csv") -> None:
    """`frame` over the file at `path`, in the format `data_type` names.

    The counterpart of the read, and it exists for one caller: the ROI
    plugin writes its region columns back into the user's own table. Writing a
    parquet back as a CSV would be a silent format change to somebody's data.
    """
    path = Path(path)
    if data_type == "parquet":
        frame.write_parquet(path)
    else:
        frame.write_csv(path)
