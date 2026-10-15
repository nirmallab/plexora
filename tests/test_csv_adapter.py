import numpy as np
import polars as pl
import pytest

from plexora.server.models.adapters import CsvAdapter, get_adapter
from plexora.server.models.adapters.base import NormalizedDatasource

from tests.helpers import csv_spec


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


def _spec(csv_path, **overrides):
    return csv_spec(csv_path, **overrides)


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

    normalized = CsvAdapter(_spec(csv_path)).load_table()

    assert normalized.table.equals(_reference_table(csv_path))


def test_load_table_returns_normalized_datasource_metadata(tmp_path):
    csv_path = tmp_path / "cells.csv"
    df = _write_csv(csv_path, count=8)

    normalized = CsvAdapter(_spec(csv_path, celltype="phenotype")).load_table()

    assert isinstance(normalized, NormalizedDatasource)
    assert normalized.id_column == "id"
    assert normalized.x_column == "X_centroid"
    assert normalized.y_column == "Y_centroid"
    assert normalized.celltype_column == "phenotype"
    # A CSV's table IS the file, so the identifier column the project named is
    # carried in the table itself -- there is no second place for it to live.
    assert normalized.table["CellID"].to_list() == df["CellID"].to_list()
    assert set(normalized.feature_columns) == {"MarkerA", "MarkerB"}


def test_load_table_negative_infinity_replaced_with_zero(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path)

    normalized = CsvAdapter(_spec(csv_path)).load_table()

    assert (normalized.table["MarkerB"] == float("-inf")).sum() == 0
    assert normalized.table["MarkerB"][0] == 0


def test_positional_id_is_synthesized_without_an_id_field(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv(csv_path, count=4)

    normalized = CsvAdapter(_spec(csv_path, cell_id=None)).load_table()

    assert normalized.id_column == "id"
    assert normalized.table["id"].to_list() == [0, 1, 2, 3]


def _write_csv_with_own_id(path, ids):
    pl.DataFrame({
        "id": ids,
        "CellID": np.arange(len(ids), dtype=np.uint32) + 1,
        "X_centroid": np.linspace(1, 4, len(ids)),
        "Y_centroid": np.linspace(1, 4, len(ids)),
        "MarkerA": np.linspace(0, 3, len(ids)),
    }).write_csv(path)


def test_a_table_with_its_own_id_column_loads(tmp_path):
    """Polars refused to add the row number under a name the file already
    had, so a table carrying an `id` column -- an exported index -- did not
    load at all, and the edit page reported it as a JSON parse error."""
    csv_path = tmp_path / "cells.csv"
    _write_csv_with_own_id(csv_path, [7, 3, 9, 1])

    normalized = CsvAdapter(_spec(csv_path)).load_table()

    assert normalized.table["id"].to_list() == [0, 1, 2, 3]
    assert normalized.table["CellID"].to_list() == [1, 2, 3, 4]
    assert normalized.table.columns.count("id") == 1


def test_a_role_on_a_table_id_that_is_not_the_row_number_is_refused(tmp_path):
    """The role would otherwise read the row number in its place, silently."""
    csv_path = tmp_path / "cells.csv"
    _write_csv_with_own_id(csv_path, [7, 3, 9, 1])

    with pytest.raises(ValueError, match="its own column called 'id'"):
        CsvAdapter(_spec(csv_path, cell_id="id")).load_table()


def test_a_role_on_a_table_id_that_is_the_row_number_loads(tmp_path):
    csv_path = tmp_path / "cells.csv"
    _write_csv_with_own_id(csv_path, [0, 1, 2, 3])

    normalized = CsvAdapter(_spec(csv_path, cell_id="id")).load_table()

    assert normalized.table["id"].to_list() == [0, 1, 2, 3]


def test_get_adapter_defaults_to_csv():
    assert get_adapter("csv") is CsvAdapter


def test_get_adapter_rejects_unknown_data_type():
    try:
        get_adapter("not-a-real-format")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown data_type")
