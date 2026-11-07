"""A picked CSV parsed by the website's page reads as polars reads it.

The page's parser (client src/js/node/csv.js) runs in node over CSVs written
here (tests/js/csv_columns_probe.mjs: whole, in pieces cut anywhere, and as
merged parts), and plexora.browser.columns builds the frame from its output
with the schema polars infers from the head. Every case must equal
`pl.read_csv` of the same file -- dtypes, nulls, NaN and infinities, text with
quotes, commas, line breaks and accents, CRLF, a missing last line break.
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from plexora.browser import columns

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "csv_columns_probe.mjs"

CASES = {
    "numbers": (
        "CellID,X_centroid,Y_centroid,CD3,CD8\n"
        "1,10.5,20.25,0.5,1e-3\n"
        "2,-3.0,4,.5,5.\n"
        "3,1e3,2E-2,123456789.123,-0\n"
        "40000000000,0,0,nan,inf\n"
        "5,7,8,-inf,NaN\n"
    ),
    "empties": (
        "id_col,x,label\n"
        "1,,a\n"
        ",2.5,\n"
        "3,3.5,c\n"
    ),
    "text": (
        "CellID,phenotype,note\n"
        '1,"CD8+, PD1+","said ""hi"""\n'
        "2,Trég µ,plain\n"
        '3,"two\nlines",""\n'
        "4,B cell,\n"
    ),
    "crlf": "a,b,c\r\n1,2.5,x\r\n2,3.5,y\r\n3,4.5,z",
    "booleans": "flag,v\ntrue,1\nfalse,2\ntrue,3\n",
    "blank_lines": "a,b\n1,2\n\n3,4\n",
}


def _write(tmp_path, n_rows=3000):
    """The cases, and one large plain table (parsed in merged parts too)."""
    for name, text in CASES.items():
        (tmp_path / f"{name}.csv").write_bytes(text.encode("utf-8"))
    rng = np.random.default_rng(5)
    big = pl.DataFrame({
        "CellID": np.arange(1, n_rows + 1),
        "X_centroid": rng.random(n_rows) * 5000,
        "Y_centroid": rng.random(n_rows) * 5000,
        **{f"M{k}": rng.lognormal(3, 1, n_rows) for k in range(6)},
        "imageid": rng.choice(["slide_A", "slide_B", "slide_C"], n_rows),
    })
    big.write_csv(tmp_path / "large.csv")
    labels = ["CD8+, PD1+", 'the "B" cells', "tumour\nedge", "Trég", ""]
    quoted = big.head(800).with_columns(
        phenotype=pl.Series(rng.choice(labels, 800)).replace("", None))
    quoted.write_csv(tmp_path / "quoted_large.csv")


def _parsed(path: Path) -> dict:
    """The probe's output as `columns.frame` takes it."""
    raw = json.loads(path.read_text(encoding="utf-8"))
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


@pytest.fixture
def parsed_dir(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    _write(tmp_path)
    schemas = {}
    for csv in sorted(tmp_path.glob("*.csv")):
        schema = columns.schema_of(csv)
        assert schema is not None, csv.name
        schemas[csv.stem] = schema
        (tmp_path / f"{csv.stem}.kinds.json").write_text(json.dumps(columns.kinds(schema)))
    done = subprocess.run([node, str(PROBE), str(tmp_path)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "csv columns probe OK" in done.stdout
    return tmp_path, schemas


@pytest.mark.parametrize("name", [*CASES, "large", "quoted_large"])
def test_the_page_reads_a_csv_as_polars_does(parsed_dir, name):
    folder, schemas = parsed_dir
    built, notes = columns.frame(schemas[name], _parsed(folder / f"{name}.parsed.json"))
    assert notes == []
    assert_frame_equal(built, pl.read_csv(folder / f"{name}.csv"))


def test_a_value_polars_would_refuse_is_read_leniently(tmp_path):
    """Past the rows polars infers from, an integer column with a fraction or
    a number column with text would fail the whole table; the page reads the
    column as floats, or the field as missing, and says so."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    head = "".join(f"{i},{i}.5\n" for i in range(120))
    (tmp_path / "late.csv").write_text("n,v\n" + head + "7.25,oops\n", encoding="utf-8")
    schema = columns.schema_of(tmp_path / "late.csv")
    assert schema == [["n", "Int64"], ["v", "Float64"]]
    (tmp_path / "late.kinds.json").write_text(json.dumps(columns.kinds(schema)))
    done = subprocess.run([node, str(PROBE), str(tmp_path)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:]
    built, notes = columns.frame(schema, _parsed(tmp_path / "late.parsed.json"))
    assert built.schema == {"n": pl.Float64, "v": pl.Float64}
    assert built["n"][-1] == 7.25 and built["v"][-1] is None
    assert any(note.startswith("n:") for note in notes) and any(note.startswith("v:") for note in notes)


def test_a_column_the_page_cannot_type_leaves_the_file_to_polars(tmp_path):
    (tmp_path / "dates.csv").write_text("d,v\n2024-01-02,1\n", encoding="utf-8")
    import polars

    head = polars.read_csv(tmp_path / "dates.csv", try_parse_dates=True)
    assert columns.kinds([[n, str(t)] for n, t in head.schema.items()]) is None


@pytest.mark.parametrize("n_rows", [1, 2, 150, 200, 5000])
def test_a_prefix_read_is_polars_own_prefix(tmp_path, n_rows):
    """read_flat_table(n_rows=) parses the first records' bytes alone (polars
    handed the path maps the whole file; on the website a large one makes it
    panic): the same rows and dtypes as polars' own prefix read."""
    from plexora.server.models.adapters.flat_table import read_flat_table

    _write(tmp_path)
    for csv in sorted(tmp_path.glob("*.csv")):
        assert_frame_equal(read_flat_table(csv, "csv", n_rows=n_rows), pl.read_csv(csv, n_rows=n_rows))
