from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import polars as pl


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
