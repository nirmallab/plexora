"""The website's Parquet stand-in (`boot.parquet`) answers a schema probe from
the file's footer and a one-row read from its first rows.

`boundary_mask.is_boundary_table` asks for a parquet's column names on every
mask attach and every load ("Schema only -- no row is read"), and a z-stack's
object table is opened with `read_parquet(path, n_rows=1)` for its schema. On
the website those go through pyarrow, inside a 4 GiB WebAssembly heap whose
failed allocation ends Pyodide for the tab, and a Xenium boundary table is
millions of vertex rows: reading all of it to learn its column names is the
cost these callers were written to avoid.
"""

import polars as pl
import pyarrow.parquet as pq

from plexora.browser import boot

ROWS = 20_000
GROUP = 1_000


def _vertex_table(path):
    """A boundary table in the Xenium layout, in many row groups."""
    frame = pl.DataFrame({
        "cell_id": [f"c{i // 8}" for i in range(ROWS)],
        "vertex_x": [float(i % 997) for i in range(ROWS)],
        "vertex_y": [float(i % 991) for i in range(ROWS)],
        "label_id": [i // 8 + 1 for i in range(ROWS)],
    })
    frame.write_parquet(path, row_group_size=GROUP)
    return frame


def _arrow_shim(monkeypatch):
    """`boot.parquet(native=False)` with every replaced name put back after the
    test, and a count of the rows pyarrow hands back from `read_table`."""
    for owner, name in ((pl.DataFrame, "write_parquet"), (pl, "read_parquet"),
                        (pl, "scan_parquet")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    read = []
    real = pq.read_table

    def counting(*args, **kwargs):
        table = real(*args, **kwargs)
        read.append(table.num_rows)
        return table

    monkeypatch.setattr(pq, "read_table", counting)
    assert boot.parquet(native=False) is True
    return read


def test_a_schema_probe_through_the_website_parquet_reads_no_rows(tmp_path, monkeypatch):
    """`scan_parquet(path).collect_schema()` names the columns without reading
    a row of the table, so `is_boundary_table` stays a footer read there too."""
    from plexora.server.utils import boundary_mask

    path = tmp_path / "cell_boundaries.parquet"
    _vertex_table(path)
    read = _arrow_shim(monkeypatch)

    names = pl.scan_parquet(path).collect_schema().names()
    assert names == ["cell_id", "vertex_x", "vertex_y", "label_id"]
    assert sum(read) == 0, f"a schema probe read {sum(read)} of {ROWS} rows"

    read.clear()
    assert boundary_mask.is_boundary_table(path) is True
    assert sum(read) == 0, f"is_boundary_table read {sum(read)} of {ROWS} rows"


def test_a_one_row_read_through_the_website_parquet_reads_only_the_first_rows(
        tmp_path, monkeypatch):
    """`read_parquet(path, n_rows=1)` -- the object table's schema read
    (volume/objects.py) -- returns the first row having read no more than the
    first row group, not every row of the file."""
    path = tmp_path / "objects.parquet"
    frame = _vertex_table(path)
    read = _arrow_shim(monkeypatch)

    head = pl.read_parquet(path, n_rows=1)
    assert head.equals(frame.head(1))
    assert sum(read) <= GROUP, f"a one-row read read {sum(read)} of {ROWS} rows"
