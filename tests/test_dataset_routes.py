"""Datasets over HTTP, and the project listing a file browser is drawn from.

The interesting half is what these routes must NOT do. Deleting a folder is not
deleting what is in it; assigning is one verb whatever gesture posted it; the
five keys `GET /projects` has always returned are still there byte for byte,
because Figure Builder's library renders its cards from the same list -- and an
import that cannot file its project still imports it.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora import paths
from plexora.server.models import datasets
from plexora.server.models.project import DataSpec, Project, write_config
from tests import helpers


@pytest.fixture
def client(tmp_path):
    write_config(paths.config_path(), {
        "sample1": helpers.entry("sample1"),
        "sample2": helpers.entry("sample2"),
        "sample3": helpers.entry("sample3"),
    })
    # The module-level app, as every other route test uses: the route modules
    # register on it by side-effect import at load, so a second create_app()
    # would hand back a Flask with no routes on it at all.
    return plexora.app.test_client()


def _create(client, name, **payload):
    response = client.post("/datasets", json={"name": name, **payload})
    assert response.status_code == 201, response.get_json()
    return response.get_json()["dataset"]


# --------------------------------------------------------------------------
# Making and naming folders
# --------------------------------------------------------------------------

def test_a_new_dataset_comes_back_with_its_id(client):
    dataset = _create(client, "Melanoma Cohort", projects=["sample1"])

    assert dataset["name"] == "Melanoma Cohort"
    assert dataset["projects"] == ["sample1"]
    assert dataset["projectCount"] == 1
    assert dataset["id"]


def test_the_listing_is_by_name(client):
    _create(client, "zebra")
    _create(client, "Alpha")

    listed = client.get("/datasets").get_json()["datasets"]

    assert [d["name"] for d in listed] == ["Alpha", "zebra"]


def test_a_name_already_taken_is_a_conflict_not_a_bad_request(client):
    """Different codes because the client does different things: 409 means
    "try another name", 400 means "that was not a name"."""
    _create(client, "Cohort")

    assert client.post("/datasets", json={"name": "cohort"}).status_code == 409
    assert client.post("/datasets", json={"name": "  "}).status_code == 400


def test_a_dataset_cannot_be_made_around_a_project_that_is_not_there(client):
    response = client.post("/datasets", json={"name": "Cohort",
                                              "projects": ["ghost"]})

    assert response.status_code == 400
    assert "ghost" in response.get_json()["error"]


def test_renaming_keeps_the_id_and_the_members(client):
    dataset = _create(client, "Cohort", projects=["sample1"])

    renamed = client.post(f"/datasets/{dataset['id']}",
                          json={"name": "Melanoma"}).get_json()["dataset"]

    assert renamed["id"] == dataset["id"]
    assert renamed["name"] == "Melanoma"
    assert renamed["projects"] == ["sample1"]


def test_renaming_onto_a_taken_name_is_a_conflict(client):
    first = _create(client, "One")
    _create(client, "Two")

    assert client.post(f"/datasets/{first['id']}",
                       json={"name": "two"}).status_code == 409


def test_an_unknown_dataset_is_a_404_on_every_verb(client):
    assert client.post("/datasets/nosuch", json={"name": "x"}).status_code == 404
    assert client.post("/datasets/nosuch/delete").status_code == 404
    assert client.post("/projects/assign",
                       json={"projects": ["sample1"],
                             "dataset": "nosuch"}).status_code == 404


# --------------------------------------------------------------------------
# Deleting a folder is not deleting what is in it
# --------------------------------------------------------------------------

def test_deleting_a_dataset_releases_its_projects_and_keeps_them(client):
    dataset = _create(client, "Cohort", projects=["sample1", "sample2"])

    result = client.post(f"/datasets/{dataset['id']}/delete").get_json()

    assert result["released"] == ["sample1", "sample2"]
    assert client.get("/datasets").get_json()["datasets"] == []
    assert Project.find("sample1") is not None
    assert Project.find("sample2") is not None
    assert [p["dataset"] for p in client.get("/projects").get_json()] == [None] * 3


def test_deleting_a_project_takes_it_out_of_its_dataset(client):
    dataset = _create(client, "Cohort", projects=["sample1", "sample2"])

    assert client.post("/project/sample1/delete").status_code == 200

    remaining = client.get("/datasets").get_json()["datasets"][0]
    assert remaining["id"] == dataset["id"]
    assert remaining["projects"] == ["sample2"]


# --------------------------------------------------------------------------
# One verb for every gesture that moves a project
# --------------------------------------------------------------------------

def test_assigning_moves_several_projects_at_once(client):
    """Dropping a multi-selection on a folder is one request, not one per
    card: half a move that failed in the middle is the state nothing on the
    page can describe."""
    dataset = _create(client, "Cohort")

    result = client.post("/projects/assign",
                         json={"projects": ["sample1", "sample3"],
                               "dataset": dataset["id"]}).get_json()

    assert result["dataset"]["projects"] == ["sample1", "sample3"]


def test_assigning_releases_whatever_held_them(client):
    first = _create(client, "First", projects=["sample1", "sample2"])
    second = _create(client, "Second")

    client.post("/projects/assign",
                json={"projects": ["sample1"], "dataset": second["id"]})

    listed = {d["name"]: d["projects"] for d in
              client.get("/datasets").get_json()["datasets"]}
    assert listed == {"First": ["sample2"], "Second": ["sample1"]}
    assert first["id"] != second["id"]


def test_assigning_to_null_is_how_a_project_leaves_a_dataset(client):
    dataset = _create(client, "Cohort", projects=["sample1"])

    result = client.post("/projects/assign",
                         json={"projects": ["sample1"], "dataset": None}).get_json()

    assert result["dataset"] is None
    assert client.get("/datasets").get_json()["datasets"][0]["projects"] == []
    assert dataset["projects"] == ["sample1"]  # what it was before


def test_a_request_that_forgot_to_say_where_is_refused(client):
    """`null` means "nowhere" and is a real answer. An absent key is a caller
    that forgot, and treating the two alike would let a malformed request
    silently empty a folder."""
    assert client.post("/projects/assign",
                       json={"projects": ["sample1"]}).status_code == 400
    assert client.post("/projects/assign",
                       json={"projects": [], "dataset": None}).status_code == 400


# --------------------------------------------------------------------------
# What a card is drawn from
# --------------------------------------------------------------------------

def test_the_five_original_keys_are_unchanged(client):
    """Figure Builder's library renders its cards from this same list."""
    project = client.get("/projects").get_json()[0]

    for key in ("name", "createdAt", "lastOpenedAt", "thumbnailUrl", "shared"):
        assert key in project, key
    assert project["thumbnailUrl"] == "/project_thumbnail/sample1"
    assert project["shared"] is False


def test_a_project_carries_the_dataset_holding_it(client):
    dataset = _create(client, "Cohort", projects=["sample2"])

    listed = {p["name"]: p["dataset"] for p in client.get("/projects").get_json()}

    assert listed["sample2"] == {"id": dataset["id"], "name": "Cohort"}
    assert listed["sample1"] is None


def test_an_image_only_project_is_complete_and_says_so(client):
    project = client.get("/projects").get_json()[0]

    assert project["needsSetup"] is False
    assert project["table"] == "missing"
    assert project["segmentation"] == "missing"
    assert project["imageKind"] == "ome_tiff"


def test_a_project_whose_data_is_undecided_is_flagged(client):
    Project.mutate("sample1", lambda p: p.patch(dataset=DataSpec(
        type="spatialdata", src="/s.zarr", unresolved=("table",))))

    listed = {p["name"]: p for p in client.get("/projects").get_json()}

    assert listed["sample1"]["needsSetup"] is True
    assert listed["sample1"]["table"] == "unresolved"
    assert listed["sample1"]["unresolved"] == ["table"]
    assert listed["sample2"]["needsSetup"] is False


def test_a_dangling_member_is_hidden_from_the_listing(client, tmp_path):
    """A shared root that is not mounted this morning shows fewer projects; it
    must not show a folder claiming to hold one that cannot be opened."""
    dataset = _create(client, "Cohort", projects=["sample1", "sample2"])
    write_config(paths.config_path(), {"sample1": helpers.entry("sample1")})

    listed = client.get("/datasets").get_json()["datasets"][0]

    assert listed["projects"] == ["sample1"]
    # Still on disk, though -- the whole point of pruning in the view.
    assert datasets.load_all()[dataset["id"]].projects == ("sample1", "sample2")


# --------------------------------------------------------------------------
# Filing a project as it is imported
# --------------------------------------------------------------------------

def _image(tmp_path, name="slide.ome.tif"):
    """256px, not smaller: load_datasource walks down to the last pyramid level
    with every dimension >= 200, and a smaller image makes that walk raise."""
    path = tmp_path / name
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _import(client, image, dataset=None, dataset_new=None):
    """One image, through the one import route.

    `dataset` is an id and `dataset_new` a name to make, which is the same
    either/or the form's two fields were -- resolved by `_dataset_request`,
    which the new route reuses unchanged, which is the point of these tests.
    """
    body = {"paths": [str(image)], "name": "imported"}
    if dataset:
        body["dataset"] = {"id": dataset}
    elif dataset_new:
        body["dataset"] = {"new": dataset_new}
    return client.post("/import/sample", json=body)


def test_an_import_joins_the_dataset_it_names(client, tmp_path):
    dataset = _create(client, "Melanoma Cohort")

    response = _import(client, _image(tmp_path), dataset=dataset["id"])

    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    assert datasets.find(dataset["id"]).projects == ("imported",)


def test_an_import_can_make_the_dataset_it_names(client, tmp_path):
    """The first slide of a cohort is the one most likely to be filed wrong,
    and before this the only way to file it was to import it and then drag the
    card. The folder is made here rather than on the click that chose the name,
    so abandoning the dialog leaves nothing behind."""
    response = _import(client, _image(tmp_path), dataset_new="Pilot Batch")

    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    made = datasets.find_by_name("Pilot Batch")
    assert made is not None and made.projects == ("imported",)


def test_a_new_name_that_is_taken_joins_that_dataset(client, tmp_path):
    """Not an error. The picker offers "New dataset…" to somebody who could not
    find the folder they wanted, and two people typing the same cohort name
    mean the same folder -- so the import must not fail on the collision, and
    must not end up with two folders of one name either."""
    existing = _create(client, "Melanoma Cohort", projects=["sample1"])

    _import(client, _image(tmp_path), dataset_new="Melanoma Cohort")

    assert len(datasets.load_all()) == 1
    assert datasets.find(existing["id"]).projects == ("sample1", "imported")


def test_an_import_naming_a_deleted_dataset_is_refused_before_it_registers(
        client, tmp_path):
    """Refused before a byte is written. The project is filed AFTER it exists,
    so reporting this afterwards would leave the user with the project they
    asked for, filed nowhere, behind an error page."""
    response = _import(client, _image(tmp_path), dataset="gone000")

    assert response.status_code == 400
    assert "no longer exists" in response.get_data(as_text=True)
    assert "imported" not in plexora.get_config_names()


def test_an_import_that_names_no_dataset_is_filed_nowhere(client, tmp_path):
    _create(client, "Melanoma Cohort")

    assert _import(client, _image(tmp_path)).status_code == 200
    assert datasets.membership().get("imported") is None
