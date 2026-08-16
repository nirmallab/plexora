"""Gating's saved gates survive the move to namespaced plugin storage.

This is the regression the storage change could plausibly break: a user's
thresholds live in the database between sessions, and losing them is silent --
the panel simply comes back at defaults. Exercised through gating's own
save/get functions against a really-registered datasource rather than through
the store directly, so the pickling and the polars round trip are covered too.
"""

import sqlite3

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import data_model, database_model
from plexora.server.modules.gating import model as gating_model
from plexora.server.modules.gating.database import LEGACY_STATE_TABLE


@pytest.fixture
def project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    pl.DataFrame(
        {
            "CellID": np.arange(8, dtype=np.uint32),
            "X_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "Y_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "MarkerA": np.linspace(0, 7, 8, dtype=np.float32),
            "MarkerB": np.linspace(7, 0, 8, dtype=np.float32),
        }
    ).write_csv(csv_path)

    for module in (plexora, data_model, database_model):
        monkeypatch.setattr(module, "data_path", data_dir, raising=False)
        monkeypatch.setattr(module, "config_json_path", config_path, raising=False)

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="gate_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=None,
        data_dir=data_dir,
    )
    return "gate_sample", data_dir


def _tables(data_dir, name):
    db_file = data_dir / name / f"{name}.db"
    if not db_file.exists():
        return []
    conn = sqlite3.connect(str(db_file))
    try:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


def _legacy_model():
    """The pre-namespacing table, as an older build would have written it."""
    return type("LegacyTable", (), {"__tablename__": LEGACY_STATE_TABLE})


def _by_channel(rows):
    return {row["channel"]: row for row in rows}


def test_gates_round_trip(project):
    name, _ = project
    channels = {"MarkerA": [0.0, 7.0], "MarkerB": [0.0, 7.0]}
    gates = {"MarkerA": [2.0, 5.0]}

    gating_model.save_gating_list(name, gates, channels)
    restored = _by_channel(gating_model.get_saved_gating_list(name))

    assert restored["MarkerA"]["gate_active"] is True
    assert restored["MarkerA"]["gate_start"] == pytest.approx(2.0)
    assert restored["MarkerA"]["gate_end"] == pytest.approx(5.0)
    # An untouched marker is persisted too, but inactive -- that is what lets
    # the panel restore every slider, not just the selected one.
    assert restored["MarkerB"]["gate_active"] is False


def test_nothing_saved_reads_as_none(project):
    name, _ = project
    assert gating_model.get_saved_gating_list(name) is None


def test_saving_uses_the_namespaced_table(project):
    name, data_dir = project
    gating_model.save_gating_list(name, {"MarkerA": [1.0, 2.0]}, {"MarkerA": [0.0, 7.0]})

    tables = _tables(data_dir, name)
    assert "plugin_gating_state" in tables
    # The bare table that used to be written straight into the shared project
    # database, and then outlived the module that made it.
    assert LEGACY_STATE_TABLE not in tables


def test_gates_saved_by_an_older_build_still_load(project):
    """The upgrade path. A user with gates saved before namespacing existed
    must find them exactly where they left them."""
    name, _ = project
    legacy_rows = [
        {"channel": "MarkerA", "gate_start": 1.5, "gate_end": 6.5, "gate_active": True},
        {"channel": "MarkerB", "gate_start": 0.0, "gate_end": 7.0, "gate_active": False},
    ]
    import pickle

    database_model.save_list(
        _legacy_model(), datasource=name, cells=pickle.dumps(legacy_rows, protocol=4)
    )

    restored = _by_channel(gating_model.get_saved_gating_list(name))
    assert restored["MarkerA"]["gate_start"] == pytest.approx(1.5)
    assert restored["MarkerA"]["gate_active"] is True


def test_reading_an_older_project_converts_it(project):
    """Migrate and delete. After one read the project is fully on namespaced
    storage and the table nothing could name is gone."""
    name, data_dir = project
    import pickle

    database_model.save_list(
        _legacy_model(),
        datasource=name,
        cells=pickle.dumps([{"channel": "MarkerA", "gate_start": 1.5, "gate_end": 6.5,
                             "gate_active": True}], protocol=4),
    )

    gating_model.get_saved_gating_list(name)

    tables = _tables(data_dir, name)
    assert "plugin_gating_state" in tables
    assert LEGACY_STATE_TABLE not in tables

    gating_model.save_gating_list(name, {"MarkerA": [3.0, 4.0]}, {"MarkerA": [0.0, 7.0]})
    restored = _by_channel(gating_model.get_saved_gating_list(name))
    assert restored["MarkerA"]["gate_start"] == pytest.approx(3.0)
