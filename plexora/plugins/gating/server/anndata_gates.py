from __future__ import annotations

import contextlib
import json
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import polars as pl

from plexora import api

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


def _resolve_path(source) -> str:
    """Location of the AnnData *group* holding this datasource's data: the
    .h5ad file itself, or the selected table inside a SpatialData .zarr
    store. Everything below reads and writes through this one location, so
    gates land in the table the user actually imported -- never anywhere
    else in the store.

    `source` is the api.TableSource core hands the plugin. It is deliberately
    not the raw config entry: where a table lives on disk is core's business,
    and this plugin only needs to be told, not to know the file format.
    """
    if source is None or not source.path:
        raise ValueError("No AnnData file path configured for this datasource")
    if source.kind == 'spatialdata':
        from plexora.server.models.adapters.spatialdata_adapter import table_path

        return str(table_path(source.path, source.table))
    return source.path


def _consolidated_format(path: Path) -> int | None:
    """Which zarr format's consolidated index this group carries, if any.

    A consolidated index is a cached copy of every child's metadata, kept
    beside the group: `.zmetadata` in zarr v2, a `consolidated_metadata` key
    inside `zarr.json` in v3. Readers that find one trust it completely and
    never list the directory -- which is what makes it both the reason a
    write is refused and the reason a write must be followed by a refresh.

    Read off disk rather than from an opened group so the answer is known
    before choosing how to open it.
    """
    if (path / '.zmetadata').is_file():
        return 2
    metadata = path / 'zarr.json'
    if metadata.is_file():
        try:
            document = json.loads(metadata.read_text(encoding='utf-8'))
        except (OSError, ValueError):  # pragma: no cover - unreadable metadata
            return None
        if document.get('consolidated_metadata') is not None:
            return 3
    return None


@contextlib.contextmanager
def _open_group(path: str, writable: bool = False):
    """Yield the root group of an on-disk AnnData, for either backend.

    anndata's element codec (read_elem/write_elem) is storage-agnostic, so
    every caller below works unchanged against an h5py group from an .h5ad
    or a zarr group from a SpatialData table. A .zarr store is a directory;
    that's what distinguishes the two here.

    Writing to a *consolidated* zarr group needs two extra steps, both
    required and neither optional:

    1. Open with `use_consolidated=False`. anndata refuses to write to a
       group whose metadata is consolidated (`is_group_consolidated()` in
       anndata/_io/specs/registry.py) and raises "Cannot overwrite/edit a
       store with consolidated metadata" -- a real store written by
       spatialdata hits this, because it consolidates each table. Opening
       without the index sidesteps a guard that exists to stop exactly the
       staleness step 2 repairs.
    2. Rebuild the index afterwards. Skipping it loses the write silently:
       the new group is on disk, but every reader consults the stale index
       instead of listing the directory, so `anndata.read_zarr` and
       `spatialdata.read_zarr` both report no gates and the save appears to
       have done nothing. Verified against a copy of a real store.

    The refresh is confined to this group -- the table, never the store
    root. That is not just conservatism: re-consolidating a SpatialData
    root, which is zarr v3, silently DROPS every v2 table from the index
    (real stores mix the two), leaving a store whose tables have vanished.
    Nothing needs it anyway; a root index enumerates which elements exist,
    and adding a key inside a table does not change that.
    """
    if Path(path).is_dir():
        import zarr

        # Path (not str) deliberately: zarr v3 parses a string store as a
        # URL, mangling table names containing characters like '#'.
        location = Path(path)
        if not writable:
            yield zarr.open_group(location, mode='r')
            return

        consolidated = _consolidated_format(location)
        yield zarr.open_group(location, mode='a', use_consolidated=False)
        # Only on success, and only if there was an index to begin with:
        # a store that never had one needs no refresh, and writing one
        # would change how every other tool reads it.
        if consolidated is not None:
            zarr.consolidate_metadata(
                zarr.storage.LocalStore(location), zarr_format=consolidated
            )
        return
    with h5py.File(path, 'r+' if writable else 'r') as handle:
        yield handle


def _read_frame(group, key) -> pd.DataFrame:
    """Read just obs or var off an already-open group. This is the same
    codec anndata uses internally, so it costs one dataframe read and never
    touches X -- the property the old backed-mode .h5ad read had, now true
    for the zarr backend too (zarr-backed AnnData has no backed mode)."""
    return read_elem(group[key])


def _read_obs_column(path: str, subset: dict, column: str) -> pl.Series | None:
    """Lightweight read of a single obs column, optionally filtered by the
    same subset.column/subset.value rule AnnDataAdapter.load_table()
    applies. Returns None if the column doesn't exist. Never loads X --
    safe/cheap to call repeatedly."""
    with _open_group(path) as group:
        obs = _read_frame(group, 'obs')
    if column not in obs.columns:
        return None
    subset_column = (subset or {}).get('column')
    if subset_column:
        if subset_column not in obs.columns:
            raise ValueError(f"Subset column {subset_column!r} not found in adata.obs")
        mask = obs[subset_column].astype(str).to_numpy() == str(subset.get('value'))
        obs = obs.loc[mask]
        if obs.empty:
            raise ValueError("Subset filter matched zero observations")
    return pl.Series(column, obs[column].astype(str).to_numpy())


def resolve_current_image_id(
    path: str, source, datasource_name: str, imageid_column: str
) -> str:
    """Which column of the gates table this Plexora datasource's gates
    should be written to. Re-applies this datasource's own registration
    subset (if any) and requires the configured imageid column to resolve
    to exactly one value within it; falls back to the datasource's own
    registered name if the column doesn't exist at all."""
    subset = dict(getattr(source, 'subset', None) or {})
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
    with _open_group(path) as group:
        return [str(v) for v in _read_frame(group, 'var').index]


def save_gates_to_anndata(
    source,
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
    path = _resolve_path(source)
    var_names = _read_var_names(path)
    current_image_id = resolve_current_image_id(
        path, source, datasource_name, imageid_column
    )
    known_image_ids = all_image_ids(path, imageid_column) or [current_image_id]
    if current_image_id not in known_image_ids:
        known_image_ids = sorted(set(known_image_ids) | {current_image_id})

    with _open_group(path, writable=True) as f:
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
        # actually shows/sends (AnnDataAdapter.api.deduplicate_names(): first
        # occurrence keeps the plain name, e.g. "PTPRC"; the next becomes
        # "PTPRC_1", etc.) -- not the raw, still-duplicated var_names list.
        # Matching against raw var_names here used to silently drop every
        # gate on a "_N"-suffixed channel (no raw var_name is ever literally
        # "PTPRC_1"), undercounting how many gated markers actually got
        # written with zero error -- confirmed against real exemplar data
        # (orion.h5ad has a duplicated "PTPRC").
        var_index = {name: i for i, name in enumerate(api.deduplicate_names(var_names))}
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


def load_gates_from_anndata(
    source,
    datasource_name: str,
    table_name: str = "gates",
    imageid_column: str = "imageid",
) -> dict:
    """Reverse of save_gates_to_anndata: reads adata.uns[table_name]'s column
    for this datasource's current image back out as {channel: lower_bound},
    keyed by the same deduplicated display names save_gates_to_anndata
    writes against -- so it plugs directly into gatingList.gating_channels
    client-side without a name-mapping step. Read-only, no h5py write mode.
    """
    path = _resolve_path(source)
    current_image_id = resolve_current_image_id(
        path, source, datasource_name, imageid_column
    )

    with _open_group(path) as f:
        uns = f.get('uns')
        if uns is None or table_name not in uns:
            return {"image_id": current_image_id, "gates": {}}
        existing = read_elem(uns[table_name])
        if not isinstance(existing, pd.DataFrame) or current_image_id not in existing.columns:
            return {"image_id": current_image_id, "gates": {}}
        column = existing[current_image_id]

    # Table rows are ordered like var_names at the time of the last save
    # (see save_gates_to_anndata's realignment note) -- matched back to
    # today's deduplicated display names by position, the same "common,
    # overwhelmingly likely" fast-path assumption that function documents.
    var_names = _read_var_names(path)
    display_names = api.deduplicate_names(var_names)

    gates = {}
    for position, name in enumerate(display_names):
        if position >= len(column):
            break
        value = column.iloc[position]
        if value is None or (isinstance(value, float) and np.isnan(value)):
            continue
        gates[name] = float(value)

    return {"image_id": current_image_id, "gates": gates}
