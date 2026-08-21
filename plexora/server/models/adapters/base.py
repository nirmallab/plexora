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
    source_obs_ids: list[str]
    x_column: str
    y_column: str
    feature_columns: list[str]
    celltype_column: str | None
    obs_metadata: pl.DataFrame | None = None
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


class DatasourceAdapter(Protocol):
    """One instance per (config entry, load). Stateless with respect to
    data_model.py's module globals -- an adapter only knows how to turn a
    featureData config dict into a NormalizedDatasource. All I/O happens in
    load_table(), called once per load_datasource().
    """

    def load_table(self) -> NormalizedDatasource: ...

    def read_obs_column(self, name: str) -> MetadataColumn | None:
        """One annotation column the loaded table does not carry.

        None means "there is no such thing here" rather than "not found": a CSV
        adapter's table IS the file, so every column is already in `frame()` and
        there is nothing this could add. Adapters that build a table from a read
        spec return the values, subset exactly as `load_table()` subset them, so
        the result lines up row-for-row with the loaded frame.
        """
        ...
