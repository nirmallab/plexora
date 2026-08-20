from __future__ import annotations

from pathlib import Path

import polars as pl

from .base import NormalizedDatasource


class CsvAdapter:
    """Adapter for the flat-CSV feature-table workflow.

    Takes the project's DataSpec (server/models/project.py): `src` says where
    the file is, `roles` say what its columns mean. A role the project never
    recorded is None here, which is why the coordinate columns are optional --
    a CSV imported but not yet fully described still loads, it just has no
    usable coordinates until something asks the user for them.
    """

    def __init__(self, spec):
        self.csv_path = Path(spec.src)
        self.x_column = spec.roles.x
        self.y_column = spec.roles.y
        self.id_field = spec.roles.cell_id
        self.celltype_column = spec.roles.celltype

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
