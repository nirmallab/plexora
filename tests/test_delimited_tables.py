"""Delimited cell tables: the separator is read from the file, not assumed.

The docs promise `.tsv` (tab-separated) and `.txt` (tab- or comma-separated),
and every read of a delimited table -- the schema read, the full load, the
feature sample -- goes through `read_flat_table`, so the sniff lives there.
A tab-separated table read with polars' default comma used to arrive as ONE
column whose name was the whole header line.
"""

import numpy as np
import polars as pl
import pytest

from plexora.server.models.adapters import CsvAdapter
from plexora.server.models.adapters.flat_table import (
    delimited_options,
    read_flat_table,
    sniff_separator,
    write_flat_table,
)
from plexora.server.models.adapters.inspection import inspect_flat_table
from plexora.server.models.project import ColumnGroups, ColumnRoles, DataSpec

HEADER = ["CellID", "X_centroid", "Y_centroid", "CD3", "CD20"]
ROWS = [[1, 10.5, 20.25, 3.0, 0.5], [2, 11.0, 21.0, 4.0, 1.5], [3, 12.0, 22.0, 5.0, 2.5]]


def _text(separator, *, decimal=".", bom=False, crlf=False):
    end = "\r\n" if crlf else "\n"
    lines = [separator.join(HEADER)]
    for row in ROWS:
        lines.append(separator.join(str(v).replace(".", decimal) for v in row))
    data = end.join(lines) + end
    return ("﻿" if bom else "") + data


CASES = {
    "tab.tsv": dict(separator="\t"),
    "tab.txt": dict(separator="\t"),
    "comma.txt": dict(separator=","),
    "comma.tsv": dict(separator=","),  # a .tsv holding commas: content wins
    "semicolon.csv": dict(separator=";"),
    "semicolon_decimal_comma.csv": dict(separator=";", decimal=","),
    "bom_crlf.tsv": dict(separator="\t", bom=True, crlf=True),
    "bom_crlf.csv": dict(separator=",", bom=True, crlf=True),
}


@pytest.fixture(params=sorted(CASES))
def table(request, tmp_path):
    path = tmp_path / request.param
    path.write_bytes(_text(**CASES[request.param]).encode("utf-8"))
    return path


def _check(frame, rows=len(ROWS)):
    assert frame.columns == HEADER
    assert frame.height == rows
    assert frame["CellID"].to_list() == [r[0] for r in ROWS][:rows]
    np.testing.assert_allclose(frame["Y_centroid"].to_numpy(),
                               [r[2] for r in ROWS][:rows])
    assert frame.schema["CD20"].is_float()


def test_full_read(table):
    _check(read_flat_table(table, "csv"))


def test_prefix_read(table):
    """The schema/classification read (n_rows), which takes `csv_head`'s
    bytes rather than the path."""
    _check(read_flat_table(table, "csv", n_rows=2), rows=2)


def test_inspection_lists_the_real_columns(table):
    names = [c["name"] for c in inspect_flat_table(table)["columns"]]
    assert names == HEADER


def test_adapter_load_and_sample(table):
    spec = DataSpec(type="csv", src=str(table),
                    roles=ColumnRoles(cell_id="CellID", x="X_centroid", y="Y_centroid"),
                    columns=ColumnGroups(markers=("CD3", "CD20")))
    adapter = CsvAdapter(spec)
    loaded = adapter.load_table()
    assert loaded.feature_columns == ["CD3", "CD20"]
    np.testing.assert_allclose(loaded.table["CD20"].to_numpy(), [r[4] for r in ROWS])
    sample = adapter.sample_features(2)
    assert sorted(sample) == ["CD20", "CD3"] and len(sample["CD3"]) == 2


def test_sniffing_reads_the_header_not_the_extension(tmp_path):
    path = tmp_path / "cells.tsv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    assert sniff_separator(path) == ","
    path = tmp_path / "cells.csv"
    path.write_text("a\tb\n1\t2\n", encoding="utf-8")
    assert sniff_separator(path) == "\t"


def test_a_quoted_separator_in_a_name_is_not_counted(tmp_path):
    path = tmp_path / "cells.csv"
    path.write_text('"CD3; mean","CD20; mean",X\n1,2,3\n', encoding="utf-8")
    assert sniff_separator(path) == ","
    assert read_flat_table(path).columns == ["CD3; mean", "CD20; mean", "X"]


def test_one_column_table_reads_as_one_column(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("CellID\n1\n2\n", encoding="utf-8")
    assert read_flat_table(path).columns == ["CellID"]


def test_semicolon_with_point_decimals_stays_numeric(tmp_path):
    """`decimal_comma` would read `1.5` as text, so it is only asked for when
    the numbers are written with commas."""
    path = tmp_path / "cells.csv"
    path.write_text(_text(";"), encoding="utf-8")
    assert "decimal_comma" not in delimited_options(path)
    path.write_text(_text(";", decimal=","), encoding="utf-8")
    assert delimited_options(path).get("decimal_comma") is True


def test_write_back_keeps_the_files_separator(tmp_path):
    """The ROI and QC plugins write their columns back into the user's own
    table; a .tsv must come back a .tsv."""
    path = tmp_path / "cells.tsv"
    path.write_text(_text("\t"), encoding="utf-8")
    frame = read_flat_table(path).with_columns(pl.lit("in").alias("roi"))
    write_flat_table(frame, path)
    assert path.read_text(encoding="utf-8").splitlines()[0] == "\t".join(HEADER + ["roi"])
    assert read_flat_table(path).columns == HEADER + ["roi"]

    path = tmp_path / "eu.csv"
    path.write_text(_text(";", decimal=","), encoding="utf-8")
    frame = read_flat_table(path)
    write_flat_table(frame, path)
    assert "10,5" in path.read_text(encoding="utf-8")
    np.testing.assert_allclose(read_flat_table(path)["X_centroid"].to_numpy(),
                               [r[1] for r in ROWS])
