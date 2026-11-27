"""`plexora.api`'s project and dataset records: read and edit without loading.

A plugin page manages a cohort from these -- what a project's table is, which
column is which, which dataset holds it -- so what is pinned is that an edit
here is the edit the core surfaces make (confirmed, checked, the viewers told)
and that a read never opens a datasource.
"""

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora import api
from plexora.server.models import data_model, viewer_sessions
from plexora.server.models.project import Project, write_config
from plexora import paths
from tests import helpers


def _image(tmp_path, name="slide.ome.tif"):
    path = tmp_path / name
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _csv(tmp_path, name="cells.csv", **extra):
    path = tmp_path / name
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": np.linspace(0, 3, 4),
        "CD8": np.linspace(0, 3, 4),
        "area": np.linspace(10, 40, 4),
        **extra,
    }).write_csv(path)
    return path


@pytest.fixture
def sample(tmp_path):
    return plexora.create_project(_image(tmp_path), data=_csv(tmp_path),
                                  cell_id="CellID", x="X_centroid", y="Y_centroid")


@pytest.fixture
def published(monkeypatch):
    events = []
    monkeypatch.setattr(viewer_sessions, "publish",
                        lambda project, plugin, kind, payload, origin=None:
                        events.append((project, plugin, kind, payload)) or 1)
    return events


# --------------------------------------------------------------------------
# project_info
# --------------------------------------------------------------------------

def test_project_info_reads_the_record_and_loads_nothing(sample, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("project_info loaded a datasource")

    monkeypatch.setattr(data_model, "load_datasource", refuse)
    info = api.project_info(sample)

    assert info["name"] == sample
    assert info["has_table"] is True
    assert info["type"] == "csv"
    assert info["src"].endswith("cells.csv")
    assert info["table"] is None
    assert set(info["roles"]) == {"cell_id", "x", "y", "image_id", "celltype", "z", "t"}
    assert info["roles"]["cell_id"] == "CellID"
    assert info["roles"]["x"] == "X_centroid"
    assert info["roles"]["celltype"] is None
    assert info["pixel_size"] is None
    assert info["image"]["src"]
    assert isinstance(info["image"]["channels"], list)
    assert info["mask"] is None
    assert info["log_transformed"] is False


def test_project_info_on_an_image_alone_has_no_table(tmp_path):
    name = plexora.create_project(_image(tmp_path))

    info = api.project_info(name)

    assert info["has_table"] is False
    assert info["type"] is info["src"] is info["table"] is None
    assert info["markers"] == [] and info["metadata_columns"] == []


def test_project_info_speaks_an_anndatas_own_vocabulary():
    """x/y are the obs columns the positions come from, not the adapter's
    synthesized X/Y, so the answer can be handed back to update_project."""
    spec = helpers.anndata_spec(
        "/tmp/cells.h5ad", obs_id_field="cell", row_number_ids=False,
        coordinates={"source": "obs", "x_column": "cx", "y_column": "cy"},
        markers=("CD3",), obs_columns=("cell", "cx", "cy", "region"))
    write_config(paths.config_path(), {"ad": helpers.entry("ad", dataset=spec)})

    info = api.project_info("ad")

    assert info["roles"]["cell_id"] == "cell"
    assert (info["roles"]["x"], info["roles"]["y"]) == ("cx", "cy")
    assert info["coordinates"]["source"] == "obs"
    assert info["metadata_columns"] == ["cell", "cx", "cy", "region"]
    assert info["markers"] == ["CD3"]


def test_project_info_states_a_typed_calibration_in_microns():
    write_config(paths.config_path(), {"cal": helpers.entry("cal")})
    project = Project.load("cal")
    from dataclasses import replace

    project.patch(image=replace(project.image, pixel_size={
        "value": 650, "unit": "nm", "source": "manual"})).save()

    assert api.project_info("cal")["pixel_size"] == pytest.approx(0.65)


def test_project_info_of_an_unknown_project_is_a_key_error():
    with pytest.raises(KeyError):
        api.project_info("ghost")


# --------------------------------------------------------------------------
# update_project
# --------------------------------------------------------------------------

def test_update_project_records_roles_and_tells_the_viewers(sample, published):
    info = api.update_project(sample, roles={"celltype": "area"},
                              markers=["CD3", "CD8"])

    assert info["roles"]["celltype"] == "area"
    assert info["roles"]["cell_id"] == "CellID"  # not named, so kept
    assert info["markers"] == ["CD3", "CD8"]
    # One side of the split implies the other, as configure_project has it.
    assert "CD3" not in info["metadata_columns"]
    project = Project.load(sample)
    assert "role:celltype" in project.confirmed and "markers" in project.confirmed
    assert published == [(sample, "core", "dataset.changed",
                          {"sections": ["file"], "by": "update_project"})]


def test_markers_alone_are_a_var_change(sample, published):
    api.update_project(sample, markers=["CD3"])

    assert published[-1][3]["sections"] == ["var"]
    assert Project.load(sample).columns.markers == ("CD3",)


def test_an_empty_role_clears_it(sample, published):
    api.update_project(sample, roles={"celltype": "area"})
    info = api.update_project(sample, roles={"celltype": ""})

    assert info["roles"]["celltype"] is None


def test_pixel_size_is_set_and_cleared(sample, published):
    assert api.update_project(sample, pixel_size=0.5)["pixel_size"] == 0.5
    assert Project.load(sample).image.pixel_size["source"] == "manual"
    assert api.update_project(sample, pixel_size=0)["pixel_size"] is None


def test_nothing_to_change_tells_nobody(sample, published):
    api.update_project(sample)

    assert published == []


def test_a_refused_edit_leaves_the_record_as_it_was(sample, published, monkeypatch):
    from plexora import datasource

    before = Project.load(sample)

    def fail(*args, **kwargs):
        raise ValueError("no")

    monkeypatch.setattr(datasource, "set_pixel_size", fail)
    with pytest.raises(ValueError):
        api.update_project(sample, roles={"celltype": "area"}, pixel_size=1.0)

    assert Project.load(sample) == before
    assert published == []


def test_a_new_table_is_attached(tmp_path, published):
    name = plexora.create_project(_image(tmp_path))

    info = api.update_project(name, table_src=str(_csv(tmp_path, "other.csv")),
                              roles={"cell_id": "CellID"})

    assert info["has_table"] is True
    assert info["src"].endswith("other.csv")
    assert info["roles"]["cell_id"] == "CellID"
    assert published[-1][3]["sections"] == ["file"]


def test_update_project_of_an_unknown_project_is_a_key_error():
    with pytest.raises(KeyError):
        api.update_project("ghost", markers=["CD3"])


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------

@pytest.fixture
def three():
    write_config(paths.config_path(), {name: helpers.entry(name)
                                       for name in ("s1", "s2", "s3")})


def test_a_dataset_round_trip(three):
    made = api.create_dataset("Cohort", ["s1", "s2"])

    assert made["projects"] == ["s1", "s2"]
    assert [d["id"] for d in api.list_datasets()] == [made["id"]]

    renamed = api.update_dataset(made["id"], name="Trial", projects=["s2", "s3"])
    assert renamed["name"] == "Trial"
    assert renamed["projects"] == ["s2", "s3"]

    assert api.delete_dataset(made["id"]) == {"id": made["id"], "released": ["s2", "s3"]}
    assert api.list_datasets() == []


def test_projects_set_on_one_dataset_leave_another(three):
    first = api.create_dataset("A", ["s1"])
    second = api.create_dataset("B", [])

    api.update_dataset(second["id"], projects=["s1"])

    by_id = {d["id"]: d for d in api.list_datasets()}
    assert by_id[first["id"]]["projects"] == []
    assert by_id[second["id"]]["projects"] == ["s1"]


def test_meta_merges_and_none_removes(three):
    made = api.create_dataset("Cohort")

    api.update_dataset(made["id"], meta={"palette": "set2", "markers": {"a": 1}})
    api.update_dataset(made["id"], meta={"phenotypes": ["T"]})
    after = api.update_dataset(made["id"], meta={"palette": None})

    assert after["meta"] == {"markers": {"a": 1}, "phenotypes": ["T"]}
    assert api.list_datasets()[0]["meta"] == after["meta"]


def test_dataset_refusals_are_value_errors(three):
    api.create_dataset("Cohort")

    with pytest.raises(ValueError):
        api.create_dataset("cohort")
    with pytest.raises(ValueError):
        api.create_dataset("Other", ["ghost"])
    with pytest.raises(ValueError):
        api.update_dataset("nope", name="x")
    with pytest.raises(ValueError):
        api.delete_dataset("nope")


def test_the_route_merges_meta(three):
    client = plexora.app.test_client()
    made = api.create_dataset("Cohort")

    client.post(f"/datasets/{made['id']}", json={"meta": {"a": 1, "b": 2}})
    reply = client.post(f"/datasets/{made['id']}", json={"meta": {"a": None}})

    assert reply.status_code == 200
    assert reply.get_json()["dataset"]["meta"] == {"b": 2}
    assert client.post(f"/datasets/{made['id']}", json={"meta": [1]}).status_code == 400


# --------------------------------------------------------------------------
# records_stamp
# --------------------------------------------------------------------------

def test_the_stamp_moves_with_projects_and_datasets(three):
    stamp = api.records_stamp()
    assert stamp == api.records_stamp()
    assert len(stamp) == 16

    made = api.create_dataset("Cohort")
    after_dataset = api.records_stamp()
    assert after_dataset != stamp

    api.update_dataset(made["id"], meta={"k": "a much longer value than before"})
    after_meta = api.records_stamp()
    assert after_meta != after_dataset

    write_config(paths.config_path(), {name: helpers.entry(name)
                                       for name in ("s1", "s2")})
    assert api.records_stamp() != after_meta
