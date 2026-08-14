from __future__ import annotations

import h5py
import numpy as np
import pandas as pd
import polars as pl

from plexora.server.models.adapters.anndata_adapter import _deduplicate_names

try:
    from anndata.io import read_elem, write_elem  # anndata >= 0.10, public API
except ImportError:  # pragma: no cover - older anndata fallback
    from anndata._io.specs import read_elem, write_elem

# Column name used for the var_names index when the gates table is
# represented in-memory as a polars DataFrame (AnnData's own on-disk
# dataframe encoding stores the index separately, under this same name, so
# a real ad.read_h5ad() sees adata.uns[table_name].index.name == this value).
INDEX_COLUMN = "var_names"


def _pandas_to_polars(df: pd.DataFrame, index_name: str) -> pl.DataFrame:
    """Manual conversion (no pl.from_pandas()/pyarrow -- not an installed
    dependency here, see anndata_gates design notes in the project plan)."""
    data = {index_name: df.index.astype(str).tolist()}
    for col in df.columns:
        data[str(col)] = df[col].to_numpy(dtype="float64")
    return pl.DataFrame(data)


def _polars_to_pandas(df: pl.DataFrame, index_name: str) -> pd.DataFrame:
    """Manual conversion (no .to_pandas()/pyarrow) -- the write_elem/read_elem
    calls into AnnData's own on-disk dataframe codec are the only reason
    pandas appears in this module at all."""
    image_columns = [c for c in df.columns if c != index_name]
    return pd.DataFrame(
        df.select(image_columns).to_numpy(),
        index=pd.Index(df[index_name].to_list(), name=index_name),
        columns=image_columns,
    )


def _resolve_path(feature_config: dict) -> str:
    data_source = feature_config.get('dataSource') or {}
    path = data_source.get('path') or feature_config.get('src')
    if not path:
        raise ValueError("No AnnData file path configured for this datasource")
    return path


def _read_obs_column(path: str, subset: dict, column: str) -> pl.Series | None:
    """Lightweight backed-mode read of a single obs column, optionally
    filtered by the same subset.column/subset.value rule
    AnnDataAdapter.load_table() applies. Returns None if the column doesn't
    exist. Never loads X -- safe/cheap to call repeatedly."""
    import anndata as ad

    adata = ad.read_h5ad(path, backed='r')
    try:
        if column not in adata.obs.columns:
            return None
        obs = adata.obs
        subset_column = (subset or {}).get('column')
        if subset_column:
            if subset_column not in obs.columns:
                raise ValueError(f"Subset column {subset_column!r} not found in adata.obs")
            mask = obs[subset_column].astype(str).to_numpy() == str(subset.get('value'))
            obs = obs.loc[mask]
            if obs.empty:
                raise ValueError("Subset filter matched zero observations")
        return pl.Series(column, obs[column].astype(str).to_numpy())
    finally:
        if adata.isbacked:
            adata.file.close()


def resolve_current_image_id(
    path: str, feature_config: dict, datasource_name: str, imageid_column: str
) -> str:
    """Which column of the gates table this Plexora datasource's gates
    should be written to. Re-applies this datasource's own registration
    subset (if any) and requires the configured imageid column to resolve
    to exactly one value within it; falls back to the datasource's own
    registered name if the column doesn't exist at all."""
    data_source = feature_config.get('dataSource') or {}
    subset = data_source.get('subset') or {}
    values = _read_obs_column(path, subset, imageid_column)
    if values is None:
        return datasource_name

    unique_values = values.unique().to_list()
    if len(unique_values) != 1:
        raise ValueError(
            f"Column {imageid_column!r} does not resolve to a single image "
            f"for datasource {datasource_name!r} (found {len(unique_values)} "
            "distinct values within this datasource's own data) -- refusing "
            "to guess which gates-table column to update."
        )
    return unique_values[0]


def all_image_ids(path: str, imageid_column: str) -> list[str] | None:
    """Every distinct value of imageid_column across the *whole* source
    file (no subset applied) -- used to eagerly create one gates-table
    column per known image. None if the column doesn't exist anywhere."""
    values = _read_obs_column(path, {}, imageid_column)
    if values is None:
        return None
    return sorted(values.unique().to_list())


def _read_var_names(path: str) -> list[str]:
    import anndata as ad

    adata = ad.read_h5ad(path, backed='r')
    try:
        return [str(v) for v in adata.var_names]
    finally:
        if adata.isbacked:
            adata.file.close()


def save_gates_to_anndata(
    feature_config: dict,
    datasource_name: str,
    active_gates: dict,
    table_name: str = "gates",
    imageid_column: str = "imageid",
) -> dict:
    """Writes only adata.uns[table_name] (rows=var_names, one column per
    known image) via direct h5py + anndata's element codec -- never a full
    ad.read_h5ad()/write_h5ad() round trip. Overwrites only the column for
    the image this datasource currently represents; every other image's
    column (freshly added blank, or previously saved) is left untouched.

    active_gates: {channel_name: lower_bound}, already filtered to
    currently-active gates by the caller.
    """
    path = _resolve_path(feature_config)
    var_names = _read_var_names(path)
    current_image_id = resolve_current_image_id(
        path, feature_config, datasource_name, imageid_column
    )
    known_image_ids = all_image_ids(path, imageid_column) or [current_image_id]
    if current_image_id not in known_image_ids:
        known_image_ids = sorted(set(known_image_ids) | {current_image_id})

    with h5py.File(path, 'r+') as f:
        uns = f.require_group('uns')

        if table_name in uns:
            existing = read_elem(uns[table_name])
            if not isinstance(existing, pd.DataFrame):
                raise ValueError(
                    f"adata.uns[{table_name!r}] already exists and is not a table"
                )
            existing_table = _pandas_to_polars(existing, INDEX_COLUMN)
            existing_var_names = existing_table[INDEX_COLUMN].to_list()
            if existing_var_names == var_names:
                # Fast, exact path -- the overwhelmingly common case (panel
                # hasn't changed between saves). No realignment needed.
                table = existing_table
            else:
                # Realign rows to the current var_names -- stale vars are
                # dropped, new vars appear as all-NaN rows. Expected if the
                # source panel changed since the last save, not a bug.
                # Done via an explicit first-occurrence-name lookup rather
                # than a polars join: real panels can have duplicate
                # var_names (e.g. a marker re-stained across cycles -- see
                # the var_index note below), and joining on a non-unique key
                # produces a row explosion (each duplicate on one side
                # matches every duplicate on the other) instead of a clean
                # 1:1 realignment.
                old_index = {}
                for i, name in enumerate(existing_var_names):
                    if name not in old_index:
                        old_index[name] = i
                table = pl.DataFrame({INDEX_COLUMN: var_names})
                for col in existing_table.columns:
                    if col == INDEX_COLUMN:
                        continue
                    old_values = existing_table[col].to_list()
                    new_values = [
                        old_values[old_index[name]] if name in old_index else None
                        for name in var_names
                    ]
                    table = table.with_columns(pl.Series(col, new_values, dtype=pl.Float64))
        else:
            table = pl.DataFrame({INDEX_COLUMN: var_names})

        for image_id in known_image_ids:
            if image_id not in table.columns:
                table = table.with_columns(
                    pl.lit(None, dtype=pl.Float64).alias(image_id)
                )

        values = np.full(len(var_names), np.nan, dtype="float64")
        # active_gates is keyed by the *gating channel* name, which for a
        # duplicate var_name is the deduplicated display name the frontend
        # actually shows/sends (AnnDataAdapter._deduplicate_names(): first
        # occurrence keeps the plain name, e.g. "PTPRC"; the next becomes
        # "PTPRC_1", etc.) -- not the raw, still-duplicated var_names list.
        # Matching against raw var_names here used to silently drop every
        # gate on a "_N"-suffixed channel (no raw var_name is ever literally
        # "PTPRC_1"), undercounting how many gated markers actually got
        # written with zero error -- confirmed against real exemplar data
        # (orion.h5ad has a duplicated "PTPRC").
        var_index = {name: i for i, name in enumerate(_deduplicate_names(var_names))}
        n_written = 0
        for channel, lower_bound in active_gates.items():
            idx = var_index.get(channel)
            if idx is None or lower_bound is None:
                continue
            values[idx] = float(lower_bound)
            n_written += 1

        table = table.with_columns(pl.Series(current_image_id, values, dtype=pl.Float64))

        if table_name in uns:
            del uns[table_name]
        write_elem(uns, table_name, _polars_to_pandas(table, INDEX_COLUMN))

    return {
        "table_name": table_name,
        "imageid_column": imageid_column,
        "image_id": current_image_id,
        "path": path,
        "n_active_gates": n_written,
        "n_image_columns": len(table.columns) - 1,
    }
