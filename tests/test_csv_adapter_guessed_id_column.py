"""A flat table whose only cell-id column is literally called `id`.

`classify.guess_roles` names a bare `id` as the cell id (its last cell_id
pattern), and `CsvAdapter._without_own_id` refuses any role naming `id` whose
values are not the row number. A quantification table keyed by mask labels
1..N therefore registered with a guess that made the table fail on every load.
"""
import numpy as np
import polars as pl

from plexora.datasource import flat_table_spec
from plexora.server.models.adapters import CsvAdapter
from plexora.server.models.adapters.inspection import inspect_flat_table


def _write_labelled_table(path, count=6):
    labels = np.arange(count, dtype=np.int64) + 1  # mask labels 1..N
    pl.DataFrame({
        "id": labels,
        "x": np.linspace(5, 50, count),
        "y": np.linspace(7, 70, count),
        "CD3": np.linspace(0, 3, count),
    }).write_csv(path)
    return labels.tolist()


def test_a_table_whose_label_column_is_called_id_loads_with_the_guessed_roles(tmp_path):
    """The roles the import screen predicts from the header must be roles the
    adapter can read: the table loads, and if a cell id role was guessed, it
    still carries the file's mask labels rather than being refused."""
    csv_path = tmp_path / "cells.csv"
    labels = _write_labelled_table(csv_path)

    inspection = inspect_flat_table(csv_path, "csv")
    spec = flat_table_spec(str(csv_path), inspection["columns"])

    normalized = CsvAdapter(spec).load_table()

    assert normalized.table.height == len(labels)
    cell_id = spec.roles.cell_id
    if cell_id is not None and cell_id != "id":
        assert normalized.table[cell_id].to_list() == labels
