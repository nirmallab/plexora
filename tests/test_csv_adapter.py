import numpy as np
import polars as pl

from plexora.server.models.adapters import CsvAdapter, get_adapter
from plexora.server.models.adapters.base import NormalizedDatasource


def _write_csv(path, count=16):
    xs = np.arange(count, dtype=np.float32) * 20 + 5
    ys = np.arange(count, dtype=np.float32) * 10 + 7
    marker_b = np.linspace(0, 8, count, dtype=np.float32)
    marker_b[0] = -np.inf
    df = pl.DataFrame(
        {
            "CellID": np.arange(count, dtype=np.uint32) + 100,
            "X_centroid": xs,
            "Y_centroid": ys,
            "phenotype": ["typeA", "typeB"] * (count // 2),
            "MarkerA": np.linspace(0, 10, count, dtype=np.float32),
            "MarkerB": marker_b,
        }
    )
    df.write_csv(path)
    return df


def _feature_config(csv_path, celltype=None):
    config = {
        "src": str(csv_path),
        "xCoordinate": "X_centroid",
        "yCoordinate": "Y_centroid",
        "idField": "CellID",
    }
    if celltype:
        config["celltype"] = celltype
    return config


def _reference_table(csv_path):
    """Reproduces data_model.py's pre-refactor inline load_datasource() body,
    which CsvAdapter.load_table() must remain behavior-identical to."""
    df = pl.read_csv(csv_path)
    df = df.with_row_index("id").with_columns(pl.col("id").cast(pl.Int64))
    numeric_cols = [c for c, dt in df.schema.items() if dt in (pl.Float32, pl.Float64)]
    df = df.with_columns([
        pl.when(pl.col(c) == float("-inf")).then(0).otherwise(pl.col(c)).alias(c)
        for c in numeric_cols
    ])
    return df


def test_load_table_matches_pre_refactor_inline_logic(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path)

    normalized = CsvAdapter(_feature_config(csv_path)).load_table()

    assert normalized.table.equals(_reference_table(csv_path))


def test_load_table_returns_normalized_datasource_metadata(tmp_path):
    csv_path = tmp_path / "cells.csv"
    df = _write_csv(csv_path, count=8)

    normalized = CsvAdapter(_feature_config(csv_path, celltype="phenotype")).load_table()

    assert isinstance(normalized, NormalizedDatasource)
    assert normalized.id_column == "id"
    assert normalized.x_column == "X_centroid"
    assert normalized.y_column == "Y_centroid"
    assert normalized.celltype_column == "phenotype"
    assert normalized.source_obs_ids == df["CellID"].cast(pl.Utf8).to_list()
    assert set(normalized.feature_columns) == {"MarkerA", "MarkerB"}


def test_load_table_negative_infinity_replaced_with_zero(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path)

    normalized = CsvAdapter(_feature_config(csv_path)).load_table()

    assert (normalized.table["MarkerB"] == float("-inf")).sum() == 0
    assert normalized.table["MarkerB"][0] == 0


def test_source_obs_ids_fall_back_to_positional_id_without_id_field(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path, count=4)

    feature_config = _feature_config(csv_path)
    del feature_config["idField"]
    normalized = CsvAdapter(feature_config).load_table()

    assert normalized.source_obs_ids == normalized.table["id"].cast(pl.Utf8).to_list()


def test_get_adapter_defaults_to_csv():
    assert get_adapter("csv") is CsvAdapter


def test_get_adapter_rejects_unknown_data_type():
    try:
        get_adapter("not-a-real-format")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown data_type")
