"""Writing plugin columns back into a user's delimited table keeps its format.

The ROI plugin's "Map to cells" and QC's "Save into file" both write a new
table to a temporary file beside the user's own and rename it over the
original. `write_flat_table` keeps a file's separator and decimal mark
(tests/test_delimited_tables.py), but only when it can see them; these tests go
through the callers' own path, so a `.tsv` must still be tab-separated and a
European `;` / `1,5` export must still use semicolons and decimal commas
after the write.
"""

from types import SimpleNamespace

import polars as pl

from plexora.plugins.qc.server import source_write
from plexora.plugins.roi.server import adapters

HEADER = ["CellID", "sample", "X_centroid"]
ROWS = [(1, "A", "10.5"), (2, "A", "20.25")]


def _table(path, separator, decimal="."):
    lines = [separator.join(HEADER)]
    for cell, sample, x in ROWS:
        lines.append(separator.join([str(cell), sample, x.replace(".", decimal)]))
    # Bytes, not write_text: on Windows write_text would add CRLF.
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def _dataset(path):
    return SimpleNamespace(
        source_kind="csv",
        table=SimpleNamespace(source=SimpleNamespace(path=str(path), subset=None)),
    )


def _roi_write(path):
    adapters.write_cell_columns(_dataset(path), ["Tumor", ""], ["Tumor 1", ""],
                                prefix="rois")


def _qc_write(path):
    qc = (pl.DataFrame({"plexora_qc_pass": [True, False]}),)
    source_write._write_flat(_dataset(path), "csv", qc, None, False)


def _header(path):
    return path.read_bytes().decode("utf-8").splitlines()[0]


def test_roi_map_to_cells_keeps_a_tsv_tab_separated(tmp_path):
    """ROI columns written into cells.tsv leave it a tab-separated file."""
    path = _table(tmp_path / "cells.tsv", "\t")
    _roi_write(path)
    assert _header(path) == "\t".join(HEADER + ["rois_category", "rois_name"])


def test_roi_map_to_cells_keeps_semicolons_and_decimal_commas(tmp_path):
    """ROI columns written into a `;` / `1,5` export keep both."""
    path = _table(tmp_path / "eu.csv", ";", decimal=",")
    _roi_write(path)
    text = path.read_bytes().decode("utf-8")
    assert _header(path) == ";".join(HEADER + ["rois_category", "rois_name"])
    assert "10,5" in text and "10.5" not in text


def test_qc_save_into_file_keeps_a_tsv_tab_separated(tmp_path):
    """QC's columns written into cells.tsv leave it a tab-separated file."""
    path = _table(tmp_path / "cells.tsv", "\t")
    _qc_write(path)
    assert _header(path) == "\t".join(HEADER + ["plexora_qc_pass"])


def test_qc_save_into_file_keeps_semicolons_and_decimal_commas(tmp_path):
    """QC's columns written into a `;` / `1,5` export keep both."""
    path = _table(tmp_path / "eu.csv", ";", decimal=",")
    _qc_write(path)
    text = path.read_bytes().decode("utf-8")
    assert _header(path) == ";".join(HEADER + ["plexora_qc_pass"])
    assert "10,5" in text and "10.5" not in text
