"""Which project shows a table, and making one when none does.

The bridge's first step whenever SCIMAP Pro wants its table shown: Plexora
answers with the project that READS that file (and that image of it), or
registers one from the image -- the table read where it lies, never copied.
"""

from __future__ import annotations

import pytest

from plexora.agent import AgentSession, registry
from plexora.server.models.project import Project
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
