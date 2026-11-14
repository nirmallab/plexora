"""Two projects reading different images' cells out of one table on a node.

A cohort table on a node (one .h5ad spanning several slides) is imported once
per slide, and every one of those projects is bound to the SAME node resource:
the resource id comes from the file's path, and only the read spec's `subset`
differs. The node holds one loaded spec per resource, so whichever project
loaded last decides what every other project's reads return. These tests pin
that each project keeps reading its own image's cells whoever loaded since.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from tests.node_harness import node_process  # noqa: F401 - fixture

# Confirmed by the 2026-11 audit and not fixed: the fix needs a change to the
# node API (a resource keyed by its read spec), so it waits for a decision
# (docs/internal/AUDIT_2026-11-14.md, nr-node-1). Strict, so a fix shows here.
pytestmark = pytest.mark.xfail(strict=True, reason="audit nr-node-1: confirmed, not fixed")


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
