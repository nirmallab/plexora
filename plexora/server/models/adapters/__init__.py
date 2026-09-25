from pathlib import Path

from .base import DatasourceAdapter, MetadataColumn, NormalizedDatasource
from .classify import classify_columns, classify_from_inspection
from .flat_table import (FLAT_TABLE_SUFFIXES, FLAT_TABLE_TYPES, is_flat_table,
                         read_flat_table, write_flat_table)
from .csv_adapter import CsvAdapter
from .anndata_adapter import AnnDataAdapter
from .spatialdata_adapter import SpatialDataAdapter

#: Data type -> the class that reads it. `csv` and `parquet` share one, because
#: they are one table in two encodings: `CsvAdapter` dispatches its single
#: format-specific step on `DataSpec.type` and everything after the read --
#: the positional id, the -inf guard, the marker split, the log transform --
#: is the same code. A second class would be a second place for those to drift.
_ADAPTERS = {
    "csv": CsvAdapter,
    "parquet": CsvAdapter,
    "anndata": AnnDataAdapter,
    "spatialdata": SpatialDataAdapter,
}

#: Extensions the single "Data" input on the upload page accepts, mapped to the
#: adapter that reads them. A directory is decided by inspecting it (a .zarr
#: store holding a `tables/` group is SpatialData, otherwise it is a
#: zarr-backed AnnData), since the extension alone cannot tell them apart.
_SUFFIX_TYPES = {
    **FLAT_TABLE_SUFFIXES,
    ".h5ad": "anndata",
}

#: What to tell the user when a path is none of the above. Kept here rather
#: than in the route so the accepted list cannot drift from the table above.
SUPPORTED_DATA_DESCRIPTION = "a .csv, .parquet, .h5ad or .zarr file"


def get_adapter(data_type: str):
    """Look up the adapter class registered for a project's data type."""
    try:
        return _ADAPTERS[data_type]
    except KeyError:
        raise ValueError(f"Unknown datasource data_type: {data_type!r}") from None


def detect_data_type(path) -> str:
    """Which adapter reads the file at `path`.

    The upload page has one Data input rather than a tab per format, so this
    is what decides where a dropped path goes. It reads the filesystem -- a
    .zarr store is a directory, and only looking inside distinguishes a
    SpatialData store from a plain zarr-backed AnnData -- so it is a detection
    step, not a string parse.

    Raises ValueError naming the accepted formats, which is what the upload
    form shows: an unrecognized path is ordinary user error, not a bug.
    """
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(path):
        return _detect_remote_data_type(path)
    path = Path(path).expanduser()
    if not path.exists():
        raise ValueError(f"No such file: {path}")

    suffix = path.suffix.lower()
    if path.is_dir():
        if suffix != ".zarr":
            raise ValueError(
                f"{path.name} is a directory but not a .zarr store. Provide "
                f"{SUPPORTED_DATA_DESCRIPTION}."
            )
        return "spatialdata" if _has_spatialdata_tables(path) else "anndata"

    data_type = _SUFFIX_TYPES.get(suffix)
    if data_type is None:
        raise ValueError(
            f"Cannot read {path.name}: expected {SUPPORTED_DATA_DESCRIPTION}."
        )
    return data_type


#: The groups whose presence makes a store SpatialData -- the same set
#: `spatial_scene.is_spatialdata_store` accepts, plus `tables`.
_SPATIALDATA_GROUPS = ("tables", "images", "labels", "points", "shapes")


def _detect_remote_data_type(url) -> str:
    """`detect_data_type` for a web address: only zarr is read from the web.

    SpatialData when any of its element groups is there, AnnData when `obs`
    and `var` are, and otherwise a sentence saying what was found. Each check
    is one cached metadata read, so nothing here needs the host to list.
    """
    from plexora.server.providers.base import RemoteUnreachable
    from plexora.server.utils import ome_zarr, remote_store

    try:
        view = ome_zarr._RemoteView.of(url)
        if not view.exists():
            result = remote_store.probe(url)
            if result.status == "offline":
                raise RemoteUnreachable(result.detail, url)
            raise ValueError(f"Nothing at {url} answers as a zarr store.")
        # Every group this asks about, in one concurrent round.
        view.store.read_many(
            [view._key(name, doc) for name in _SPATIALDATA_GROUPS + ("obs", "var")
             for doc in ("zarr.json", ".zgroup", ".zattrs", ".zarray")],
            concurrency=16)
        if any(view.is_group(name) for name in _SPATIALDATA_GROUPS):
            return "spatialdata"
        if view.exists("obs") and view.exists("var"):
            return "anndata"
    except remote_store.RemoteSupportMissing as error:
        raise ValueError(str(error)) from None
    except RemoteUnreachable as error:
        raise ValueError(str(error)) from None
    raise ValueError(
        f"{remote_store.url_name(url)} is a zarr store but neither a SpatialData "
        "store nor an AnnData (no tables/images/labels groups, no obs and var).")


def _has_spatialdata_tables(store) -> bool:
    """Whether a .zarr directory is a SpatialData store rather than a bare
    AnnData written to zarr.

    ONE answer, `spatial_scene.is_spatialdata_store`, because there used to be
    two and they disagreed: this asked for a `tables/` group, the scene reader
    accepts any of `images/labels/points/shapes`, and `resolve_image_path` has a
    third notion again. A store holding a morphology image and no table is a
    SpatialData store -- calling it AnnData sent it to a reader that cannot open
    it, which is what an import of a segmented-but-unquantified run hit.

    Structural and cheap either way: nothing is opened, so a store with
    thousands of chunks costs a handful of stats.

    The name is kept because that is what the caller's branch means by it: with
    no tables the store is still read as spatialdata, and `deferred_spec`
    records `unresolved=("table",)` rather than refusing the import.
    """
    from plexora.server.utils import spatial_scene

    return spatial_scene.is_spatialdata_store(store)


__all__ = [
    "DatasourceAdapter",
    "MetadataColumn",
    "NormalizedDatasource",
    "CsvAdapter",
    "AnnDataAdapter",
    "SpatialDataAdapter",
    "FLAT_TABLE_SUFFIXES",
    "FLAT_TABLE_TYPES",
    "SUPPORTED_DATA_DESCRIPTION",
    "is_flat_table",
    "read_flat_table",
    "write_flat_table",
    "classify_columns",
    "classify_from_inspection",
    "detect_data_type",
    "get_adapter",
]
