from __future__ import annotations

from pathlib import Path

from .anndata_adapter import AnnDataAdapter

# A SpatialData store keeps every annotation table as a plain AnnData group
# under <store>.zarr/tables/<name>, so once the user has picked *which* table
# to use it is an ordinary AnnData and every downstream config field,
# inspection result and adapter behavior is identical to the .h5ad path.
# That's why SpatialDataAdapter subclasses AnnDataAdapter and overrides only
# the read.
TABLES_GROUP = "tables"

# Zarr's own "this directory is a group" markers -- v3 writes zarr.json, v2
# writes .zgroup. Both appear in a single real store: spatialdata 0.7 wrote
# the exemplar LSP20209.zarr with a v3 root but v2 table groups, so anything
# sniffing the layout has to accept either.
_GROUP_MARKERS = ("zarr.json", ".zgroup")


def table_path(store, table) -> Path:
    """Resolve <store>/tables/<table>, rejecting anything that isn't a plain
    table name. Without this an empty name resolves to the tables/ group
    itself -- which *is* a valid zarr group, so the read gets that far and
    then fails deep inside anndata with a confusing
    "AnnData.__init__() got an unexpected keyword argument '<some table>'".
    Separators are refused for the same reason a table name arriving from a
    form post shouldn't be able to address anything outside tables/.
    """
    name = str(table or '').strip()
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError(f"Invalid SpatialData table name: {str(table)!r}")
    return Path(store) / TABLES_GROUP / name


def _is_group(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in _GROUP_MARKERS)


def read_spatialdata_table(store, table):
    """Read a single table out of a SpatialData store as an AnnData.

    Deliberately reads *only* the named table rather than going through
    spatialdata.read_zarr(store, selection=('tables',)), which eagerly
    materializes every table's X: on the exemplar LSP20209.zarr that is
    0.55s and +846MB (it loads two 1536-dim embedding tables nobody asked
    for) versus 0.04s and +13MB for the one table actually being imported.
    A future plugin that genuinely needs the other elements can open the
    full store itself -- the config records the store path alongside the
    table name for exactly that reason.

    spatialdata._io.io_table._read_table is the same per-table reader
    sd.read_zarr calls, so this keeps spatialdata's own TableModel version
    parsing rather than reimplementing it. It's a private symbol though, so
    a spatialdata upgrade that moves it falls back to anndata's reader
    (_read_table is a thin wrapper over exactly that, plus attrs
    normalization we don't consume) instead of breaking a packaged build.
    A test pins the import so the move is noticed rather than silently
    absorbed.
    """
    path = table_path(store, table)
    if not _is_group(path):
        raise ValueError(
            f"SpatialData store {str(store)!r} has no table named {str(table)!r} "
            f"(expected a zarr group at {path})"
        )

    try:
        from spatialdata._io.io_table import _read_table
    except ImportError:
        import anndata as ad

        return ad.read_zarr(path)
    return _read_table(path)


def _node_length(node) -> int | None:
    """Length of an on-disk AnnData node along its first axis, from zarr
    metadata only -- never reads values.

    A column is a plain array under zarr v2 but frequently a *group* under
    v3, where anndata encodes strings as 'nullable-string-array'
    (values/mask) and categoricals as 'categorical' (codes/categories);
    sparse X is a group too, carrying its shape in an attr. Both encodings
    occur in one real store -- spatialdata 0.7 wrote the exemplar
    LSP20209.zarr with a v3 root but v2 table groups -- so this handles all
    of them rather than assuming either.
    """
    shape = getattr(node, "shape", None)
    if shape is not None:
        return int(shape[0])
    attr_shape = node.attrs.get("shape")
    if attr_shape:
        return int(attr_shape[0])
    # A group standing in for one column: every encoding keeps a full-length
    # array under one of these names ('categories' is deliberately absent --
    # it holds the distinct levels, not one entry per observation).
    for child in ("codes", "values", "data"):
        try:
            child_shape = getattr(node[child], "shape", None)
        except KeyError:
            continue
        if child_shape is not None:
            return int(child_shape[0])
    return None


def _frame_length(group, key) -> int | None:
    """Row count of an on-disk AnnData dataframe group (obs/var). Prefers the
    index anndata records under the '_index' attr, falling back to any column
    -- they all share the axis length."""
    try:
        frame = group[key]
    except KeyError:
        return None
    index_name = frame.attrs.get("_index")
    candidates = ([index_name] if index_name else []) + [
        k for k in frame.keys() if k != index_name
    ]
    for candidate in candidates:
        try:
            length = _node_length(frame[candidate])
        except (KeyError, AttributeError, IndexError, TypeError):
            continue
        if length is not None:
            return length
    return None


def _table_shape(group) -> tuple[int | None, int | None]:
    """(n_obs, n_var) for a table group. X carries both in one place when it
    is readable (a dense array's shape, or a sparse group's 'shape' attr);
    obs/var are the fallback for a table whose X is absent."""
    try:
        x_shape = getattr(group["X"], "shape", None) or group["X"].attrs.get("shape")
        if x_shape is not None and len(x_shape) >= 2:
            return int(x_shape[0]), int(x_shape[1])
    except (KeyError, AttributeError, IndexError, TypeError):
        pass
    return _frame_length(group, "obs"), _frame_length(group, "var")


def list_spatialdata_tables(store) -> list[dict]:
    """Every table in a SpatialData store, with its shape, for the import
    form's table picker. Metadata-only: shapes come from zarr's array
    headers, so listing all three tables of the exemplar store (one of them
    243348x1536) costs ~5ms and ~1MB rather than loading any values.

    Raises ValueError if `store` isn't a readable SpatialData store, so the
    form can tell "not a store" apart from "a store with no tables" (a real
    state -- a store may hold only images/labels/shapes).
    """
    import zarr

    store_path = Path(store)
    if not _is_group(store_path):
        raise ValueError(f"{str(store)!r} is not a zarr store.")
    tables_dir = store_path / TABLES_GROUP
    if not tables_dir.is_dir():
        return []

    tables = []
    for entry in sorted(tables_dir.iterdir()):
        if not _is_group(entry):
            continue
        try:
            # Path (not str) matches spatialdata's own reader: zarr v3 parses
            # a string store as a URL, so table names containing characters
            # like '#' would be truncated.
            group = zarr.open_group(entry, mode="r")
        except Exception:
            continue
        n_obs, n_var = _table_shape(group)
        tables.append({"name": entry.name, "n_obs": n_obs, "n_var": n_var})
    return tables


def list_table_layers(store, table) -> list[str]:
    """The extra expression matrices one table carries alongside its X.

    Metadata-only, like list_spatialdata_tables above: zarr's group listing
    names the children without reading any of them, so this costs a directory
    walk rather than the hundreds of MB materializing a second matrix would.

    Best-effort by design -- an unreadable or layer-less table is an ordinary
    empty answer, not an error. The caller is asking "is there anything to
    choose between here?", and "no" is a fine reply.
    """
    import zarr

    path = table_path(store, table)
    if not _is_group(path):
        return []
    try:
        group = zarr.open_group(path, mode="r")
        return sorted(str(name) for name in group["layers"].keys() if name)
    except Exception:
        return []


class SpatialDataAdapter(AnnDataAdapter):
    """Adapter for a single table inside a SpatialData (.zarr) store.

    `spec.src` is the store root and `spec.table` names the table within it;
    every other field means exactly what it means for AnnData (see
    anndata_adapter.py), because the resolved table *is* an AnnData.

    Table coordinates are used as-is, in the pixel space of the registered
    image -- no SpatialData coordinate-system transform is applied. That
    holds for tables written alongside the image they annotate (the
    exemplar store's obsm['spatial'] is literally its X_centroid/Y_centroid
    columns); a store needing a transform would have to grow explicit
    support here.
    """

    def __init__(self, spec):
        super().__init__(spec)
        self.table = spec.table
        if not self.table:
            raise ValueError(
                "A SpatialData datasource needs a table -- it names which "
                "table inside the .zarr store to load."
            )

    def _read_adata(self):
        return read_spatialdata_table(self.path, self.table)

    def _read_obs(self):
        """The table's obs group, without the rest of the table.

        The same saving as the .h5ad path and for the same reason, only larger:
        `read_spatialdata_table` already avoids the store's *other* tables, but
        it still materializes this one's X -- and the exemplar store keeps a
        1536-dimensional embedding table whose X is most of its 846 MB. An
        annotation column is a directory read next to that.
        """
        import zarr

        try:
            from anndata.io import read_elem  # anndata >= 0.10, public API
        except ImportError:  # pragma: no cover - older anndata
            from anndata._io.specs import read_elem

        # Path, not str: zarr v3 parses a string store as a URL, so a table name
        # containing '#' would be truncated (same reason as list_spatialdata_tables).
        group = zarr.open_group(table_path(self.path, self.table), mode="r")
        return read_elem(group["obs"])
