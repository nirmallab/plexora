from pathlib import Path

from .base import DatasourceAdapter, NormalizedDatasource
from .classify import classify_columns, classify_from_inspection
from .csv_adapter import CsvAdapter
from .anndata_adapter import AnnDataAdapter
from .spatialdata_adapter import SpatialDataAdapter

_ADAPTERS = {
    "csv": CsvAdapter,
    "anndata": AnnDataAdapter,
    "spatialdata": SpatialDataAdapter,
}

#: Extensions the single "Data" input on the upload page accepts, mapped to the
#: adapter that reads them. A directory is decided by inspecting it (a .zarr
#: store holding a `tables/` group is SpatialData, otherwise it is a
#: zarr-backed AnnData), since the extension alone cannot tell them apart.
_SUFFIX_TYPES = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".h5ad": "anndata",
}

#: What to tell the user when a path is none of the above. Kept here rather
#: than in the route so the accepted list cannot drift from the table above.
SUPPORTED_DATA_DESCRIPTION = "a .csv, .h5ad or .zarr file"


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


def _has_spatialdata_tables(store) -> bool:
    """Whether a .zarr directory is a SpatialData store rather than a bare
    AnnData written to zarr. Structural, and cheap: SpatialData keeps its
    tables under a `tables/` group, and a plain AnnData has no such thing.
    Nothing is opened -- a store with thousands of chunks costs one stat."""
    from .spatialdata_adapter import TABLES_GROUP

    return (Path(store) / TABLES_GROUP).is_dir()


__all__ = [
    "DatasourceAdapter",
    "NormalizedDatasource",
    "CsvAdapter",
    "AnnDataAdapter",
    "SpatialDataAdapter",
    "SUPPORTED_DATA_DESCRIPTION",
    "classify_columns",
    "classify_from_inspection",
    "detect_data_type",
    "get_adapter",
]
