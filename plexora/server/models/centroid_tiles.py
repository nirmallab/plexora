import json
import os
import shutil
import threading
from pathlib import Path

import numpy as np
import polars as pl

from plexora import paths
from plexora.server.models.project import Project


CACHE_VERSION = 1
DEFAULT_TILE_SIZE = 512
DEFAULT_MAX_POINTS_PER_TILE = 5000
INTERNAL_DTYPE = np.dtype([
    ("row", "<u4"),
    ("id", "<u4"),
    ("x", "<f4"),
    ("y", "<f4"),
])
RESPONSE_DTYPE = np.dtype([
    ("id", "<u4"),
    ("x", "<f4"),
    ("y", "<f4"),
])

_cache_locks = {}
_cache_locks_guard = threading.Lock()
_filter_tables = {}


def _lock_for(datasource_name):
    with _cache_locks_guard:
        if datasource_name not in _cache_locks:
            _cache_locks[datasource_name] = threading.RLock()
        return _cache_locks[datasource_name]


def _cache_dir(datasource_name):
    """This datasource's centroid tile cache.

    A derived artifact, so it goes beside the project when that root can be
    written to and into the user's own root when it cannot -- see
    paths.derived_root. Reads go through the same function rather than
    find_derived because the manifest is checked for existence anyway, and a
    half-built cache in the other root is not something to adopt.
    """
    return paths.derived_root(datasource_name) / "centroids_v1"


def _manifest_path(datasource_name):
    return _cache_dir(datasource_name) / "manifest.json"


def _project(config, datasource_name):
    return Project.from_entry(datasource_name, config[datasource_name])


def _load_table(config, datasource_name):
    """Load the full normalized datasource table through the same adapter
    data_model.py dispatches on, rather than assuming the source file is
    always a CSV -- that assumption broke entirely for non-CSV datasources
    (e.g. AnnData, where the columns named by the id/X/Y roles only exist
    after adapter normalization, not in the raw source file at all). Local
    import avoids a circular dependency with data_model.py, which itself
    imports this module.
    """
    from plexora.server.models import data_model
    from plexora.server.models.adapters import get_adapter

    project = _project(config, datasource_name)
    if project.resource("table") is not None:
        # The file is on another machine and the ids and coordinates -- which
        # is all this builds from -- are already here, pulled once when the
        # project loaded. Asking the node for them again would be a second copy
        # of the one payload the compact cache exists to avoid repeating.
        data_model._ensure_loaded(datasource_name)
        return data_model.get_datasource_df()
    spec = project.dataset
    return get_adapter(spec.type)(spec).load_table().table


def _source_signature(csv_path):
    stat = csv_path.stat()
    return {
        "csv_path": str(csv_path.resolve()),
        "csv_size": stat.st_size,
        "csv_mtime_ns": stat.st_mtime_ns,
    }


def _node_signature(binding):
    """What a node-backed table's cache was built from.

    The node's own fingerprint and the generation it was at, in the same three
    keys `_is_manifest_current` already compares -- so the staleness check is
    one rule rather than two that can disagree. Better information than a stat,
    too: a node bumps its generation on every reload, including reloads caused
    by somebody editing the file on that machine, which a stat here could never
    see.
    """
    fingerprint = dict(binding.fingerprint or {})
    return {
        "csv_path": f"node://{binding.node}/{binding.resource_id}",
        "csv_size": fingerprint.get("size"),
        "csv_mtime_ns": fingerprint.get("mtime_ns"),
    }


def _expected_manifest(config, datasource_name):
    project = _project(config, datasource_name)
    binding = project.resource("table")
    signature = (_node_signature(binding) if binding is not None
                 else _source_signature(Path(project.dataset.src).expanduser().resolve()))
    tile_size = int(config[datasource_name].get("tileWidth") or DEFAULT_TILE_SIZE)
    tile_size = max(1, tile_size)
    width = int(config[datasource_name]["width"])
    height = int(config[datasource_name]["height"])
    max_level = max(1, int(config[datasource_name].get("maxLevel", 1)))
    return {
        "version": CACHE_VERSION,
        **signature,
        "id_column": project.roles.cell_id or "id",
        "x_column": project.roles.x,
        "y_column": project.roles.y,
        "width": width,
        "height": height,
        "tile_size": tile_size,
        "level_count": max_level,
        "max_points_per_tile": DEFAULT_MAX_POINTS_PER_TILE,
        "record_dtype": "row:uint32,id:uint32,x:float32,y:float32",
        "response_dtype": "id:uint32,x:float32,y:float32",
    }


def _is_manifest_current(manifest, expected):
    keys = [
        "version",
        "csv_path",
        "csv_size",
        "csv_mtime_ns",
        "id_column",
        "x_column",
        "y_column",
        "width",
        "height",
        "tile_size",
        "level_count",
    ]
    return all(manifest.get(key) == expected.get(key) for key in keys)


def get_manifest(config, datasource_name, build=True):
    project = _project(config, datasource_name)
    if not project.has_table or not (project.roles.x and project.roles.y):
        # Nothing to place: either no feature table at all (image-only), or
        # one whose coordinate columns nobody has identified yet. Centroids
        # are positions, so an unresolved x/y role is as good as no table.
        return {"status": "missing"}
    lock = _lock_for(datasource_name)
    with lock:
        expected = _expected_manifest(config, datasource_name)
        path = _manifest_path(datasource_name)
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            if _is_manifest_current(manifest, expected):
                return {**manifest, "status": "ready"}

        if not build:
            return {**expected, "status": "missing"}

        manifest = build_cache(config, datasource_name, expected)
        return {**manifest, "status": "ready"}


def build_cache(config, datasource_name, expected=None):
    expected = expected or _expected_manifest(config, datasource_name)
    root = _cache_dir(datasource_name)
    tmp_root = root.with_name(f"{root.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    usecols = [expected["id_column"], expected["x_column"], expected["y_column"]]
    table = _load_table(config, datasource_name).select(usecols)

    # Explicit null->NaN fill (rather than relying on Polars' default float
    # export behavior) so invalid/unparseable values behave like pandas'
    # pd.to_numeric(errors="coerce") did: NaN, not null, so the np.isfinite
    # checks below keep working unchanged.
    ids = table[expected["id_column"]].cast(pl.Float64, strict=False).fill_null(float("nan")).to_numpy()
    xs = table[expected["x_column"]].cast(pl.Float64, strict=False).fill_null(float("nan")).to_numpy()
    ys = table[expected["y_column"]].cast(pl.Float64, strict=False).fill_null(float("nan")).to_numpy()
    valid = np.isfinite(ids) & np.isfinite(xs) & np.isfinite(ys)
    rows = np.nonzero(valid)[0].astype(np.uint32, copy=False)
    ids = ids[valid].astype(np.uint32, copy=False)
    xs = xs[valid].astype(np.float32, copy=False)
    ys = ys[valid].astype(np.float32, copy=False)

    for level in range(expected["level_count"]):
        _write_level(tmp_root, level, rows, ids, xs, ys, expected)

    manifest = {
        **expected,
        "point_count": int(len(ids)),
    }
    manifest_path = tmp_root / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    if root.exists():
        shutil.rmtree(root)
    try:
        shutil.move(str(tmp_root), str(root))
    except PermissionError:
        target_manifest = root / "manifest.json"
        if target_manifest.exists():
            with target_manifest.open("r", encoding="utf-8") as handle:
                promoted = json.load(handle)
            if _is_manifest_current(promoted, expected):
                _filter_tables.pop(datasource_name, None)
                return promoted
        raise
    _filter_tables.pop(datasource_name, None)
    return manifest


def _write_level(root, level, rows, ids, xs, ys, manifest):
    level_dir = root / f"level_{level}"
    level_dir.mkdir(parents=True, exist_ok=True)
    tile_span = manifest["tile_size"] * (2 ** level)
    tx = np.floor(xs / tile_span).astype(np.int64, copy=False)
    ty = np.floor(ys / tile_span).astype(np.int64, copy=False)
    tx = np.clip(tx, 0, max(0, int(np.ceil(manifest["width"] / tile_span)) - 1))
    ty = np.clip(ty, 0, max(0, int(np.ceil(manifest["height"] / tile_span)) - 1))

    order = np.lexsort((rows, tx, ty))
    tx_sorted = tx[order]
    ty_sorted = ty[order]
    starts = np.r_[0, np.flatnonzero((tx_sorted[1:] != tx_sorted[:-1]) | (ty_sorted[1:] != ty_sorted[:-1])) + 1]
    stops = np.r_[starts[1:], len(order)]

    max_points = int(manifest["max_points_per_tile"])
    for start, stop in zip(starts, stops):
        tile_order = order[start:stop]
        if level > 0 and len(tile_order) > max_points:
            step = int(np.ceil(len(tile_order) / max_points))
            tile_order = tile_order[::step][:max_points]
        records = np.empty(len(tile_order), dtype=INTERNAL_DTYPE)
        records["row"] = rows[tile_order]
        records["id"] = ids[tile_order]
        records["x"] = xs[tile_order]
        records["y"] = ys[tile_order]
        tile_x = int(tx[tile_order[0]])
        tile_y = int(ty[tile_order[0]])
        records.tofile(level_dir / f"tile_{tile_x}_{tile_y}.bin")


def _read_tile(datasource_name, level, tile):
    path = _cache_dir(datasource_name) / f"level_{int(level)}" / f"tile_{int(tile['x'])}_{int(tile['y'])}.bin"
    if not path.exists():
        return np.empty(0, dtype=INTERNAL_DTYPE)
    return np.fromfile(path, dtype=INTERNAL_DTYPE)


def _load_filter_table(config, datasource_name, gates):
    if not gates:
        return None
    expected = _expected_manifest(config, datasource_name)
    gate_columns = tuple(sorted(gates.keys()))
    key = (
        datasource_name,
        expected["csv_path"],
        expected["csv_size"],
        expected["csv_mtime_ns"],
        gate_columns,
    )
    cached = _filter_tables.get(key)
    if cached is not None:
        return cached

    table = _load_table(config, datasource_name).select(list(gate_columns))
    numeric = {}
    for column in gate_columns:
        numeric[column] = (
            table[column].cast(pl.Float32, strict=False).fill_null(float("nan")).to_numpy()
        )
    _filter_tables.clear()
    _filter_tables[key] = numeric
    return numeric


def _apply_gates(records, filter_table, gates):
    if records.size == 0 or not gates:
        return records
    rows = records["row"].astype(np.int64, copy=False)
    keep = np.ones(records.shape[0], dtype=bool)
    for column, values in gates.items():
        if column not in filter_table:
            continue
        low = float(values[0])
        high = float(values[1])
        column_values = filter_table[column][rows]
        keep &= np.isfinite(column_values) & (column_values > low) & (column_values < high)
    return records[keep]


def get_tiles(config, datasource_name, level, tiles, gates=None, max_points=None):
    project = _project(config, datasource_name)
    if not project.has_table or not (project.roles.x and project.roles.y):
        return []
    get_manifest(config, datasource_name, build=True)
    gates = gates or {}
    arrays = [_read_tile(datasource_name, level, tile) for tile in tiles]
    if not arrays:
        records = np.empty(0, dtype=INTERNAL_DTYPE)
    else:
        records = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]

    filter_table = _load_filter_table(config, datasource_name, gates)
    records = _apply_gates(records, filter_table, gates)

    if max_points and records.shape[0] > int(max_points):
        step = int(np.ceil(records.shape[0] / int(max_points)))
        records = records[::step][:int(max_points)]

    response = np.empty(records.shape[0], dtype=RESPONSE_DTYPE)
    response["id"] = records["id"]
    response["x"] = records["x"]
    response["y"] = records["y"]
    return response
