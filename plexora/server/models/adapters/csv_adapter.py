from __future__ import annotations

from pathlib import Path

import polars as pl

from .base import NormalizedDatasource


class CsvAdapter:
    """Adapter for the original flat-CSV feature-table workflow.

    load_table() is a verbatim lift of data_model.py's pre-refactor
    load_datasource() body (same read, same positional 'id' column, same
    -inf fix) -- this is a pure interface change, not a behavior change.
    """

    def __init__(self, feature_config: dict):
        self.csv_path = Path(feature_config['src'])
        self.x_column = feature_config['xCoordinate']
        self.y_column = feature_config['yCoordinate']
        self.id_field = feature_config.get('idField')
        self.celltype_column = feature_config.get('celltype')

    def load_table(self) -> NormalizedDatasource:
        df = pl.read_csv(self.csv_path)
        # Manufacture a stable positional 'id' column, mirroring pandas'
        # implicit RangeIndex usage in the code this replaced -- must happen
        # immediately after read_csv, before any other transform, since
        # downstream code treats 'id' as a stable per-row identity.
        df = df.with_row_index("id").with_columns(pl.col("id").cast(pl.Int64))
        numeric_cols = [c for c, dt in df.schema.items() if dt in (pl.Float32, pl.Float64)]
        df = df.with_columns([
            pl.when(pl.col(c) == float("-inf")).then(0).otherwise(pl.col(c)).alias(c)
            for c in numeric_cols
        ])

        if self.id_field and self.id_field in df.columns:
            source_obs_ids = df[self.id_field].cast(pl.Utf8).to_list()
        else:
            source_obs_ids = df["id"].cast(pl.Utf8).to_list()

        excluded = {"id", self.x_column, self.y_column, self.id_field, self.celltype_column}
        feature_columns = [c for c in df.columns if c not in excluded]

        return NormalizedDatasource(
            table=df,
            id_column="id",
            source_obs_ids=source_obs_ids,
            x_column=self.x_column,
            y_column=self.y_column,
            feature_columns=feature_columns,
            celltype_column=self.celltype_column,
        )
