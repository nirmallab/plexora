"""Which adapter reads the file the user dropped in the one Data input.

The upload page has a single Data field rather than a tab per format, so this
is what decides where a path goes. It reads the filesystem on purpose: a .zarr
store is a directory, and only looking inside distinguishes a SpatialData store
from a plain zarr-backed AnnData.
"""

import pytest

from plexora.server.models.adapters import (
    SUPPORTED_DATA_DESCRIPTION,
    detect_data_type,
)
from plexora.server.models.adapters.spatialdata_adapter import TABLES_GROUP


def test_a_csv_is_a_csv(tmp_path):
    path = tmp_path / "cells.csv"
    path.write_text("CellID,CD3\n1,2\n", encoding="utf-8")
    assert detect_data_type(path) == "csv"


@pytest.mark.parametrize("suffix", [".csv", ".tsv", ".txt", ".CSV"])
def test_the_delimited_text_extensions_all_route_to_csv(tmp_path, suffix):
    path = tmp_path / f"cells{suffix}"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    assert detect_data_type(path) == "csv"


def test_an_h5ad_is_anndata(tmp_path):
    path = tmp_path / "cells.h5ad"
    path.write_bytes(b"")
    assert detect_data_type(path) == "anndata"


def test_a_zarr_holding_tables_is_a_spatialdata_store(tmp_path):
    """Structural, and cheap: SpatialData keeps its tables under a `tables/`
    group and a plain AnnData has no such thing. Nothing is opened, so a store
    with thousands of chunks costs one stat."""
    store = tmp_path / "sample.zarr"
    (store / TABLES_GROUP / "cells").mkdir(parents=True)
    assert detect_data_type(store) == "spatialdata"


def test_a_zarr_without_tables_is_a_plain_anndata(tmp_path):
    store = tmp_path / "cells.zarr"
    (store / "obs").mkdir(parents=True)
    assert detect_data_type(store) == "anndata"


def test_a_missing_path_is_reported_as_such(tmp_path):
    with pytest.raises(ValueError, match="No such file"):
        detect_data_type(tmp_path / "nope.csv")


def test_an_unsupported_extension_names_what_is_accepted(tmp_path):
    """Ordinary user error, not a bug -- so the message has to say what the
    field takes, and say it in the same words the form's hint does."""
    path = tmp_path / "notes.docx"
    path.write_bytes(b"")
    with pytest.raises(ValueError) as excinfo:
        detect_data_type(path)
    assert SUPPORTED_DATA_DESCRIPTION in str(excinfo.value)


def test_a_directory_that_is_not_a_zarr_store_is_rejected(tmp_path):
    folder = tmp_path / "results"
    folder.mkdir()
    with pytest.raises(ValueError, match="directory but not a .zarr"):
        detect_data_type(folder)
