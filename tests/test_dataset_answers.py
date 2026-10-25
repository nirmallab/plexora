"""A dataset's samples share what was said about them once (`dataset_answers`).

A cohort's tables come out of one pipeline and its images off one panel, so
the first sample's confirmed table answers and channel names are borrowed by
every sample in the dataset that fits -- same columns, same channel count --
and a sample that differs is asked as before.
"""

from __future__ import annotations

import pytest

import plexora
from plexora.server.models.project import Project
from tests import dataset_fixtures as fx
from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def _cohort(tmp_path, names=("A", "B"), extra=None):
    specs = []
    for name in names:
        image = fx.image(tmp_path / "images" / f"{name}.ome.tiff")
        table = fx.csv(tmp_path / "tables" / f"{name}.csv",
                       extra=(extra or {}).get(name))
        specs.append({"image": str(image), "data": str(table), "name": name})
    return plexora.create_dataset("Cohort", projects=specs)


ANSWER = {
    "roles": {"cell_id": "CellID", "x": "X_centroid", "y": "Y_centroid"},
    "markers": ["CD3"], "metadata": ["CellID", "X_centroid", "Y_centroid", "Area"],
    "confirm": ["role:cell_id", "role:x", "role:y", "markers"],
}


def test_a_sample_with_the_same_columns_takes_the_first_ones_answers(client, tmp_path):
    _cohort(tmp_path)
    assert client.post("/A/requirements", json=ANSWER).status_code == 200
    before = Project.load("B")
    assert "role:cell_id" not in before.confirmed

    response = client.get("/B/requirements?keys=role:cell_id,role:x,role:y,markers")
    assert response.status_code == 200
    assert response.get_json()["missing"] == []
    after = Project.load("B")
    assert {"role:cell_id", "role:x", "role:y", "markers"} <= set(after.confirmed)
    assert after.roles.cell_id == "CellID" and after.roles.x == "X_centroid"
    assert list(after.dataset.columns.markers) == ["CD3"]


def test_a_sample_whose_table_differs_is_asked(client, tmp_path):
    _cohort(tmp_path, extra={"B": ["Phenotype"]})
    client.post("/A/requirements", json=ANSWER)
    client.get("/B/requirements")
    assert "role:cell_id" not in Project.load("B").confirmed


def test_a_sample_answered_by_hand_keeps_its_own_answers(client, tmp_path):
    _cohort(tmp_path)
    swapped = {**ANSWER, "roles": {"cell_id": "CellID", "x": "Y_centroid", "y": "X_centroid"}}
    client.post("/B/requirements", json=swapped)
    client.post("/A/requirements", json=ANSWER)
    client.get("/B/requirements")
    assert Project.load("B").roles.x == "Y_centroid"


def test_a_sample_outside_the_dataset_borrows_nothing(client, tmp_path):
    _cohort(tmp_path)
    image = fx.image(tmp_path / "images" / "C.ome.tiff")
    plexora.create_project(image=str(image), data=str(fx.csv(tmp_path / "tables" / "C.csv")),
                           name="C")
    client.post("/A/requirements", json=ANSWER)
    client.get("/C/requirements")
    assert "role:cell_id" not in Project.load("C").confirmed


def _names(name):
    return [c["name"] for c in plexora.get_config()[name]["imageData"] if c["name"] != "Area"]


def test_an_image_on_default_names_takes_its_datasets_channel_names(client, tmp_path):
    _cohort(tmp_path, names=("A", "B", "C"))
    from plexora.datasource import rename_channels

    rename_channels("A", ["DNA", "CD3", "CD8"])
    rename_channels("C", ["Hoechst", "Ki67", "CD45"])   # named by hand: left alone
    # What registration names them when nothing does: the file's name and an
    # index (the first one guessed from the table's markers).
    assert _names("B")[1:] == ["B.ome.tiff_1", "B.ome.tiff_2"], _names("B")

    assert client.get("/B").status_code == 200
    assert _names("B") == ["DNA", "CD3", "CD8"]
    assert _names("C") == ["Hoechst", "Ki67", "CD45"]


def test_a_different_channel_count_keeps_its_own_names(client, tmp_path):
    _cohort(tmp_path)
    image = fx.image(tmp_path / "images" / "D.ome.tiff", channels=2)
    plexora.create_project(image=str(image), name="D")
    from plexora.server.models import datasets as registry

    registry.assign(["D"], registry.find_by_name("Cohort").id)
    from plexora.datasource import rename_channels

    rename_channels("A", ["DNA", "CD3", "CD8"])
    before = _names("D")
    client.get("/D")
    assert _names("D") == before


def test_the_panel_says_whose_answers_it_used(client, tmp_path):
    _cohort(tmp_path)
    client.post("/A/requirements", json=ANSWER)
    body = client.get("/B/tools/gating/panel").get_json()
    assert body.get("answered_from") == "A"
