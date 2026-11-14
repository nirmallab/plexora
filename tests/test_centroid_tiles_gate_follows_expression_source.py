"""A gated centroid layer gates on the values the project reads NOW.

Turning log1p on (or pointing an AnnData project at another matrix) leaves the
table file exactly as it was -- same path, same size, same mtime -- but every
marker value the app shows changes. The centroid layer's gate filter must read
the new values, the same ones the loaded table, the histogram and the server's
gate rule (`get_filter_columns` + `apply_range_mask`) read, or the centroids
drawn as positive are not the cells the gate selects.
"""
import dataclasses

import numpy as np
import polars as pl

from plexora.server.models import centroid_tiles, data_model

from tests.autogate_fixtures import make_gating_project
from tests.helpers import csv_spec, entry


def _geometry(project_entry):
    return {**project_entry, "width": 1024, "height": 1024,
            "tileWidth": 256, "tileHeight": 256, "maxLevel": 1}


def _all_tiles(n=4):
    return [{"x": x, "y": y} for x in range(n) for y in range(n)]


def test_turning_on_log1p_gates_the_centroids_on_the_logged_values(tmp_path, monkeypatch):
    monkeypatch.setattr(centroid_tiles, "_filter_tables", {})
    count = 40
    csv_path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(count, dtype=np.uint32) + 1,
        "x": np.arange(count, dtype=np.float32) * 20 + 5,
        "y": np.arange(count, dtype=np.float32) * 20 + 5,
        "CD3": np.linspace(100.0, 1000.0, count),   # raw intensities
    }).write_csv(csv_path)
    raw = csv_spec(csv_path, cell_id="CellID", x="x", y="y", markers=("CD3",))
    name = "log_switch"
    raw_config = {name: _geometry(entry(name, dataset=raw))}

    # A gate on the raw scale, which also fills the gate-column cache.
    raw_gate = {"CD3": [500.0, 2000.0]}
    before = centroid_tiles.get_tiles(raw_config, name, 0, _all_tiles(), gates=raw_gate)
    assert 0 < len(before) < count

    # The user turns log1p on: the same file, read through log1p.
    logged = dataclasses.replace(raw, is_transformed=True)
    log_config = {name: _geometry(entry(name, dataset=logged))}
    # log1p(100..1000) = 4.6..6.9: every cell passes this gate on the new scale.
    log_gate = {"CD3": [0.0, 10.0]}
    after = centroid_tiles.get_tiles(log_config, name, 0, _all_tiles(), gates=log_gate)

    assert len(after) == count, (
        f"{len(after)} of {count} centroids pass a log1p-scale gate every logged "
        "value is inside: the gate is still applied to the raw values")


def test_the_centroid_gate_after_a_log1p_switch_agrees_with_the_loaded_table(
        tmp_path, monkeypatch):
    from plexora.api import features

    monkeypatch.setattr(centroid_tiles, "_filter_tables", {})
    info = make_gating_project(tmp_path, name="cgate", mask=False)
    name = info["name"]
    tiles = [{"x": x, "y": y} for x in range(6) for y in range(6)]  # 768 px / 128

    data_model.load_datasource(name, wait_for_table=True)
    raw_values = data_model.get_datasource_df()["CD3"].to_numpy()
    raw_gate = {"CD3": [float(np.median(raw_values)), float(raw_values.max()) + 1.0]}
    data_model.get_centroid_tiles(name, 0, tiles, raw_gate)

    # What the Edit page, the requirements modal or an agent's
    # set_expression_source does: one write, then a reload of this project.
    _, after, changed = features.apply(name, features_log=True)
    assert changed and after["features_log"] is True

    logged = data_model.get_datasource_df()["CD3"].to_numpy()
    assert logged.max() < raw_values.max()  # the reload did read through log1p
    low = float(np.median(logged))
    log_gate = {"CD3": [low, float(logged.max()) + 1.0]}
    expected = int(((logged > low) & (logged <= logged.max() + 1.0)).sum())

    # The server's own gate rule over the loaded table's columns
    # (`get_filter_columns` + `apply_range_mask`) reads the reloaded values.
    columns = data_model.get_filter_columns(name, ["CD3"])
    assert int(data_model.apply_range_mask(columns, log_gate).sum()) == expected

    centroids = data_model.get_centroid_tiles(name, 0, tiles, log_gate)
    assert len(centroids) == expected, (
        f"the centroid layer draws {len(centroids)} cells for a gate the loaded "
        f"(log1p) table puts {expected} cells inside")
