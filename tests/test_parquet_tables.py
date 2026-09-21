"""A cell table that came as a parquet.

Every spatial platform shipping today writes its per-cell table as parquet --
Xenium's `cells.parquet` is the one people hit first -- and Plexora read a
`.csv` and an `.h5ad` and nothing else. Picking a Xenium run therefore got as
far as proposing the whole sample, wrote the image, and then refused the table
with "Cannot read cells.parquet: expected a .csv, .h5ad or .zarr file."

A parquet cell table is a FLAT table: the columns on disk are the columns of
the table, the marker/metadata line is not drawn by the file, and there is
nothing inside to choose between. That is exactly what a CSV is, so it is read
by the same adapter and answers the same questions -- and the thing these tests
mostly guard is that "flat" is asked as a question (`is_flat_table`) rather
than spelled `== "csv"` in each of the dozen places that branch on it. Missing
one of those does not raise: it offers the AnnData read-spec controls for a
file that has no obsm, or goes looking for `adata.layers` in a parquet.

What is NOT here: parquet as a points or shapes layer. `transcripts.parquet`
and `cell_boundaries.parquet` were already read, by columns, in
`import_proposal._detect_parquet` -- see tests/test_import_proposal.py. This is
the table under them.
"""

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from dataclasses import replace

import plexora
from plexora.server.models.adapters import (
    CsvAdapter,
    SUPPORTED_DATA_DESCRIPTION,
    detect_data_type,
    get_adapter,
    is_flat_table,
)
from plexora.server.models.project import Project

from .helpers import csv_spec

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("pyarrow")


#: The columns a Xenium run's `cells.parquet` actually carries, in order.
#: Named here rather than reduced to three, because the marker/metadata split
#: is one of the things being asserted and it is only interesting when there
#: is something on both sides of it.
def _cells_frame(rows=64, seed=0):
    rng = np.random.default_rng(seed)
    return pl.DataFrame({
        "cell_id": [f"aaaabbbb-{index}" for index in range(rows)],
        "x_centroid": rng.uniform(0, 50, rows),
        "y_centroid": rng.uniform(0, 50, rows),
        "transcript_counts": rng.integers(0, 500, rows),
        "control_probe_counts": rng.integers(0, 5, rows),
        "total_counts": rng.integers(0, 500, rows),
        "cell_area": rng.uniform(10, 200, rows),
        "nucleus_area": rng.uniform(5, 100, rows),
    })


def _xenium_run(root, *, rows=64):
    """A Xenium run with the four files the importer reads off one.

    Written under `downloads/` rather than at the top of tmp_path, which IS the
    data root: a run folder whose name matches the project's would land on top
    of that project's own state directory, and the copy-into-the-project step
    would silently become a no-op. Where 10x actually leaves a run.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiment.xenium").write_text(
        json.dumps({"run_name": "run_0042", "pixel_size": 0.2125}),
        encoding="utf-8")
    rng = np.random.default_rng(1)
    tifffile.imwrite(root / "morphology.ome.tif",
                     rng.integers(0, 3000, (2, 128, 128)).astype(np.uint16))
    _cells_frame(rows).write_parquet(root / "cells.parquet")
    pl.DataFrame({
        "feature_name": rng.choice(["CD3", "CD8", "EPCAM"], 200),
        "x_location": rng.uniform(0, 50, 200),
        "y_location": rng.uniform(0, 50, 200),
    }).write_parquet(root / "transcripts.parquet")
    return root


# -- the format is recognised ------------------------------------------------

def test_a_parquet_is_its_own_data_type(tmp_path):
    path = tmp_path / "cells.parquet"
    _cells_frame().write_parquet(path)
    assert detect_data_type(path) == "parquet"


def test_the_refusal_names_parquet_among_what_is_accepted(tmp_path):
    """The sentence the user saw. It is built from the suffix table, so this
    fails if parquet is ever dropped from one and not the other."""
    path = tmp_path / "notes.docx"
    path.write_bytes(b"")
    with pytest.raises(ValueError) as excinfo:
        detect_data_type(path)
    assert ".parquet" in SUPPORTED_DATA_DESCRIPTION
    assert SUPPORTED_DATA_DESCRIPTION in str(excinfo.value)


def test_flat_is_a_question_rather_than_a_comparison_with_csv():
    """The seam this change turns on.

    A dozen places branch on "is this a CSV?" when what they mean is "is the
    file the table?" -- whether to copy it into the project, whether to offer
    the column classifier or the read-spec controls, whether `adata.layers` is
    a thing to go looking for. Spelled `== "csv"`, adding parquet means finding
    all dozen, and the one that is missed gives a wrong answer silently rather
    than an error.
    """
    assert is_flat_table("csv") and is_flat_table("parquet")
    assert not is_flat_table("anndata")
    assert not is_flat_table("spatialdata")
    # A project with no table at all, which every caller passes through here.
    assert not is_flat_table(None)


def test_one_adapter_reads_both_encodings(tmp_path):
    """Not two classes: everything after the read -- the positional id, the
    -inf guard, the marker split, the log transform -- is one implementation,
    and a second class would be a second place for those to drift."""
    assert get_adapter("parquet") is get_adapter("csv") is CsvAdapter


def test_a_parquet_and_the_csv_of_it_normalize_identically(tmp_path):
    """The claim that makes one adapter honest. Same frame, two encodings, and
    the loaded table has to be indistinguishable -- otherwise a project's
    numbers would depend on which format its pipeline happened to write."""
    frame = _cells_frame()
    csv_file = tmp_path / "cells.csv"
    parquet_file = tmp_path / "cells.parquet"
    frame.write_csv(csv_file)
    frame.write_parquet(parquet_file)

    base = csv_spec(csv_file, cell_id="cell_id", x="x_centroid", y="y_centroid",
                    markers=["transcript_counts", "total_counts"])
    from_csv = CsvAdapter(base).load_table()
    from_parquet = CsvAdapter(
        replace(base, type="parquet", src=str(parquet_file))).load_table()

    assert from_parquet.table.columns == from_csv.table.columns
    assert from_parquet.feature_columns == from_csv.feature_columns
    assert from_parquet.x_column == from_csv.x_column
    assert from_parquet.table.height == from_csv.table.height
    # The values, not just the shape -- a CSV round-trips floats through text
    # and a parquet does not, so this is the one comparison worth a tolerance.
    for column in ("x_centroid", "cell_area"):
        assert np.allclose(from_parquet.table[column].to_numpy(),
                           from_csv.table[column].to_numpy())


# -- and a Xenium run therefore imports --------------------------------------

@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def test_a_xenium_run_imports_with_its_cell_table(client, tmp_path):
    """The bug report, end to end. The run's morphology, its transcripts and
    its cells, from one pick of one folder."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")

    response = client.post("/import/sample", json={"paths": [str(run)]})

    assert response.status_code == 200, response.data
    project = Project.load(response.get_json()["name"])
    assert project.dataset is not None, "the cell table was dropped"
    assert project.dataset.type == "parquet"
    assert [layer.id for layer in project.spatial_layers] == ["transcripts"]


def test_the_vendor_column_names_resolve_to_roles(client, tmp_path):
    """`x_centroid`/`y_centroid` are what 10x writes, and the same predictor
    that reads a CSV header reads these -- so the import asks nothing and the
    cells are placeable the moment the viewer opens.

    The identifier is the one column that does NOT survive as written. A
    Xenium `cell_id` is a string (`aaaacidg-1`) and a centroid record packs
    the id as an integer, so the importer adds a numeric counterpart beside
    it and the role names that -- see server/utils/xenium_cells.py.
    """
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    roles = Project.load("run_0042").dataset.roles
    assert (roles.cell_id, roles.x, roles.y) == ("cell_index", "x_centroid",
                                                 "y_centroid")


def test_the_string_cell_id_gains_a_numeric_counterpart(client, tmp_path):
    """The whole Cells layer turned on this. `centroid_tiles` casts the id
    column to float to pack it into a fixed-width record, so a string id made
    every row invalid: the cache built, reported ready, and drew nothing."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    project = Project.load("run_0042")
    table = pl.read_parquet(project.dataset.src)

    assert table["cell_index"].dtype.is_numeric()
    assert table["cell_index"].to_list() == list(range(1, table.height + 1))
    # The vendor's own code is kept: it is what a collaborator quotes.
    assert table["cell_id"][0] == "aaaabbbb-0"


def test_the_copied_table_is_in_reference_pixels(client, tmp_path):
    """Everything in a Xenium run is in MICRONS, and a table gets no
    transform -- `CsvAdapter` reads x and y and they are reference pixels by
    definition of being in the table. Left alone, every centroid landed at a
    fifth of its true distance from the origin, bunched into the top-left
    corner of the slide."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    original = pl.read_parquet(run / "cells.parquet")
    client.post("/import/sample", json={"paths": [str(run)]})

    project = Project.load("run_0042")
    table = pl.read_parquet(project.dataset.src)

    assert table["x_centroid"].max() == pytest.approx(
        original["x_centroid"].max() / 0.2125)
    # The run's own file is untouched: it is the vendor's output and is not
    # ours to write to.
    assert pl.read_parquet(run / "cells.parquet")["x_centroid"].max() \
        == pytest.approx(original["x_centroid"].max())


def test_what_the_importer_changed_is_written_down(client, tmp_path):
    """A silent improvement to a file the user handed over is exactly the
    kind of thing that has to be recorded: without it, somebody comparing
    Plexora's `x_centroid` against the run's own finds two different numbers
    and nothing anywhere saying why."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    derived = dict(Project.load("run_0042").dataset.derived)

    assert derived["units"] == "reference_pixels"
    assert derived["pixel_size"] == pytest.approx(0.2125)
    assert derived["cell_index_from"] == "row_number"


def test_the_run_pixel_size_reaches_the_image(client, tmp_path):
    """Every layer's transform is built from this number, so a project whose
    ImageSpec did not carry it was one where the scale bar said "px" while
    every transform in the record was in microns."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    pixel_size = Project.load("run_0042").image.pixel_size

    assert pixel_size["value"] == pytest.approx(0.2125)
    assert pixel_size["source"] == "metadata"


def test_the_counts_are_markers_and_the_geometry_is_not(client, tmp_path):
    """The split is a guess the user can correct, but it has to start out
    roughly right: `cell_area` offered as a marker is a nonsense histogram in
    every plugin."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    columns = Project.load("run_0042").dataset.columns
    assert "transcript_counts" in columns.markers
    assert "cell_area" in columns.metadata
    assert "x_centroid" in columns.metadata


def test_the_registered_table_actually_loads(client, tmp_path):
    """Registration records a path and a type; this is the other half --
    `get_adapter` reads that record back and produces cells."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042", rows=32)
    client.post("/import/sample", json={"paths": [str(run)]})

    spec = Project.load("run_0042").dataset
    normalized = get_adapter(spec.type)(spec).load_table()

    assert normalized.table.height == 32
    assert normalized.x_column == "x_centroid"
    assert "transcript_counts" in normalized.feature_columns


def test_no_table_name_is_invented_for_a_file_that_is_the_table(client,
                                                                tmp_path):
    """`DataSpec.table` names a table INSIDE a container -- the one a
    SpatialData store's picker chooses. A parquet is the table, and the
    proposal used to put the string "parquet" in this field, where it read as
    the name of a table nothing would ever find."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    assert Project.load("run_0042").dataset.table is None


def test_the_table_is_copied_into_the_project(client, tmp_path):
    """The rule a CSV already followed, and it matters more here: the ROI
    plugin writes its region columns into this file, and writing them into
    10x's own output folder is not something to do behind the user."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    src = Path(Project.load("run_0042").dataset.src)
    assert src.name == "cells.parquet"
    assert run not in src.parents, "the vendor's own folder is being written to"
    assert src.is_file()


# -- the surfaces that ask "is this flat?" -----------------------------------

def test_inspecting_a_parquet_asks_no_container_questions(client, tmp_path):
    """The Data field's live inspection. A flat table has one table and its
    columns are confirmed on the classification screen, so there is nothing to
    pick -- the same answer a CSV gets."""
    path = tmp_path / "cells.parquet"
    _cells_frame().write_parquet(path)

    body = client.post("/inspect_data", json={"path": str(path)}).get_json()

    assert body["ok"] is True
    assert body["data_type"] == "parquet"
    assert body["tables"] == [] and body["ambiguous"] == []


def test_the_edit_page_offers_the_column_classifier_not_the_read_spec(client,
                                                                      tmp_path):
    """The branch that would have been missed by a `== "csv"` left behind: a
    parquet's columns need classifying exactly as a CSV's do, and it has no
    obsm, no layers and no matrix to choose between."""
    from plexora.server.routes.project_routes import _describe

    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})

    described = _describe(Project.load("run_0042"))
    assert described["has"]["columns"] is True
    assert described["has"]["readSpec"] is False


def test_nothing_goes_looking_for_anndata_annotations_in_a_parquet(tmp_path):
    """`source_layers`/`source_obsm` open the file when a project recorded
    none. For a flat table there is nothing to open and no picker to fill, and
    the guard has to know that -- reached, it would open the parquet through
    anndata on every requirements fetch and swallow the failure."""
    from plexora.server.models.adapters.inspection import (source_layers,
                                                           source_obsm)

    path = tmp_path / "cells.parquet"
    _cells_frame().write_parquet(path)
    spec = replace(csv_spec(path, cell_id="cell_id", x="x_centroid",
                            y="y_centroid"), type="parquet")

    assert source_layers(spec) == []
    assert source_obsm(spec) == []


def test_the_browser_may_send_one(client, tmp_path):
    """A flat table is copied into the project anyway, so uploading one costs
    a copy that was always going to happen -- the argument that admitted CSV,
    and it does not change with the encoding. It is also the only way to name
    a local file from a session with no data node."""
    import io

    path = tmp_path / "cells.parquet"
    _cells_frame().write_parquet(path)

    response = client.post(
        "/upload_data_file",
        data={"file": (io.BytesIO(path.read_bytes()), "cells.parquet")},
        content_type="multipart/form-data")

    body = response.get_json()
    assert body["ok"] is True, body
    assert detect_data_type(body["path"]) == "parquet"


# -- a parquet that is not Xenium's --------------------------------------

def test_a_cell_table_from_any_pipeline_is_proposed_as_one(tmp_path):
    """The Xenium shape (`cell_id` + `x_centroid`/`y_centroid`) is one vendor's
    spelling. What makes a parquet a cell table is that a centroid can be read
    out of it -- anything else is a table nothing could place cells from."""
    from plexora.server.models.import_proposal import inspect_paths

    path = tmp_path / "quantification.parquet"
    pl.DataFrame({
        "CellID": np.arange(8),
        "X_centroid": np.linspace(0, 100, 8),
        "Y_centroid": np.linspace(0, 100, 8),
        "CD3": np.linspace(0, 1, 8),
    }).write_parquet(path)

    sample = inspect_paths([str(path)]).samples[0]
    table = next(layer for layer in sample.layers if layer.role == "table")
    assert table.kind == "table"
    assert table.table is None


def test_a_parquet_with_no_coordinates_is_still_refused_by_name(tmp_path):
    """Reported rather than raised, with the columns in the message: a file
    that is genuinely not a cell table must not be imported as an empty one,
    and the user needs to see what Plexora actually read."""
    from plexora.server.models.import_proposal import inspect_paths

    path = tmp_path / "gene_panel.parquet"
    pl.DataFrame({"gene": ["CD3", "CD8"], "codeword": [1, 2]}).write_parquet(path)

    proposal = inspect_paths([str(path)])
    assert proposal.samples == []
    reason = proposal.unrecognised[0]["reason"]
    assert "gene_panel.parquet" in reason and "gene" in reason


def test_register_datasource_takes_one_too(tmp_path):
    """The programmatic door -- `plexora.create_project(data=...)` and the CLI
    reach `register_datasource`, which read its schema with `pl.read_csv` and
    would have failed on the first byte."""
    from plexora.datasource import register_datasource

    image = tmp_path / "slide.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 64, 64), dtype=np.uint16))
    path = tmp_path / "cells.parquet"
    _cells_frame(rows=8).write_parquet(path)

    register_datasource(name="demo", image=image, features=path,
                        x="x_centroid", y="y_centroid")

    spec = Project.load("demo").dataset
    assert spec.type == "parquet"
    assert get_adapter(spec.type)(spec).load_table().table.height == 8
