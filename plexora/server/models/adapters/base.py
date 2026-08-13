from __future__ import annotations

from dataclasses import dataclass
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


class DatasourceAdapter(Protocol):
    """One instance per (config entry, load). Stateless with respect to
    data_model.py's module globals -- an adapter only knows how to turn a
    featureData config dict into a NormalizedDatasource. All I/O happens in
    load_table(), called once per load_datasource().
    """

    def load_table(self) -> NormalizedDatasource: ...
