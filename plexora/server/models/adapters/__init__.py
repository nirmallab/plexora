from .base import DatasourceAdapter, NormalizedDatasource
from .csv_adapter import CsvAdapter
from .anndata_adapter import AnnDataAdapter

_ADAPTERS = {
    "csv": CsvAdapter,
    "anndata": AnnDataAdapter,
}


def get_adapter(data_type: str):
    """Look up the adapter class registered for a config['data_type'] value.

    Absence of a 'data_type' key in a datasource's config entry means
    'csv' -- the format every datasource used before this dispatch existed.
    """
    try:
        return _ADAPTERS[data_type]
    except KeyError:
        raise ValueError(f"Unknown datasource data_type: {data_type!r}") from None


__all__ = ["DatasourceAdapter", "NormalizedDatasource", "CsvAdapter", "AnnDataAdapter", "get_adapter"]
