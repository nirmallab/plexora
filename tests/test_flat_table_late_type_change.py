"""A CSV whose column only shows its real type after the first 100 rows loads.

polars infers a CSV's dtypes from its first 100 rows. A quantification table
exported from R writes a missing value as `NA` and a whole number as `0`, so a
marker that is missing only further down, or that is whole for the first
hundred cells and fractional later, made the full read fail with a parse error
and the project could not be opened at all.
"""

import numpy as np
import polars as pl

from plexora.server.models.adapters import CsvAdapter
from plexora.server.models.adapters.flat_table import read_flat_table
from plexora.server.models.project import ColumnGroups, ColumnRoles, DataSpec

HEADER = "CellID,X_centroid,Y_centroid,CD3,CD20"


def _write(path, rows):
    lines = [HEADER] + [",".join(str(v) for v in row) for row in rows]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def _na_after_row_100(tmp_path):
    rows = [[i, i + 0.5, i + 0.25, 1.5 + i, 0.5] for i in range(150)]
    rows.append([150, 150.5, 150.25, "NA", 0.5])
    return _write(tmp_path / "na_late.csv", rows)


def _whole_then_fractional(tmp_path):
    rows = [[i, i + 0.5, i + 0.25, 0, 0.5] for i in range(150)]
    rows.append([150, 150.5, 150.25, 300.5, 0.5])
    return _write(tmp_path / "ints_then_float.csv", rows)


def test_na_after_row_100_reads_as_a_missing_number(tmp_path):
    frame = read_flat_table(_na_after_row_100(tmp_path), "csv")
    assert frame.height == 151
    assert frame.schema["CD3"].is_float()
    value = frame["CD3"][150]
    assert value is None or np.isnan(value)


def test_whole_numbers_then_a_decimal_after_row_100_read_as_floats(tmp_path):
    frame = read_flat_table(_whole_then_fractional(tmp_path), "csv")
    assert frame.height == 151
    assert frame.schema["CD3"].is_float()
    assert frame["CD3"][150] == 300.5


def test_a_project_over_such_a_table_loads(tmp_path):
    spec = DataSpec(
        src=str(_whole_then_fractional(tmp_path)), type="csv",
        roles=ColumnRoles(x="X_centroid", y="Y_centroid", cell_id="CellID"),
        columns=ColumnGroups(markers=["CD3", "CD20"]))
    table = CsvAdapter(spec).load_table().table
    assert table.height == 151
    assert table.schema["CD3"] == pl.Float64
