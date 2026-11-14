"""Map to cells writes two columns into a CSV and leaves every other cell's text alone.

The flat-table write reads the user's whole file and writes it back with the
two ROI columns added. The columns it adds are the only thing that may change:
a zero-padded sample id ('007'), a ZIP code ('02139'), a TRUE/FALSE flag or a
number written '1E3' is the user's own text, and joins against their clinical
sheets are keyed on exactly that text. Re-parsing those columns with type
inference and letting polars re-spell them ('7', 'true', '1000.0') changes the
user's measurements file in columns Plexora has no business touching -- the
"data-loss bug wearing a feature's clothes" the adapters module promises not
to be.
"""

import csv
import types

from plexora.plugins.roi.server import adapters

ORIGINAL = (
    "CellID,sample,zip,barcode,flag,area\n"
    "1,007,02139,0099,TRUE,1E3\n"
    "2,010,00501,0100,FALSE,2.50\n"
)


def _dataset(path):
    source = types.SimpleNamespace(path=str(path), subset=None)
    return types.SimpleNamespace(source_kind="csv",
                                 table=types.SimpleNamespace(source=source))


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_map_to_cells_keeps_the_text_of_columns_it_did_not_add(tmp_path):
    """Every original column reads back exactly as the user wrote it."""
    path = tmp_path / "cells.csv"
    path.write_bytes(ORIGINAL.encode("utf-8"))
    before = _rows(path)

    adapters.write_cell_columns(_dataset(path), ["Tumor", ""], ["Tumor 1", ""],
                                prefix="roi")

    after = _rows(path)
    width = len(before[0])
    assert [row[:width] for row in after] == before
    assert len(after[0]) == width + 2
