"""A table swap that is refused leaves the project's own table copy alone.

A flat table is copied into the project's state directory under its own file
name, and the project record names that copy. Swapping in a different file
that happens to share the name (every MCMICRO run calls it `cells.csv`) must
not overwrite the copy until the new table has been inspected and has loaded:
a swap that is refused leaves the record as it was, so the bytes it names must
be as they were too.
"""

from pathlib import Path

import numpy as np
import polars as pl

from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.test_project_edit_routes import _import, isolate  # noqa: F401


def _cells(folder, cd3):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": cd3,
    }).write_csv(path)
    return path


def test_an_unreadable_table_of_the_same_name_does_not_overwrite_the_projects_copy(
        isolate, tmp_path):  # noqa: F811
    """The new cells.csv is empty, so the swap is refused -- and the project's
    own copy of the old cells.csv must still hold the old bytes."""
    original = _cells(tmp_path / "A", np.linspace(0, 3, 4))
    before = _import(isolate, tmp_path, "swap_empty", data=original)
    copy = Path(before.dataset.src)
    kept = copy.read_bytes()
    assert kept, "the import should have copied the table in"

    empty = tmp_path / "B" / "cells.csv"
    empty.parent.mkdir()
    empty.write_bytes(b"")

    response = isolate.post("/swap_empty/requirements", json={"data": str(empty)})

    assert response.status_code != 200
    after = Project.load("swap_empty")
    assert after.dataset.src == before.dataset.src
    assert copy.read_bytes() == kept, (
        "the refused swap overwrote the table the project still names")
    data_model.load_datasource("swap_empty", reload=True)
    assert data_model.get_datasource_df()["CD3"].to_list() == [0.0, 1.0, 2.0, 3.0]


def test_a_same_named_table_that_will_not_load_is_put_back_with_the_old_values(
        isolate, tmp_path, monkeypatch):  # noqa: F811
    """The new cells.csv inspects, but the reload refuses it, so the old record
    is put back -- and that record must read the old table's values, not the
    refused file's."""
    original = _cells(tmp_path / "A", np.linspace(0, 3, 4))
    before = _import(isolate, tmp_path, "swap_refused", data=original)
    copy = Path(before.dataset.src)
    kept = copy.read_bytes()

    replacement = _cells(tmp_path / "B", np.array([100.0, 200.0, 300.0, 400.0]))
    real = data_model.load_datasource
    calls = []

    def refuse_first(name, *args, **kwargs):
        # The reload of the swapped-in table is the first call; the adapter
        # refuses it the way it refuses any file it cannot read.
        calls.append(name)
        if len(calls) == 1:
            raise ValueError("the adapter cannot read this table")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(data_model, "load_datasource", refuse_first)
    response = isolate.post("/swap_refused/requirements",
                            json={"data": str(replacement)})

    assert response.status_code == 400, response.get_data(as_text=True)
    after = Project.load("swap_refused")
    assert after.dataset.src == before.dataset.src
    assert copy.read_bytes() == kept, (
        "the restored record names a copy that now holds the refused table")
    monkeypatch.setattr(data_model, "load_datasource", real)
    real("swap_refused", reload=True)
    assert data_model.get_datasource_df()["CD3"].to_list() == [0.0, 1.0, 2.0, 3.0]
