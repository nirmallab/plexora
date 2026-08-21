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
        # The marker/metadata split the user confirmed at import. A CSV header
        # does not draw that line itself, which is the whole reason the
        # classification screen exists -- so this is the only thing that knows
        # Area and Eccentricity are measurements rather than stains.
        self.marker_columns = list(spec.columns.markers)
        # Explicit opt-in only -- no heuristic guessing at whether the values
        # "look" already transformed. Same contract as AnnDataAdapter, which is
        # where this used to be honoured and only there: a CSV kept the flag
        # and read straight past it, so a user who asked for the transform got
        # a project that said it was transformed and was not.
        self.apply_log_transform = bool(spec.is_transformed)

    def read_obs_column(self, name: str):
        """Always None: a CSV's table IS the file.

        There is no second place to look -- `load_table()` reads every column,
        so anything a caller could ask for is already in `frame()`. Returning
        None says "nothing to add here", which is what lets the caller treat a
        column that is missing from both as genuinely unknown rather than as a
        format it forgot to handle.
        """
        return None

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

        feature_columns = self._feature_columns(df)
        if self.apply_log_transform:
            # After the -inf guard above and over the markers only, matching
            # AnnDataAdapter: transforming a coordinate or a cell id would move
            # every cell on the image. Numeric ones at that -- the split is the
            # user's to correct, so a text column can end up in the marker box,
            # and log1p on it is an error rather than a bad number.
            transform = [c for c in feature_columns if df.schema[c].is_numeric()]
            df = df.with_columns([pl.col(c).log1p().alias(c) for c in transform])

        return NormalizedDatasource(
            table=df,
            id_column="id",
            source_obs_ids=source_obs_ids,
            x_column=self.x_column,
            y_column=self.y_column,
            feature_columns=feature_columns,
            celltype_column=self.celltype_column,
        )

    def _feature_columns(self, df) -> list[str]:
        """Which columns hold marker intensities.

        The recorded split when there is one, narrowed to columns this file
        actually has: the answer can outlive the file it was given for, and a
        marker naming a column that is gone would be log-transformed into a
        polars error and offered as a gate with no data behind it.

        Everything-but-the-roles is the fallback, for a project registered
        before the classification screen ran. It cannot tell a stain from a
        measurement -- that is what the screen is for -- so it is a last resort
        and not the answer.
        """
        excluded = {"id", self.x_column, self.y_column, self.id_field, self.celltype_column}
        recorded = [c for c in self.marker_columns
                    if c in df.columns and c not in excluded]
        if recorded:
            return recorded
        return [c for c in df.columns if c not in excluded]
