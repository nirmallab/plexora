"""A picked CSV's columns, parsed by the page, as the frame polars would read.

On the website a table the user picked is parsed by the page's own workers
(client src/js/node/csv.js): polars in WebAssembly has no threads, and a large
table does not parse at all. What polars would have made of it is fixed here
before the page reads a byte past the head:

- `schema_of(path)` is polars' own reading of the head -- the first 100 rows,
  which is all `pl.read_csv` infers its dtypes from -- as `[[name, dtype],
  ...]`, and `kinds(schema)` what the page parses each column as ("int",
  "num", "str");
- `frame(schema, parsed)` builds the frame from what the page hands back,
  each column cast to the dtype polars chose. A column polars would have
  refused further down (an integer column with a fraction, text in a number
  column) is read as floats and as missing values instead of failing the
  whole table, and `frame` says which in `notes`.

Anything the page cannot match (a dtype other than integers, floats, text and
booleans) makes `schema_of` answer None, and the file is read by polars as
anywhere else.
"""

from __future__ import annotations

import numpy as np
import polars as pl

#: How many rows `pl.read_csv` infers its schema from (its default).
INFER_ROWS = 100


def schema_of(path) -> list | None:
    """`[[name, dtype], ...]` for the CSV at `path`, as `pl.read_csv` would
    type its columns (from the same first rows it infers from); None when a
    column is of a kind the page does not parse."""
    from plexora.server.models.adapters.flat_table import read_flat_table

    head = read_flat_table(path, "csv", n_rows=INFER_ROWS)
    schema = [[name, str(dtype)] for name, dtype in head.schema.items()]
    return schema if kinds(schema) is not None else None


def _kind(dtype: str) -> str | None:
    if dtype.startswith(("Int", "UInt")):
        return "int"
    if dtype.startswith("Float"):
        return "num"
    if dtype in ("String", "Utf8", "Boolean", "Null"):
        return "str"
    return None


def kinds(schema) -> list | None:
    """What the page parses each column of `schema` as; None if one cannot
    be parsed to match."""
    found = [_kind(dtype) for _, dtype in schema]
    return None if None in found else found


def frame(schema, parsed: dict) -> tuple[pl.DataFrame, list]:
    """`(frame, notes)` from the page's columns: `parsed` is `{rows, columns:
    [{kind: "num", values, missing, bad, fractions} | {kind: "str", codes,
    dictionary}]}` in `schema`'s order, arrays as numpy (float64 values, uint8
    missing flags or None, int32 codes with -1 for missing)."""
    rows = int(parsed["rows"])
    series = []
    notes = []
    for (name, dtype), column in zip(schema, parsed["columns"]):
        if column["kind"] == "num":
            values = np.asarray(column["values"], dtype=np.float64)[:rows]
            out = pl.Series(name, values, dtype=pl.Float64)
            missing = column.get("missing")
            if missing is not None and np.any(missing):
                out = out.scatter(np.flatnonzero(np.asarray(missing)[:rows]), None)
            if int(column.get("bad") or 0):
                notes.append(f"{name}: {column['bad']} values that are not numbers, read as missing")
            if _kind(dtype) == "int":
                if int(column.get("fractions") or 0):
                    notes.append(f"{name}: integers with fractions, read as floats")
                else:
                    out = out.cast(getattr(pl, dtype))
            elif dtype != "Float64":
                out = out.cast(getattr(pl, dtype))
        else:
            dictionary = list(column["dictionary"])
            codes = np.asarray(column["codes"], dtype=np.int64)[:rows]
            # Missing (-1) points at a null past the dictionary's end.
            index = np.where(codes < 0, len(dictionary), codes)
            out = pl.Series(name, [*dictionary, None], dtype=pl.String).gather(index)
            if dtype == "Boolean":
                out = out.str.to_lowercase().replace_strict(
                    {"true": True, "false": False}, default=None, return_dtype=pl.Boolean)
        series.append(out)
    return pl.DataFrame(series), notes


def from_page(spec, ask) -> pl.DataFrame | None:
    """The frame of a picked CSV, its columns parsed by the page: `ask(path,
    kinds)` hands the page what to parse and returns its columns as `frame`
    takes them, or None. None here -- not a CSV, a column the page cannot
    type, the page without the file -- and the caller reads the file."""
    import logging

    if getattr(spec, "type", None) != "csv" or not getattr(spec, "src", None):
        return None
    schema = schema_of(spec.src)
    if schema is None:
        return None
    parsed = ask(str(spec.src), kinds(schema))
    if parsed is None:
        return None
    built, notes = frame(schema, parsed)
    log = logging.getLogger(__name__)
    log.info("%s: %d rows parsed by the page (%s parts, %s ms)", spec.src, built.height,
             parsed.get("parts"), parsed.get("ms"))
    for note in notes:
        log.warning("%s: %s", spec.src, note)
    return built
