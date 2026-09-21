"""A Xenium `cells.parquet`, made readable by the rest of Plexora.

The table a Xenium run ships is a correct description of the run and the wrong
shape for every consumer downstream of it, in two ways that are invisible
until nothing draws:

**Its cell ids are strings.** `aaaacidg-1`, a base-26 code the instrument
generates. `centroid_tiles.build_cache` casts the id column to float to pack
it into a fixed-width record -- which is right for every other table Plexora
reads, where the id is a label value -- so every row came back NaN, every row
was dropped as invalid, and the Cells layer built a cache of nothing and
reported success. The run states a numeric id of its own in
`cell_boundaries.parquet`'s `label_id`, which is the value a rasterised
segmentation mask would carry, so that is the one used when it is there.

**Its coordinates are microns.** Everything in a Xenium run is, and the
registration that puts a run's layers into the reference image's pixel grid is
the transform `layers_for` composes from the pixel size. A TABLE gets no
transform: `CsvAdapter` reads x and y and they are reference pixels by
definition of being in the table. So the centroids landed at a twentieth of
their true distance from the origin, bunched into the top-left corner of the
slide -- which looks like a plausible tissue section, only small.

Both are fixed once, at import, on the COPY the project owns
(`import_routes._copy_into_project`) -- never on the run directory, which is
the vendor's output and is not ours to write to. Fixing them here rather than
teaching each consumer means `CsvAdapter`, the centroid cache, the ROI
plugin's write-back, gating, the cell explorer and the ball tree all get the
correction without knowing it happened, and the provenance record this returns
is what tells the user it did.
"""

from __future__ import annotations

from pathlib import Path

#: The column added to carry a numeric cell identifier. Added rather than
#: replacing `cell_id`: the string code is what the user sees in Xenium
#: Explorer and what a collaborator will quote in an email, and dropping it to
#: make an index fit would make the two products impossible to talk about
#: together.
INDEX_COLUMN = "cell_index"

#: The Xenium cell table's own column names. Specific rather than a heuristic,
#: because this is only ever called for a table an importer already identified
#: as a Xenium run's -- and a heuristic that fired on somebody else's CSV
#: would rescale coordinates that were already right.
ID_COLUMN = "cell_id"
X_COLUMN = "x_centroid"
Y_COLUMN = "y_centroid"

#: Where the run states a numeric id per cell, and under what name.
BOUNDARY_FILES = ("cell_boundaries.parquet", "nucleus_boundaries.parquet")
LABEL_COLUMN = "label_id"


def _label_ids(root):
    """`(cell_id -> label_id)` as a polars frame, or None.

    Read with a lazy scan projected to two columns, so the 6-million-vertex
    boundary table costs about a quarter of a second and a few tens of
    megabytes rather than being materialized. `unique` because the table is
    one row per polygon VERTEX.
    """
    if not root:
        return None
    import polars as pl

    for name in BOUNDARY_FILES:
        candidate = Path(root) / name
        if not candidate.is_file():
            continue
        try:
            frame = (pl.scan_parquet(str(candidate))
                     .select([ID_COLUMN, LABEL_COLUMN])
                     .unique()
                     .collect())
        except Exception:
            continue
        if frame.height:
            return frame
    return None


def normalise_cells_table(path, *, pixel_size=None, root=None):
    """Rewrite a Xenium cell table in place so Plexora can read it.

    @param path - the project's own COPY of `cells.parquet`.
    @param pixel_size - microns per reference pixel, from the run's manifest.
        Without it the coordinates are left alone: a guessed scale puts every
        cell at the wrong distance from the origin while looking entirely
        plausible, which is worse than the honest failure of leaving them in
        microns and saying so.
    @param root - the run directory, for `label_id`.
    @returns a provenance record, or None when there was nothing to do (which
        is what a re-import of an already-normalised copy hits).
    """
    import polars as pl

    path = Path(path)
    if path.suffix.lower() != ".parquet" or not path.is_file():
        return None
    try:
        frame = pl.read_parquet(str(path))
    except Exception:
        return None

    names = set(frame.columns)
    if ID_COLUMN not in names or not {X_COLUMN, Y_COLUMN} <= names:
        return None
    if INDEX_COLUMN in names:
        # Already done. This is the project's own copy being re-attached
        # through the edit page rather than a fresh copy of the run's file,
        # and scaling it a second time would put every cell at a twentieth of
        # a twentieth of where it belongs.
        return None

    record = {}

    if not frame[ID_COLUMN].dtype.is_numeric():
        labels = _label_ids(root)
        joined = None
        if labels is not None:
            candidate = frame.select(ID_COLUMN).join(
                labels, on=ID_COLUMN, how="left")
            if candidate.height == frame.height \
                    and candidate[LABEL_COLUMN].null_count() == 0:
                joined = candidate[LABEL_COLUMN].cast(pl.Int64)
        if joined is not None:
            frame = frame.with_columns(joined.alias(INDEX_COLUMN))
            record["cell_index_from"] = LABEL_COLUMN
        else:
            # 1-based, matching how every segmentation mask numbers its
            # labels and how `label_id` itself is numbered -- 0 is background
            # everywhere in this codebase.
            frame = frame.with_columns(
                pl.int_range(1, frame.height + 1, dtype=pl.Int64)
                  .alias(INDEX_COLUMN))
            record["cell_index_from"] = "row_number"
        # Next to the id it stands in for, not at the end: the cell explorer
        # shows columns in table order, and an identifier buried behind eleven
        # count columns reads as a measurement.
        order = list(frame.columns)
        order.remove(INDEX_COLUMN)
        order.insert(order.index(ID_COLUMN) + 1, INDEX_COLUMN)
        frame = frame.select(order)

    if pixel_size:
        step = 1.0 / float(pixel_size)
        frame = frame.with_columns([
            (pl.col(X_COLUMN).cast(pl.Float64) * step).alias(X_COLUMN),
            (pl.col(Y_COLUMN).cast(pl.Float64) * step).alias(Y_COLUMN),
        ])
        record["units"] = "reference_pixels"
        record["pixel_size"] = float(pixel_size)
    else:
        record["units"] = "microns"

    frame.write_parquet(str(path))
    return record
