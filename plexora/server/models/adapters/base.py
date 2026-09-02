from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import polars as pl


@dataclass(frozen=True)
class MetadataColumn:
    """One annotation column, aligned row-for-row with the loaded table.

    Exists because `NormalizedDatasource.table` is deliberately narrow for the
    structural formats: AnnDataAdapter materializes id/X/Y/the id field/the
    markers/the celltype column and nothing else, so an arbitrary `.obs` column
    is named by `obs_columns` but is not IN the table. A tool that colours cells
    by an annotation needs the values, and re-reading the whole file to get one
    column would cost the same as the import did.

    `categories` is the source's own category order when it declares one -- a
    pandas Categorical in obs. It is carried rather than re-derived because it
    is the one ordering that cannot be recovered from the values: a legend
    sorted alphabetically puts "Stage 10" before "Stage 2", and a file that
    already says what order its levels go in should be believed. None means the
    source said nothing, and the caller is free to sort.
    """

    name: str
    values: np.ndarray
    categories: tuple[str, ...] | None = None


@dataclass(frozen=True)
class NormalizedDatasource:
    """Source-agnostic view of an observation/cell table.

    `table` always carries a positional 'id' column plus the resolved
    x/y/feature/celltype columns under the exact names data_model.py's
    downstream gating/query code already expects (the same names stored in
    config[...]['featureData'][0]['xCoordinate']/['yCoordinate']/etc.) --
    adapters differ only in how they produce this table, never in its shape.
    """

    table: pl.DataFrame
    id_column: str
    x_column: str
    y_column: str
    feature_columns: list[str]
    celltype_column: str | None
    # The two below describe what else the SOURCE offers -- not what this table
    # ended up holding. They exist for adapters that build `table` from a read
    # spec rather than reading a file verbatim, and they are what a user later
    # picks from when changing that spec. Both are empty for a CSV, where the
    # table's columns already are the file's columns and there is one matrix.

    #: The source file's own annotation column names, in its own order. What a
    #: user chooses between when saying which column holds the cell id or the
    #: coordinates -- see Project.role_columns.
    obs_columns: list[str] = field(default_factory=list)
    #: The names of the extra expression matrices the file carries alongside
    #: `X`. What a user chooses between when saying which one holds the marker
    #: intensities -- see Project.feature_source.
    layers: list[str] = field(default_factory=list)
    #: The source file's obsm arrays as {"name", "shape"}. What a user chooses
    #: between when saying where the cell coordinates are -- see
    #: Project.coordinate_options. The shape is carried because it is the only
    #: other thing on offer, and even it cannot separate a position from an
    #: embedding: `spatial` and `X_umap` are routinely both (n, 2) float32.
    obsm: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class TablePlan:
    """Everything about a table that can be known without reading its matrix.

    The split this represents is the whole memory story. `load_table()` used to
    open with `adata = self._read_adata()` -- the entire file, unbacked --
    *before* it looked at the subset, so picking one image out of sixty cost
    more than loading all sixty rather than less. And `datasource.py` calls the
    adapter at REGISTRATION purely to learn the marker/metadata split and the
    obs/layer/obsm vocabularies, every one of which is metadata. That is why a
    large multi-image file could not even be imported.

    So the adapter answers in two steps. `plan()` returns this -- obs and var
    only, no matrix, bounded by the annotation columns -- and `stream()` reads
    the matrix in row blocks afterwards, if anybody actually needs the values.

    **Every user-facing `ValueError` an adapter can raise about a read spec is
    raised by `plan()`**: an unknown subset column, a subset matching no
    observations, a table spanning several images with no image chosen, a
    missing obsm key, non-finite coordinates, an unknown layer, a feature name
    colliding with a reserved column, a missing celltype or obs_id column. That
    is what lets the routes keep validating a user's answer synchronously (and
    restoring the previous project when it is wrong) while the expensive half
    moves to a background job.
    """

    #: Rows the loaded table will have, after any subset.
    rows: int
    #: Positions of those rows in the SOURCE, ascending, or None for "all of
    #: them". Ascending because h5py fancy selection requires it and because a
    #: contiguous run lets the reader use a slab read instead.
    row_indices: "np.ndarray | None"
    #: Columns already materialized out of obs: the positional id, X, Y, the
    #: resolved identifier column and the celltype column. Bounded by
    #: construction -- a handful of columns, never the matrix.
    columns: dict
    #: Marker names, in matrix column order, already deduplicated and checked
    #: for collisions with the reserved names.
    feature_columns: list
    #: Which matrix `stream()` should read: ("X", None), ("layer", name), or
    #: ("obs", None) when the features came out of obs and are already in
    #: `columns`.
    feature_source: tuple
    id_column: str
    x_column: str
    y_column: str
    celltype_column: "str | None"
    #: The column holding the source's own observation identifier -- either the
    #: obs column the user named, or DEFAULT_ID_COLUMN carrying obs_names. Not
    #: the same thing as `id_column`, which is always the positional row index.
    obs_id_column: str = ""
    obs_columns: list = field(default_factory=list)
    layers: list = field(default_factory=list)
    obsm: list = field(default_factory=list)

    @property
    def table_columns(self) -> list:
        """The loaded table's column names, in order, without loading it.

        Registration needs exactly this and nothing else -- the marker/metadata
        split is `feature_columns` against the rest -- which is why importing a
        file no longer has to read one.
        """
        names = ["id", "X", "Y", self.obs_id_column]
        names += [name for name in self.feature_columns if name not in names]
        if self.celltype_column and self.celltype_column not in names:
            names.append(self.celltype_column)
        return names


class DatasourceAdapter(Protocol):
    """One instance per (config entry, load). Stateless with respect to
    data_model.py's module globals -- an adapter only knows how to turn a
    featureData config dict into a NormalizedDatasource. All I/O happens in
    load_table(), called once per load_datasource().
    """

    def load_table(self) -> NormalizedDatasource: ...

    def plan(self) -> TablePlan:
        """Everything about the table that needs no matrix read. See TablePlan."""
        ...

    def stream(self, plan: TablePlan, sink, progress=None) -> None:
        """Write the marker values into `sink`, one row block at a time.

        `sink(name, start, values)` takes a column name, the row offset within
        the planned table, and a float32 array. Peak memory is one block, not
        one dataset, which is the entire point -- so an implementation that
        materializes the matrix to satisfy this has missed it.
        """
        ...

    def read_obs_column(self, name: str) -> MetadataColumn | None:
        """One annotation column the loaded table does not carry.

        None means "there is no such thing here" rather than "not found": a CSV
        adapter's table IS the file, so every column is already in `frame()` and
        there is nothing this could add. Adapters that build a table from a read
        spec return the values, subset exactly as `load_table()` subset them, so
        the result lines up row-for-row with the loaded frame.
        """
        ...
