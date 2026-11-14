"""Naming the channels of a SpatialData table reads its var, never its matrix.

`register_anndata_datasource` validates a table through `plan()`, which reads
only obs and var, so a table too large to load can still be registered. The
channel names come from the same table's var_names (or uns['all_markers']), so
deriving them must not read the whole AnnData either: on a Visium HD bin table
that is gigabytes, and on a store at a web address it is a refusal ("too large
to read over the network") after `plan()` already said yes.
"""

import pytest

from plexora import datasource
from plexora.server.models.adapters import spatialdata_adapter
from tests.helpers import use_data_root

spatialdata = pytest.importorskip("spatialdata")

from tests.test_spatialdata_image_import import _store  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    use_data_root(monkeypatch, directory)
    return directory


def _no_whole_table_read(*args, **kwargs):
    raise AssertionError("the whole table (its X included) was read to name channels")


def test_registering_a_spatialdata_table_names_channels_without_reading_its_matrix(
        tmp_path, data_dir, monkeypatch):
    store = _store(tmp_path / "sample.zarr", size=128)
    monkeypatch.setattr(spatialdata_adapter, "read_spatialdata_table", _no_whole_table_read)
    monkeypatch.setattr(spatialdata_adapter, "read_remote_table", _no_whole_table_read)

    entry = datasource.register_spatialdata_datasource(
        name="names_from_var", image=store, store=store, table="cells",
        data_dir=data_dir)

    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3", "CD8"]
