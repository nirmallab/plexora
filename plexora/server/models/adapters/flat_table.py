"""A table whose file IS the table.

CSV and Parquet are one format as far as everything above the read is
concerned: the columns on disk are the columns of the cell table, the
marker/metadata line is not drawn by the file (which is the whole reason the
classification screen exists), and there is nothing inside to choose between.
AnnData and SpatialData are the opposite -- containers holding tables, matrices
and annotations, where "which table?" and "which matrix?" are real questions.

That distinction is what the rest of the tree branches on, and it used to be
spelled `data_type == "csv"` in a dozen places. Spelled that way, adding a
second flat format means finding all dozen, and the one that gets missed is a
silent wrong answer rather than an error: `source_layers` would go looking for
`adata.layers` in a parquet, the edit page would offer the read-spec controls
instead of the column classifier. So the question has one name here,
`is_flat_table`, and the read and the write have one implementation each.

Parquet is read by polars directly -- no pyarrow. The `[spatial]` extra is for
the transcript-scale footer reads in `spatial_scene` (row counts and extents
out of a 6 GB file without touching a row); a 4 MB cell table is just read.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import polars as pl

#: The flat formats, as `DataSpec.type` records them. Both are read by the same
#: adapter -- the type names the encoding, not a different kind of table.
FLAT_TABLE_TYPES = ("csv", "parquet")

#: Suffix -> the type of flat table it holds. Folded into `_SUFFIX_TYPES` in
#: this package's `__init__`, so `detect_data_type` and this stay one list.
FLAT_TABLE_SUFFIXES = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
}


def is_flat_table(data_type) -> bool:
    """Whether this data type is a table read straight off its own file.

    The question every "is this a CSV?" branch was actually asking. False for
    None, which is what a project with no table has.
    """
    return data_type in FLAT_TABLE_TYPES


def read_flat_table(path, data_type="csv", *, n_rows=None) -> pl.DataFrame:
    """The file, as polars read it and before anything is done to it.

    `n_rows` reads a prefix -- enough to infer dtypes for the classification
    screen, or one row for a schema -- and None reads all of it. A CSV's
    prefix is parsed from its first records' bytes alone (`csv_head`): the
    same rows, read the same way, but polars handed the path maps the whole
    file -- on the website, where it has no threads, a large table makes it
    panic even for a prefix.
    """
    path = Path(path)
    if data_type == "parquet":
        return pl.read_parquet(path, n_rows=n_rows)
    # One sniff for every read of the file -- schema, full load, sampling --
    # so a tab-separated table is never read as one column by the one reader
    # that forgot to ask.
    options = delimited_options(path)
    if n_rows is not None:
        # At least the rows polars infers its dtypes from, so they are the
        # rows a read of the path would have inferred them from.
        head = csv_head(path, max(int(n_rows), _INFER_ROWS))
        if head is not None:
            return pl.read_csv(io.BytesIO(head), n_rows=n_rows, **options)
    return pl.read_csv(path, n_rows=n_rows, **options)


#: The separators a delimited cell table is written with, in the order a tie
#: is broken: comma (the CSV everything writes), tab (`.tsv`, and the `.txt`
#: that spreadsheet programs and older quantification tools export), semicolon
#: (Excel in a locale whose decimal mark is a comma).
_SEPARATORS = (",", "\t", ";")

#: How much of a file the header line is looked for in.
_SNIFF_BYTES = 1 << 20


def _header_line(path) -> str:
    """The first line of the file, BOM and line end removed."""
    with open(path, "rb") as handle:
        head = handle.read(_SNIFF_BYTES)
    line = head.split(b"\n", 1)[0].rstrip(b"\r")
    return line.decode("utf-8-sig", errors="ignore")


def _count_outside_quotes(line: str, separator: str) -> int:
    count, quoted = 0, False
    for char in line:
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            count += 1
    return count


def sniff_separator(path) -> str:
    """The separator of the delimited table at `path`, from its header line.

    Read from the content, never the extension: a `.tsv` holding commas is a
    real file (and a test), and a `.txt` is either. The separator that splits
    the header most often wins; a header none of them splits is one column,
    and a comma reads that as well as anything.
    """
    try:
        line = _header_line(path)
    except OSError:
        return ","
    counts = {sep: _count_outside_quotes(line, sep) for sep in _SEPARATORS}
    best = max(_SEPARATORS, key=lambda sep: counts[sep])  # first wins a tie
    return best if counts[best] else ","


#: A number written with a decimal comma, and one written with a point.
_DECIMAL_COMMA = re.compile(r"^[-+]?\d+,\d+(?:[eE][-+]?\d+)?$")
_DECIMAL_POINT = re.compile(r"^[-+]?\d*\.\d+(?:[eE][-+]?\d+)?$")


def _uses_decimal_comma(path) -> bool:
    """Whether a semicolon table writes its numbers `1,5` -- what Excel does
    where the comma is the decimal mark, and the reason it chose semicolons.

    Only when the sample shows comma decimals and no point decimals: polars'
    `decimal_comma` reads `1.5` as text, so a semicolon file written with
    points must not be read that way."""
    head = csv_head(path, _INFER_ROWS)
    if not head:
        return False
    comma, point = _DECIMAL_COMMA, _DECIMAL_POINT
    saw_comma = False
    for line in head.decode("utf-8-sig", errors="ignore").splitlines()[1:]:
        for field in line.split(";"):
            field = field.strip().strip('"')
            if point.match(field):
                return False
            if comma.match(field):
                saw_comma = True
    return saw_comma


def delimited_options(path) -> dict:
    """The `pl.read_csv` keywords that read the delimited table at `path`.

    A UTF-8 BOM and CRLF line ends need nothing: polars reads both as is
    (tests/test_delimited_tables.py holds it to that).
    """
    separator = sniff_separator(path)
    options = {"separator": separator}
    if separator == ";" and _uses_decimal_comma(path):
        options["decimal_comma"] = True
    return options


#: The rows `pl.read_csv` infers a schema from (its `infer_schema_length`).
_INFER_ROWS = 100

#: How far into a CSV `csv_head` looks for its records.
_HEAD_LIMIT = 64 << 20


def csv_head(path, records: int) -> bytes | None:
    """The header and the first `records` records of the CSV at `path` (a
    record ends at a line break outside quotes), as bytes; the whole file if
    it is shorter; None when they are not in its first 64 MB."""
    found = 0
    quoted = False
    data = bytearray()
    with open(path, "rb") as handle:
        while len(data) < _HEAD_LIMIT:
            chunk = handle.read(1 << 20)
            if not chunk:
                return bytes(data)
            offset = len(data)
            data += chunk
            if not quoted and b'"' not in chunk:
                at = chunk.find(b"\n")
                while at >= 0:
                    found += 1
                    if found > records:
                        return bytes(data[:offset + at + 1])
                    at = chunk.find(b"\n", at + 1)
                continue
            for at, byte in enumerate(chunk):
                if byte == 0x22:
                    quoted = not quoted
                elif byte == 0x0A and not quoted:
                    found += 1
                    if found > records:
                        return bytes(data[:offset + at + 1])
    return None


def write_flat_table(frame: pl.DataFrame, path, data_type="csv") -> None:
    """`frame` over the file at `path`, in the format `data_type` names.

    The counterpart of the read, and it exists for one caller: the ROI
    plugin writes its region columns back into the user's own table. Writing a
    parquet back as a CSV would be a silent format change to somebody's data.
    """
    path = Path(path)
    if data_type == "parquet":
        frame.write_parquet(path)
    else:
        # Back in the file's own separator (and decimal mark): a user's `.tsv`
        # written back as commas would be a silent format change too.
        options = delimited_options(path) if path.is_file() else {}
        frame.write_csv(path, separator=options.get("separator", ","),
                        decimal_comma=bool(options.get("decimal_comma")))
