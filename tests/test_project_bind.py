"""Which project shows a table or an image, and making one when none does.

The bridge's first step whenever SCIMAP Pro wants its table shown: Plexora
answers with the project that READS that file (and that image of it), or
registers one from the image -- the table read where it lies, never copied.
And when a person adds an image to a workspace in Biocognia Intelligence:
the project that shows that image file, or a new one made from it.
"""

from __future__ import annotations

import pytest

from plexora.agent import AgentSession, registry
from plexora.server.models.project import Project
from tests.agent_fixtures import blob_scene, write_image_pyramid, write_mask_pyramid, write_table
from tests.scimappro_fixtures import make_anndata_project


@pytest.fixture(autouse=True)
def _core():
    registry.discover([])


def _invoke(name, arguments):
    return registry.invoke(AgentSession(), name, arguments)


def test_the_project_reading_a_table_is_found_by_file_and_image(tmp_path):
    made = make_anndata_project(tmp_path)
    answer = _invoke("find_project_for_table", {"table": made["table"], "image_id": "slide_A"})
    assert answer["ok"], answer
    assert answer["result"]["project"] == made["name"]
    assert answer["result"]["candidates"][0]["image_id"] == "slide_A"


def test_another_image_of_the_same_table_is_not_this_project(tmp_path):
    made = make_anndata_project(tmp_path, extra_images=("slide_B",))
    answer = _invoke("find_project_for_table", {"table": made["table"], "image_id": "slide_B"})
    assert answer["result"]["project"] is None
    assert [c["project"] for c in answer["result"]["candidates"]] == [made["name"]]


def test_a_path_spelled_differently_is_the_same_file(tmp_path, monkeypatch):
    made = make_anndata_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = str(made["table"]).replace(str(tmp_path) + "/", "./")
    answer = _invoke("find_project_for_table", {"table": relative})
    assert answer["result"]["project"] == made["name"]


def test_an_unknown_table_has_no_project(tmp_path):
    answer = _invoke("find_project_for_table", {"table": str(tmp_path / "other.zarr")})
    assert answer["result"] == {"project": None, "candidates": [], "ambiguous": False}


def test_two_projects_on_one_table_are_not_guessed_between(tmp_path):
    first = make_anndata_project(tmp_path, name="a", subset=False)
    make_anndata_project(tmp_path, name="b", subset=False)
    # b wrote its own copy; point it at a's table so both read the same file.
    def same_table(record):
        import dataclasses
        return dataclasses.replace(record, dataset=dataclasses.replace(
            record.dataset, src=first["table"]))
    Project.mutate("b", same_table)
    answer = _invoke("find_project_for_table", {"table": first["table"]})
    assert answer["result"]["project"] is None and answer["result"]["ambiguous"]
    assert {c["project"] for c in answer["result"]["candidates"]} == {"a", "b"}


def test_binding_without_an_image_asks_for_it(tmp_path):
    made = make_anndata_project(tmp_path, register=False)
    answer = _invoke("bind_project", {"table": made["table"], "image_id": "slide_A",
                                      "roles": {"image_id": "imageid"}})
    assert not answer["ok"]
    assert answer["error"]["code"] == "precondition_missing"
    assert answer["error"]["detail"]["missing"] == ["image"]


def test_binding_registers_a_project_reading_the_table_where_it_lies(tmp_path):
    made = make_anndata_project(tmp_path, register=False)
    roles = {"cell_id": "CellID", "x": "X_centroid", "y": "Y_centroid",
             "image_id": "imageid", "celltype": "phenotype"}
    answer = _invoke("bind_project", {"table": made["table"], "image": made["image_path"],
                                      "image_id": "slide_A", "roles": roles,
                                      "name": "bound"})
    assert answer["ok"], answer
    result = answer["result"]
    assert result["project"] == "bound" and result["created"] is True
    assert result["receipt"]["persistent_state"] == "config"
    spec = Project.load("bound").dataset
    assert Project.load("bound").dataset is not None
    assert str(spec.src) == made["table"]          # not copied
    assert spec.subset == {"column": "imageid", "value": "slide_A"}
    # A second bind is the same project.
    again = _invoke("bind_project", {"table": made["table"], "image": made["image_path"],
                                     "image_id": "slide_A", "roles": roles})
    assert again["result"] == {"project": "bound", "created": False,
                               "candidates": again["result"]["candidates"]}


def test_an_image_id_needs_to_know_its_column(tmp_path):
    made = make_anndata_project(tmp_path, register=False)
    answer = _invoke("bind_project", {"table": made["table"], "image": made["image_path"],
                                      "image_id": "slide_A"})
    assert answer["error"]["code"] == "invalid_input" and "roles.image_id" in \
        answer["error"]["message"]


def test_unknown_roles_are_refused_by_name(tmp_path):
    made = make_anndata_project(tmp_path, register=False)
    answer = _invoke("bind_project", {"table": made["table"], "image": made["image_path"],
                                      "roles": {"colour": "phenotype"}})
    assert answer["error"]["code"] == "invalid_input" and "colour" in answer["error"]["message"]


def test_the_bridge_binds_through_the_same_capabilities(tmp_path):
    from spatialbridge.errors import BridgeError
    from spatialbridge.tools import BridgeTools

    from plexora.agent.bridge_provider import PlexoraProvider

    made = make_anndata_project(tmp_path)
    tools = BridgeTools(PlexoraProvider())
    answer = tools.call("bridge_bind", {"table": made["table"], "image_id": "slide_A"})
    assert answer["bound"] and answer["project"] == made["name"]
    assert answer["workspace_id"] and answer["created"] is False
    other = make_anndata_project(tmp_path, name="fresh", register=False)
    with pytest.raises(BridgeError) as refused:
        tools.call("bridge_bind", {"table": other["table"], "image_id": "slide_A"})
    assert refused.value.code == "precondition_missing"


# -- a project from an image file (import_image) ----------------------------------


def _image_files(tmp_path, name="tonsil"):
    folder = tmp_path / "_files"
    folder.mkdir(exist_ok=True)
    image, labels, cells = blob_scene(size=256, grid=4, radius=12)
    return {"image": str(write_image_pyramid(folder / f"{name}.ome.tif", image)),
            "mask": str(write_mask_pyramid(folder / f"{name}_mask.tif", labels)),
            "table": str(write_table(folder / f"{name}_cells.csv", cells))}


def test_an_image_alone_makes_a_project_reading_it_where_it_lies(tmp_path):
    files = _image_files(tmp_path)
    answer = _invoke("import_image", {"image": files["image"]})
    assert answer["ok"], answer
    result = answer["result"]
    assert result["project"] == "tonsil" and result["created"] is True
    assert result["image"] == files["image"]
    assert result["receipt"]["persistent_state"] == "config"
    assert result["receipt"]["reversible"] is False
    record = Project.load("tonsil")
    assert str(record.image.src) == files["image"]       # not copied
    assert record.dataset is None and record.segmentation.source is None


def test_a_new_project_takes_the_mask_and_table_it_is_given(tmp_path):
    files = _image_files(tmp_path)
    answer = _invoke("import_image", {**files, "name": "with_cells", "dataset": "cohort"})
    assert answer["ok"], answer
    assert answer["result"]["project"] == "with_cells" and answer["result"]["created"] is True
    assert str(Project.load("with_cells").dataset.src) == files["table"]
    listed = _invoke("list_projects", {"dataset": "cohort"})["result"]["projects"]
    assert [(p["name"], p["segmentation"], p["table"]) for p in listed] == [
        ("with_cells", "present", "present")]


def test_an_image_a_project_already_shows_is_answered_as_it_is(tmp_path, monkeypatch):
    made = make_anndata_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # However the path is spelled, and whatever else comes with it.
    relative = str(made["image_path"]).replace(str(tmp_path) + "/", "./")
    answer = _invoke("import_image", {"image": relative, "mask": made["mask_path"],
                                      "name": "another"})
    assert answer["ok"], answer
    assert answer["result"] == {"project": made["name"], "created": False,
                                "image": made["image_path"]}
    assert Project.find("another") is None
    # One made here is the same project the next time too.
    files = _image_files(tmp_path, name="lymph")
    first = _invoke("import_image", {"image": files["image"]})["result"]
    again = _invoke("import_image", {"image": files["image"]})["result"]
    assert first["created"] is True and again == {"project": first["project"], "created": False,
                                                  "image": files["image"]}


def test_a_missing_or_unreadable_image_is_refused_and_nothing_is_made(tmp_path):
    import os

    answer = _invoke("import_image", {"image": str(tmp_path / "nowhere.ome.tif")})
    assert not answer["ok"]
    assert answer["error"]["code"] == "invalid_input" and "no image at" in answer["error"]["message"]
    junk = tmp_path / "junk.tif"
    junk.write_text("not an image")
    answer = _invoke("import_image", {"image": str(junk)})
    assert answer["error"]["code"] == "invalid_input"
    files = _image_files(tmp_path)
    answer = _invoke("import_image", {"image": files["image"], "mask": str(tmp_path / "no_mask.tif")})
    assert answer["error"]["code"] == "invalid_input" and "no_mask.tif" in answer["error"]["message"]
    os.chmod(files["image"], 0)
    try:
        if not os.access(files["image"], os.R_OK):      # not as root, nor on Windows
            answer = _invoke("import_image", {"image": files["image"]})
            assert answer["error"]["code"] == "invalid_input"
            assert "cannot be read" in answer["error"]["message"]
    finally:
        os.chmod(files["image"], 0o644)
    assert _invoke("list_projects", {})["result"]["projects"] == []


def test_the_bridge_imports_an_image_through_the_same_capability(tmp_path):
    from spatialbridge.errors import BridgeError

    from plexora.agent.bridge_provider import PlexoraProvider

    files = _image_files(tmp_path)
    provider = PlexoraProvider()
    made = provider.invoke("import_image", {"image": files["image"]})
    assert made["project"] == "tonsil" and made["created"] is True and made["operation_id"]
    again = provider.invoke("import_image", {"image": files["image"]})
    assert again["project"] == "tonsil" and again["created"] is False
    with pytest.raises(BridgeError) as refused:
        provider.invoke("import_image", {"image": str(tmp_path / "nowhere.ome.tif")})
    assert refused.value.code == "invalid_input"
