"""A picked tab- or semicolon-separated table parsed by the website's page
reads as the desktop reads it.

A `.tsv`, a tab-separated `.txt` and Excel's semicolon CSV with decimal
commas are all typed "csv", so on the website `columns.from_page` hands them
to the page's parser (client src/js/node/csv.js). `read_flat_table` sniffs
their separator; whatever the page makes of the file must be that same
frame -- or `from_page` must answer None so polars reads the file itself.
The page is run in node through tests/js/csv_columns_probe.mjs, as
tests/test_browser_columns.py runs it.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from plexora.browser import columns
from plexora.server.models.adapters.flat_table import read_flat_table

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "csv_columns_probe.mjs"

TABLES = {
    "cells.tsv": (
        "CellID\tX_centroid\tY_centroid\tCD3\n"
        "1\t10.5\t20.25\t0.5\n"
        "2\t30.0\t40.5\t1.25\n"
        "3\t50.5\t60.75\t2.0\n"
    ),
    "cells_tab.txt": (
        "CellID\tX_centroid\tY_centroid\tCD3\n"
        "1\t10.5\t20.25\t0.5\n"
        "2\t30.0\t40.5\t1.25\n"
        "3\t50.5\t60.75\t2.0\n"
    ),
    "cells_excel.csv": (
        "CellID;X_centroid;Y_centroid;CD3\n"
        "1;10,5;20,25;0,5\n"
        "2;30,0;40,5;1,25\n"
        "3;50,5;60,75;2,0\n"
    ),
}


def _page_ask(node, work):
    """`ask(path, kinds)` as the website's page answers it: node/csv.js
    parses the file and its columns come back as `columns.frame` takes them."""

    def ask(src, kinds):
        src = Path(src)
        folder = work / src.stem
        folder.mkdir()
        # The probe parses every `<name>.csv` in a folder; the page is handed
        # the picked file whatever its suffix.
        (folder / "picked.csv").write_bytes(src.read_bytes())
        (folder / "picked.kinds.json").write_text(json.dumps(kinds))
        done = subprocess.run([node, str(PROBE), str(folder)], capture_output=True, text=True,
                              cwd=REPO_ROOT, timeout=120)
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        raw = json.loads((folder / "picked.parsed.json").read_text(encoding="utf-8"))
        special = {"NaN": np.nan, "Inf": np.inf, "-Inf": -np.inf}
        for column in raw["columns"]:
            if column["kind"] == "num":
                column["values"] = np.array([special.get(v, v) if isinstance(v, str) else v
                                             for v in column["values"]], dtype=np.float64)
                if column["missing"] is not None:
                    column["missing"] = np.array(column["missing"], dtype=np.uint8)
            else:
                column["codes"] = np.array(column["codes"], dtype=np.int32)
        return raw

    return ask


@pytest.mark.parametrize("name", list(TABLES))
def test_a_picked_table_not_split_by_commas_reads_as_the_desktop_reads_it(tmp_path, name):
    """The page splits fields on commas only. A tab- or semicolon-separated
    table handed to it comes back with every value missing; it must instead be
    the frame `read_flat_table` reads, or be left to polars (None)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    picked = tmp_path / "picked"
    picked.mkdir()
    path = picked / name
    path.write_bytes(TABLES[name].encode("utf-8"))
    work = tmp_path / "work"
    work.mkdir()

    expected = read_flat_table(path, "csv")
    assert expected.height == 3 and expected["CD3"].null_count() == 0

    built = columns.from_page(SimpleNamespace(type="csv", src=str(path)), _page_ask(node, work))

    if built is not None:
        assert_frame_equal(built, expected)
