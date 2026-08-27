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


# -- importing a project whose image is not on this machine ---------------


def _big_image(directory):
    """A pyramidal image, the kind that is on a node because it is too large to
    be anywhere else."""
    rng = np.random.default_rng(5)
    data = rng.integers(0, 3000, (2, 1024, 1024), dtype=np.uint16)
    path = directory / "slide.ome.tif"
    tifffile.imwrite(path, data, photometric="minisblack", tile=(512, 512))
    return path


def test_the_import_form_accepts_a_node_address_for_the_image(
        client, tmp_path, node_process):
    """The whole reason an image is on a node is that it is too large to copy,
    so a form that insists on a local path is a form that cannot be used for
    the case data nodes exist for."""
    from plexora.server.models.project import Project

    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "remote-slide",
        "image_file": "node://o2/slide",
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("remote-slide")
    assert record.resource("image").node == "o2"
    assert record.image.width == 1024 and record.image.num_channels == 2
    # The geometry the viewer needs before it can ask for a tile, recorded
    # centrally -- the node is not asked again per request.
    assert record.image.tile_width == 512


def test_a_node_image_and_a_local_table_import_together(
        client, tmp_path, node_process):
    """The flagship split: the slide is on the cluster, the table came back to
    the laptop."""
    from plexora.server.models.project import Project

    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "split",
        "image_file": "node://o2/slide",
        "data_file": str(_table_file(tmp_path)),
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("split")
    assert record.resource("image").node == "o2"
    # The table stayed here, and went through the same inspection every other
    # import uses -- its roles are guessed, not left blank.
    assert record.resource("table") is None
    assert record.dataset.src.endswith("cells.csv")
    assert record.roles.x == "X_centroid" and record.roles.y == "Y_centroid"


def test_a_malformed_node_address_says_what_the_shape_is(client, tmp_path):
    answer = client.post("/import", data={
        "name": "bad", "image_file": "node://onlyanode",
    })
    # 400 and the form back with what was typed, like every other refusal here.
    assert answer.status_code == 400
    assert "node://&lt;node&gt;/&lt;resource&gt;" in answer.get_data(as_text=True)


def test_a_failed_node_import_leaves_no_half_project(client, tmp_path):
    """A half-registered project is worse than none: it appears in the picker,
    opens onto an error, and the name is taken so the user cannot import over
    it."""
    from plexora.server.models.project import Project

    answer = client.post("/import", data={
        "name": "ghosted", "image_file": "node://nosuchnode/slide",
    })
    assert answer.status_code == 400
    assert "nosuchnode" in answer.get_data(as_text=True)
    assert Project.find("ghosted") is None


def test_the_import_page_offers_what_the_nodes_are_serving(
        client, tmp_path, node_process):
    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    page = client.get("/upload_page").get_data(as_text=True)
    # So nobody has to know the `node://` syntax to use it.
    assert "node://o2/slide" in page
    assert "or an image on a data node" in page
