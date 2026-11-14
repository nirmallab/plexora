"""Adding a Xenium run folder again to the sample it made.

A fresh import copies the run's `cells.parquet` into the project and rewrites
the COPY so Plexora can read it (`xenium_cells.normalise_cells_table`): a
numeric `cell_index` beside the vendor's string `cell_id`, and the centroids
scaled from microns into reference pixels. The copy therefore no longer has
the original's size or mtime -- and "+ Add Layer" (or dropping the folder on
the open sample) recognised an already-attached table only by those.

So the run's raw table was proposed again, attached without the run's pixel
size, copied over the normalised one and left unnormalised: the cell id became
the vendor's string and every centroid went back to microns.
"""

import polars as pl
import pytest

import plexora
from plexora.server.models.project import Project

from tests.test_parquet_tables import _xenium_run


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


@pytest.mark.pro
def test_the_runs_own_table_is_not_proposed_again(client, tmp_path):
    """The table this sample already has came from this folder: scoped
    inspection offers nothing to add for it."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    assert client.post("/import/sample",
                       json={"paths": [str(run)]}).status_code == 200

    scoped = client.post("/import/inspect", json={
        "paths": [str(run)], "sample": "run_0042"}).get_json()
    offered = [(layer["id"], layer["role"])
               for sample in scoped.get("samples") or ()
               for layer in sample["layers"]]

    assert not any(role == "table" for _, role in offered), offered


@pytest.mark.pro
def test_adding_the_run_again_keeps_the_normalised_cell_table(client, tmp_path):
    """The numeric cell id and the pixel-scaled centroids survive the run
    folder being added again to its own sample."""
    run = _xenium_run(tmp_path / "downloads" / "run_0042")
    client.post("/import/sample", json={"paths": [str(run)]})
    original_max = pl.read_parquet(run / "cells.parquet")["x_centroid"].max()

    response = client.post("/import/layers", json={
        "sample": "run_0042", "paths": [str(run)]})
    assert response.status_code == 200, response.data

    dataset = Project.load("run_0042").dataset
    table = pl.read_parquet(dataset.src)
    found = {"cell_id role": dataset.roles.cell_id,
             "has cell_index": "cell_index" in table.columns,
             "x_centroid max": table["x_centroid"].max()}

    assert found == {"cell_id role": "cell_index",
                     "has cell_index": True,
                     "x_centroid max": pytest.approx(original_max / 0.2125)}
