"""Where the marker/metadata split is decided, and who is allowed to decide it.

A CSV header does not say which of its columns are stains and which are
measurements taken about a cell. That is the one thing about a CSV that cannot
be worked out from the file, which is why the import step asks -- and once
asked, the answer is a fact about the dataset that every surface has to read
rather than re-derive.

Three things went wrong at once on that path, and each has its own section
below:

- Gating derived its own list from "every numeric column with a histogram",
  so a project whose columns had been carefully split still got a threshold
  slider for Area, Eccentricity and the slide label.
- The import screen asked for a cell-type column, which nothing in core reads.
- A CSV was never asked whether its values were raw counts, and the adapter
  ignored the flag even when something did set it -- see
  tests/test_requirements_routes.py for the asking half.
"""

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import centroid_tiles, data_model, database_model
from plexora.server.models.adapters.csv_adapter import CsvAdapter
from plexora.server.models.project import IMPORT_ROLES, ROLE_NAMES, Project
from plexora.server.routes import import_routes, page_routes, project_routes

from tests.helpers import csv_spec, project

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "dataset_markers_probe.mjs"


def _cells_csv(path):
    """A quantification table of the ordinary shape: identifiers, centroids,
    morphology, then the stains. Every one of those is numeric."""
    pl.DataFrame({
        "CellID": np.arange(6, dtype=np.uint32),
        "X_centroid": np.linspace(1, 6, 6),
        "Y_centroid": np.linspace(1, 6, 6),
        "Area": np.linspace(50, 300, 6),
        "Eccentricity": np.linspace(0, 1, 6),
        "CD3": np.linspace(10, 60, 6),
        "DAPI": np.linspace(20, 70, 6),
    }).write_csv(path)
    return path


# --------------------------------------------------------------------------
# What a plugin is told it may threshold
# --------------------------------------------------------------------------

def test_a_plugin_reads_the_recorded_split_rather_than_guessing_from_numbers():
    """Driven through the real getter in datasetContext.js -- see the probe."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    for line in (
        "the recorded split decides, not which columns happen to be numeric",
        "a recorded marker the loaded table no longer holds is dropped",
        "an unclassified project still gets a usable list",
        "a split with nothing describable in it falls back rather than emptying",
    ):
        assert line in proc.stdout, proc.stdout


def test_no_plugin_derives_its_own_marker_list():
    """The rule itself, stated once.

    Core answers "what can be thresholded" for every plugin, because the answer
    has to be the same one the user gave on the import screen. A plugin that
    walks the description looking for histograms is building a second, weaker
    answer -- which is exactly what gating did, and it silently disagreed with
    the project record on every CSV.
    """
    offenders = {}
    for source in (REPO_ROOT / "plexora" / "plugins").rglob("*.js"):
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"Object\.keys\(\s*this\.databaseDescription\s*\)", text):
            line = text[:match.start()].count("\n") + 1
            offenders.setdefault(source.name, []).append(line)
    assert not offenders, (
        f"these plugins build a marker list out of the column statistics: {offenders}. "
        "Read ctx.dataset.table.markers -- the split the project recorded -- so "
        "every plugin agrees with the import screen and with each other."
    )


# --------------------------------------------------------------------------
# What the import screen asks for
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    for module in (plexora, data_model, import_routes, database_model,
                   centroid_tiles, page_routes, project_routes):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", tmp_path)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", config_path)
    # A real image on disk: saving the screen re-reads the datasource, and the
    # read opens it. 256px because load_datasource walks down to the last
    # pyramid level with every dimension >= 200.
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    record = project(
        "proj",
        src=str(image),
        dataset=csv_spec(
            _cells_csv(tmp_path / "cells.csv"),
            celltype="Eccentricity",
            markers=["CD3", "DAPI"],
            metadata=["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"],
        ),
    )
    config_path.write_text(json.dumps({"proj": record.to_entry()}), encoding="utf-8")
    return plexora.app.test_client()


def _page_data(client):
    """The `data` blob the Confirm Columns template hands its script."""
    html = client.get("/project/proj/columns").get_data(as_text=True)
    return json.loads(re.search(r"const data = (\{.*?\});", html, re.S).group(1))


def test_the_import_screen_does_not_ask_for_a_cell_type_column():
    """Nothing in core reads it. A plugin that wants an annotation column
    declares it and is asked through the requirements modal at the moment it
    matters, which is the whole point of the requirement machinery -- so
    putting the question in front of every CSV import buys nothing and costs a
    select the user has to reason about."""
    assert "celltype" not in IMPORT_ROLES
    assert set(IMPORT_ROLES) <= set(ROLE_NAMES)


def test_the_screen_asks_about_every_role_that_decides_how_the_table_is_read(client):
    """The classifier draws one select per label it is handed, so this map is
    literally what the screen asks."""
    data = _page_data(client)

    assert set(data["roleLabels"]) == set(IMPORT_ROLES)
    assert "celltype" not in data["roleLabels"]


def test_an_unasked_role_survives_the_screen(client):
    """Not shown is not cleared. A cell-type column recorded at import (or set
    on the edit page, which does offer every role) has to still be there after
    the user confirms their columns."""
    response = client.post("/project/proj/columns", json={
        "markers": ["CD3", "DAPI"],
        "metadata": ["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"],
        "roles": {role: None for role in IMPORT_ROLES},
    })

    assert response.status_code == 200
    assert Project.load("proj").roles.celltype == "Eccentricity"


def test_confirming_the_screen_does_not_retire_a_question_it_never_asked(client):
    """A role marked confirmed is never asked again. Echoing back a predicted
    cell-type column the user was never shown would settle that question on
    their behalf."""
    client.post("/project/proj/columns", json={
        "markers": ["CD3", "DAPI"],
        "metadata": ["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"],
        "roles": {"cell_id": "CellID", "celltype": "Eccentricity"},
    })

    assert "role:celltype" not in Project.load("proj").confirmed


# --------------------------------------------------------------------------
# What the adapter reads the split for
# --------------------------------------------------------------------------

def test_the_adapter_reports_the_recorded_markers(tmp_path):
    """Server-side half of the same answer. This used to be
    everything-but-the-roles, so Area and Eccentricity were features here too.
    """
    spec = csv_spec(
        _cells_csv(tmp_path / "cells.csv"),
        markers=["CD3", "DAPI"],
        metadata=["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"],
    )

    normalized = CsvAdapter(spec).load_table()

    assert normalized.feature_columns == ["CD3", "DAPI"]


def test_the_adapter_falls_back_for_a_project_that_was_never_classified(tmp_path):
    """A project registered before the classification screen ran has no answer
    to prefer, and reporting no features at all would be worse than the guess.
    """
    spec = csv_spec(_cells_csv(tmp_path / "cells.csv"))

    normalized = CsvAdapter(spec).load_table()

    assert set(normalized.feature_columns) == {"Area", "Eccentricity", "CD3", "DAPI"}


def test_the_log_transform_reaches_a_csv(tmp_path):
    """`is_transformed` was honoured by the AnnData adapter and read straight
    past by this one, so a CSV project could report itself log-transformed
    while every number in it was still a raw count."""
    path = _cells_csv(tmp_path / "cells.csv")
    spec = csv_spec(path, markers=["CD3", "DAPI"],
                    metadata=["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"])
    raw = CsvAdapter(spec).load_table().table

    transformed = CsvAdapter(replace(spec, is_transformed=True)).load_table().table

    assert transformed["CD3"].to_list() == pytest.approx(np.log1p(raw["CD3"].to_numpy()))
    # Markers only: a log-transformed centroid moves the cell on the image, and
    # a cell id is not a measurement at all.
    for untouched in ("CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"):
        assert transformed[untouched].to_list() == raw[untouched].to_list()
