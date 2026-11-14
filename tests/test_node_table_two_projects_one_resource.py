"""Two projects reading different images' cells out of one table on a node.

A cohort table on a node (one .h5ad spanning several slides) is imported once
per slide, and every one of those projects is bound to the SAME node resource:
the resource id comes from the file's path, and only the read spec's `subset`
differs. The node held one loaded spec per resource, so whichever project
loaded last decided what every other project's reads returned (audit
nr-node-1). These tests pin that each project keeps reading its own image's
cells whoever loaded since.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from tests.node_harness import node_process  # noqa: F401 - fixture


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _multi_image_h5ad(directory):
    """Three slides' cells in one table, at coordinate ranges far enough apart
    that reading another slide's rows cannot pass as an off-by-one."""
    import anndata as ad
    import pandas as pd

    rows, spatial, names = [], [], []
    for offset, image_id in ((0.0, "image_01"), (10_000.0, "image_02"),
                             (20_000.0, "image_03")):
        for index in range(5):
            names.append(f"{image_id}_cell_{index}")
            rows.append(image_id)
            spatial.append([offset + index, offset + index * 2])
    adata = ad.AnnData(
        X=np.random.default_rng(1).random((len(names), 3)).astype(np.float32),
        obs=pd.DataFrame({"image_id": rows}, index=names),
        var=pd.DataFrame(index=[f"protein_{i}" for i in range(3)]))
    adata.obsm["spatial"] = np.asarray(spatial, dtype=np.float64)
    path = directory / "cells.h5ad"
    adata.write_h5ad(path)
    return path


def _image_file(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _import_slide(client, tmp_path, name, image_id):
    answer = client.post("/import/sample", json={
        "paths": [str(_image_file(tmp_path / name)), "node://laptop/cells"],
        "name": name,
        "answers": {"subset_column": "image_id", "subset_value": image_id},
    })
    assert answer.status_code == 200, answer.get_data(as_text=True)


def _two_slides(client, tmp_path, node_process):
    from plexora.server.models.project import Project

    node = node_process(f"table:cells={_multi_image_h5ad(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})
    _import_slide(client, tmp_path, "slide-one", "image_01")
    _import_slide(client, tmp_path, "slide-two", "image_02")
    first, second = Project.load("slide-one"), Project.load("slide-two")
    # The premise: both projects are bound to one node resource and differ
    # only in which image's rows their read spec keeps.
    assert first.resource("table").resource_id == second.resource("table").resource_id
    assert dict(first.dataset.subset)["value"] == "image_01"
    assert dict(second.dataset.subset)["value"] == "image_02"
    return first, second


def test_a_project_keeps_reading_its_own_slide_after_another_loads_the_same_node_table(
        client, tmp_path, node_process):
    """Two NodeTableProviders over one node table, each with its own subset.

    The second load must not change what the first provider's reads return:
    slide one's x coordinates are 0..4, slide two's 10000..10004.
    """
    from plexora.server.providers.node import NodeTableProvider

    first, second = _two_slides(client, tmp_path, node_process)

    one = NodeTableProvider(first.resource("table"), first.dataset)
    one.load()
    before = one.all_cells(["X"], float)
    assert before.max() < 10_000.0, before

    NodeTableProvider(second.resource("table"), second.dataset).load()

    after = one.all_cells(["X"], float)
    assert after.max() < 10_000.0, (
        f"slide one's provider read another slide's cells after slide two "
        f"loaded the same node table: X={after.tolist()}")
    np.testing.assert_array_equal(after, before)


def test_the_viewer_keeps_its_slide_when_the_agent_opens_another_from_the_same_node_table(
        client, tmp_path, node_process):
    """The viewer has slide one open; the agent session reads slide two.

    The viewer's next whole-column read is still slide one's cells, not slide
    two's matched up by row position.
    """
    from plexora.agent.session import AgentSession
    from plexora.server.models import data_model

    _two_slides(client, tmp_path, node_process)

    data_model.load_datasource("slide-one", reload=True)
    before = data_model.get_all_cells("slide-one", ["X"], float)
    assert before.max() < 10_000.0, before

    agent_view = AgentSession().data("slide-two")
    assert agent_view is not None

    after = data_model.get_all_cells("slide-one", ["X"], float)
    assert after.max() < 10_000.0, (
        f"the viewer's slide-one read another slide's cells after the agent "
        f"loaded slide-two from the same node table: X={after.tolist()}")
    np.testing.assert_array_equal(after, before)


# -- the pieces under the two tests above ------------------------------------


def _in_process_node(path):
    from plexora.server.node.app import create_node_app

    app = create_node_app([f"table:cells={path}"], "tok", log=lambda *a, **k: None)
    return app.test_client()


NODE_TOKEN = {"X-Plexora-Node-Token": "tok"}


def test_an_operation_runs_against_the_project_it_names(client, tmp_path, node_process):
    """A write -- an ROI's cell columns, gating's `uns` -- goes to the rows of
    the project that sent it, whichever slide the node loaded last."""
    from plexora.api import table_operation
    from plexora.server.providers.http import SPEC_PARAM, spec_key

    @table_operation("audit_test.image_ids")
    def _image_ids(dataset, payload):
        # Which slides' cells this dataset holds, from their ids
        # (`image_01_cell_3`): whatever the loaded frame calls its columns.
        frame = dataset.table.frame()
        found = set()
        for column in frame.columns:
            for value in frame[column].to_list():
                if isinstance(value, str) and "_cell_" in value:
                    found.add(value.split("_cell_")[0])
        return sorted(found)

    first, second = _two_slides(client, tmp_path, node_process)
    node = _in_process_node(tmp_path / "cells.h5ad")
    keys = []
    for project in (first, second):
        answer = node.post("/node/v1/table/cells/load", headers=NODE_TOKEN,
                           json={"spec": project.dataset.to_dict()})
        assert answer.status_code == 200, answer.get_data(as_text=True)
        # The node names the spec the way the primary does, with no round trip.
        assert answer.get_json()["spec"] == spec_key(project.dataset.to_dict())
        keys.append(answer.get_json()["spec"])

    for key, expected in zip(keys, (["image_01"], ["image_02"])):
        answer = node.post(f"/node/v1/table/cells/op/audit_test.image_ids?{SPEC_PARAM}={key}",
                           headers=NODE_TOKEN, json={})
        assert answer.get_json()["result"] == expected
    # A primary too old to name a spec gets the one loaded last, as before.
    answer = node.post("/node/v1/table/cells/op/audit_test.image_ids",
                       headers=NODE_TOKEN, json={})
    assert answer.get_json()["result"] == ["image_02"]


def test_a_spec_the_node_no_longer_holds_is_a_typed_409(client, tmp_path, node_process,
                                                       monkeypatch):
    """Evicted by other projects' specs: answered with the header that tells
    the primary to load it again, not with another project's rows."""
    from plexora.server.node import resources as node_resources
    from plexora.server.providers.http import SPEC_MISSING_HEADER, SPEC_PARAM

    monkeypatch.setattr(node_resources, "LOADED_SPECS", 1)
    first, second = _two_slides(client, tmp_path, node_process)
    node = _in_process_node(tmp_path / "cells.h5ad")
    keys = [node.post("/node/v1/table/cells/load", headers=NODE_TOKEN,
                      json={"spec": project.dataset.to_dict()}).get_json()["spec"]
            for project in (first, second)]

    evicted = node.get(f"/node/v1/table/cells/describe?{SPEC_PARAM}={keys[0]}",
                       headers=NODE_TOKEN)
    assert evicted.status_code == 409
    assert evicted.headers.get(SPEC_MISSING_HEADER) == keys[0]
    kept = node.get(f"/node/v1/table/cells/describe?{SPEC_PARAM}={keys[1]}",
                    headers=NODE_TOKEN)
    assert kept.status_code == 200


def test_the_primary_reads_a_409_with_the_header_as_spec_not_loaded():
    from types import SimpleNamespace

    from plexora.server.providers import http
    from plexora.server.providers.base import SpecNotLoaded

    node = SimpleNamespace(name="laptop")
    response = SimpleNamespace(status=409, data=b'{"error": "gone"}',
                               headers={http.SPEC_MISSING_HEADER: "abc"})
    with pytest.raises(SpecNotLoaded):
        http._check(node, response, None)
    response.headers = {}
    with pytest.raises(http.ResourceError) as raised:
        http._check(node, response, None)
    assert not isinstance(raised.value, SpecNotLoaded)


def test_a_provider_loads_its_spec_again_and_repeats_the_read(monkeypatch):
    """The node restarted, or evicted this spec: one reload, then the read."""
    from types import SimpleNamespace

    from plexora.server.providers import http
    from plexora.server.providers.base import SpecNotLoaded
    from plexora.server.providers.node import NodeTableProvider

    spec = SimpleNamespace(to_dict=lambda: {"src": "cells.h5ad",
                                            "subset": {"column": "image_id",
                                                       "value": "image_01"}})
    provider = NodeTableProvider(SimpleNamespace(node="laptop", resource_id="cells"), spec)
    monkeypatch.setattr(NodeTableProvider, "node", property(lambda self: object()))
    asked, loads = [], []

    def json_request(node, method, path, **kwargs):
        asked.append(path)
        if len(asked) == 1:
            raise SpecNotLoaded("gone")
        return {"described": True}

    monkeypatch.setattr(http, "json_request", json_request)
    monkeypatch.setattr(provider, "load", lambda *a, **k: loads.append(1))

    assert provider.describe() == {"described": True}
    assert loads == [1]
    key = http.spec_key(spec.to_dict())
    assert asked == [f"/node/v1/table/cells/describe?{http.SPEC_PARAM}={key}"] * 2


def test_the_spec_key_survives_the_json_round_trip():
    import json

    from plexora.server.providers.http import spec_key

    sent = {"src": "a.h5ad", "columns": {"markers": ("CD3", "CD8")},
            "subset": {"column": "image_id", "value": "image_01"}, "scale": 0.5}
    assert spec_key(json.loads(json.dumps(sent))) == spec_key(sent)
    assert spec_key({**sent, "subset": {"column": "image_id", "value": "image_02"}}) \
        != spec_key(sent)
