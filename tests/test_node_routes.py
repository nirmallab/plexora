"""Registering a node, and pointing a project at it, through the app's own routes.

The Python API (`plexora.nodes`) is covered by test_node_table.py; this is the
surface a user reaches without writing any. Both go through a real node process,
because the interesting failures here are the ones where the browser is told
something the server cannot back up -- a node listed as reachable that is not, a
select showing "this machine" for a project that is bound elsewhere.
"""

from __future__ import annotations

import polars as pl
import pytest
import tifffile
import numpy as np

from tests.helpers import ALL_CONFIRMED, csv_spec, project
from tests.node_harness import node_process  # noqa: F401 - fixture


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _table_file(directory):
    pl.DataFrame({
        "CellID": [1, 2, 3],
        "X_centroid": [1.0, 2.0, 3.0],
        "Y_centroid": [1.0, 2.0, 3.0],
        "CD3": [0.5, 1.5, 2.5],
    }).write_csv(directory / "cells.csv")
    return directory / "cells.csv"


def _image_file(directory):
    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _project(tmp_path, name="demo"):
    path = _table_file(tmp_path)
    record = project(
        name,
        dataset=csv_spec(path, cell_id="CellID", x="X_centroid", y="Y_centroid",
                         markers=("CD3",),
                         metadata=("CellID", "X_centroid", "Y_centroid")),
        confirmed=ALL_CONFIRMED, src=str(_image_file(tmp_path)))
    record.save()
    return record, path


def test_the_settings_page_lists_no_nodes_to_begin_with(client):
    answer = client.get("/settings/nodes").get_json()
    assert answer["nodes"] == []


def test_registering_a_node_checks_that_it_answers(client, tmp_path, node_process):
    node = node_process(f"table:cells={_table_file(tmp_path)}")

    added = client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token,
    }).get_json()
    assert added["node"]["name"] == "hpc"
    # Never the token. A settings page that showed it would put it in the first
    # screenshot anybody sent asking for help.
    assert "token" not in added["node"]
    assert added["node"]["has_token"] is True

    listed = client.get("/settings/nodes").get_json()["nodes"]
    assert [entry["name"] for entry in listed] == ["hpc"]
    assert listed[0]["reachable"] is True
    assert [r["id"] for r in listed[0]["resources"]] == ["cells"]


def test_an_address_that_is_not_a_node_is_refused_at_the_form(client):
    answer = client.post("/settings/nodes", json={
        "name": "nowhere", "endpoint": "http://127.0.0.1:1", "token": "x",
    })
    assert answer.status_code == 400
    # Named, so the user can tell a typo from a node that is not running.
    assert "127.0.0.1:1" in answer.get_json()["error"]
    assert client.get("/settings/nodes").get_json()["nodes"] == []


def test_a_missing_name_is_refused_before_anything_is_contacted(client):
    answer = client.post("/settings/nodes", json={"endpoint": "http://x"})
    assert answer.status_code == 400
    assert "name" in answer.get_json()["error"]


def test_the_edit_page_reports_every_resource_as_local_by_default(client, tmp_path):
    _project(tmp_path)
    answer = client.get("/project/demo/resources").get_json()

    kinds = {entry["kind"]: entry for entry in answer["resources"]}
    assert set(kinds) == {"image", "segmentation", "table"}
    assert kinds["table"]["provider"] == "local"
    assert kinds["table"]["present"] is True
    # No mask on this project, so the section will not draw a row for it.
    assert kinds["segmentation"]["present"] is False


def test_attaching_and_detaching_a_table_through_the_route(client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})

    attached = client.post("/project/demo/resources/table",
                           json={"node": "hpc", "resource_id": "cells"}).get_json()
    table = next(e for e in attached["resources"] if e["kind"] == "table")
    assert table["provider"] == "node"
    assert table["node"] == "hpc"
    # No path, because there is no file at any path on this machine -- see
    # ResourceLocator.
    assert table["path"] is None

    # Detaching without saying where the file is HERE is refused, and the
    # refusal names the field to use: a project whose table is on a node has no
    # local copy by construction, so "bring it back" without an answer would
    # leave it pointing at nothing.
    refused = client.post("/project/demo/resources/table", json={})
    assert refused.status_code == 400
    assert "Data field" in refused.get_json()["error"]

    detached = client.post("/project/demo/resources/table",
                           json={"path": str(path)}).get_json()
    assert "error" not in detached, detached
    table = next(e for e in detached["resources"] if e["kind"] == "table")
    assert table["provider"] == "local"
    assert table["path"] == str(path)
    # And the project keeps every answer it had recorded about the table.
    from plexora.server.models.project import Project

    assert Project.load("demo").roles.cell_id == "CellID"


def test_attaching_to_a_node_that_is_not_registered_says_which_are(client, tmp_path):
    _project(tmp_path)
    answer = client.post("/project/demo/resources/table",
                         json={"node": "ghost", "resource_id": "cells"})
    assert answer.status_code == 400
    message = answer.get_json()["error"]
    assert "ghost" in message and "known nodes" in message


def test_a_node_that_stops_answering_is_still_shown_as_this_project_s_source(
        client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    client.post("/project/demo/resources/table",
                json={"node": "hpc", "resource_id": "cells"})

    node.stop()
    answer = client.get("/project/demo/resources").get_json()

    table = next(e for e in answer["resources"] if e["kind"] == "table")
    # The binding is a fact about the project and does not evaporate because a
    # laptop closed its lid. A page that reported "this machine" here would be
    # one save away from making that true.
    assert table["provider"] == "node" and table["node"] == "hpc"
    hpc = next(entry for entry in answer["nodes"] if entry["name"] == "hpc")
    assert hpc["reachable"] is False


def test_forgetting_a_node_names_the_projects_that_were_using_it(
        client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    client.post("/project/demo/resources/table",
                json={"node": "hpc", "resource_id": "cells"})

    answer = client.delete("/settings/nodes/hpc").get_json()
    assert answer["projects_affected"] == ["demo"]
    assert client.get("/settings/nodes").get_json()["nodes"] == []
