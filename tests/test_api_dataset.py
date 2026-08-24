"""The plugin-facing dataset contract.

Two halves: role resolution, which is pure config and must stay cheap; and the
handles, exercised end to end against a really-registered datasource so the API
cannot pass while being unable to answer the questions a plugin actually asks.
"""

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora import api
from plexora.api import DatasetSchema
from plexora.server.models import data_model, database_model

from tests.helpers import csv_spec, project, use_data_root


# --------------------------------------------------------------------------
# Role resolution (pure config)
# --------------------------------------------------------------------------

def test_schema_resolves_roles_from_the_project_record():
    schema = DatasetSchema.from_project(project(dataset=csv_spec("/tmp/cells.csv")))
    assert (schema.cell_id, schema.x, schema.y) == ("CellID", "X_centroid", "Y_centroid")


def test_schema_is_none_without_feature_data():
    assert DatasetSchema.from_project(project(dataset=None)) is None


def test_an_uncollected_role_resolves_to_none():
    """A project may have a table long before anything has said which column
    holds what. Plugins must tolerate None -- and declare the role in Requires
    if they cannot, so the host asks for it."""
    spec = csv_spec("/tmp/cells.csv", image_id=None)
    assert DatasetSchema.from_project(project(dataset=spec)).image_id is None

    spec = csv_spec("/tmp/cells.csv", image_id="sample_id")
    assert DatasetSchema.from_project(project(dataset=spec)).image_id == "sample_id"


def test_the_schema_carries_roles_and_nothing_else():
    """`src` is an absolute path on the server and the processing flags are not
    column roles. Neither is a plugin's business, so neither may ride along in
    the role map -- the old shape leaked both because roles shared a dict with
    them."""
    schema = DatasetSchema.from_project(project(dataset=csv_spec("/srv/secret/cells.csv")))
    assert schema.extra == {}
    assert "/srv/secret" not in repr(schema)


def test_unknown_datasource_raises(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(KeyError):
        api.dataset("does_not_exist")


# --------------------------------------------------------------------------
# Handles, against a real datasource
# --------------------------------------------------------------------------

@pytest.fixture
def registered(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    # Redirect every module that captured data_path at import time, BEFORE
    # anything runs. database_model is the one that is easy to miss: loading a
    # datasource reads its saved channel list, which creates
    # <data_path>/<name>/<name>.db. Miss it and the test writes a stray project
    # into the developer's real data directory (which is where this repo's
    # tracked plexora/data/*_sample directories came from).
    use_data_root(monkeypatch, data_dir)

    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    pl.DataFrame(
        {
            "CellID": np.arange(8, dtype=np.uint32),
            "X_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "Y_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "MarkerA": np.linspace(0, 7, 8, dtype=np.float32),
        }
    ).write_csv(csv_path)

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="api_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=None,
        data_dir=data_dir,
    )
    return api.dataset("api_sample")


def test_image_handle_reports_real_channels(registered):
    names = registered.image.channel_names
    assert names, "every plugin is guaranteed image data"
    # The 'Area' placeholder only exists when segmentation was registered, and
    # is not a real channel either way.
    assert "Area" not in names


def test_schema_round_trips_through_registration(registered):
    assert registered.schema.x == "X_centroid"
    assert registered.schema.y == "Y_centroid"


def test_markers_are_table_columns_not_image_channels(registered):
    """The two lists are routinely different -- a structural channel like DNA is
    a real image channel with no feature column. Conflating them is a
    long-standing bug source, so pin that they are computed separately."""
    assert "MarkerA" in registered.table.markers
    assert registered.table.markers != registered.image.channel_names


def test_table_frame_carries_the_positional_id_column(registered):
    frame = registered.table.frame()
    assert frame is not None
    assert "id" in frame.columns
    assert len(frame) == 8


def test_ids_matching_applies_gates(registered):
    everything = registered.table.ids_matching({"MarkerA": (-1, 999)})
    narrow = registered.table.ids_matching({"MarkerA": (2.5, 4.5)})
    assert len(everything) == 8
    assert 0 < len(narrow) < 8
    assert set(narrow).issubset(set(everything))


def test_ids_matching_is_empty_without_gates(registered):
    assert registered.table.ids_matching({}) == []


def test_an_annotation_column_is_readable_without_naming_the_format(registered):
    """The surface a cell-colouring plugin reads. Exercised here as well as in
    tests/test_metadata_columns.py because this file is where the API contract
    lives: a handle that loses this method breaks every such plugin, and the
    breakage is a panel with an empty dropdown rather than an error."""
    column = registered.table.metadata_values("X_centroid")
    assert len(column.values) == 8
    assert "X_centroid" in registered.table.metadata_columns


def test_or_mode_widens_rather_than_narrows(registered):
    gates = {"MarkerA": (0.5, 1.5), "X_centroid": (150, 300)}
    conjunction = registered.table.ids_matching(gates, mode="and")
    disjunction = registered.table.ids_matching(gates, mode="or")
    assert set(conjunction).issubset(set(disjunction))
    assert len(disjunction) > len(conjunction)
