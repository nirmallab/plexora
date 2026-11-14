"""A time series' object table attached from a node: the viewer's whole-column
read for one frame keeps to that frame's rows, as it does for a local table.

The viewer asks /get_all_cells with t=<frame> so that every cell id appears
once in its id-keyed tables (data_model.get_all_cells). A table held on a data
node must answer the same rows as the same table read locally, not every
frame's rows with ids repeated.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from tests.node_harness import node_process  # noqa: F401 - fixture

FRAMES = 2
CELLS = 5


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _series_csv(directory):
    """Two frames of the same five cells, ids 1..5 in each frame (as a
    measured series' object table has them); frame 1's cells sit far to the
    right of frame 0's, so a row from the wrong frame cannot pass for one."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["CellID,X_centroid,Y_centroid,t,protein_0,protein_1"]
    for t in range(FRAMES):
        for index in range(CELLS):
            lines.append(f"{index + 1},{t * 1000 + index},{t * 1000 + index * 2},{t},"
                         f"{0.1 * index:.2f},{0.2 * index:.2f}")
    path = directory / "series.csv"
    path.write_bytes(("\n".join(lines) + "\n").encode())
    return path


def _image_file(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _import(client, tmp_path, name, table):
    answer = client.post("/import/sample", json={
        "paths": [str(_image_file(tmp_path / name)), table], "name": name})
    assert answer.status_code == 200, answer.get_data(as_text=True)


def _with_frame_role(name):
    from plexora.server.models.project import Project

    Project.mutate(name, lambda p: p.with_roles({"t": "t"}))
    assert Project.load(name).roles.t == "t"


def _frame_reads(name):
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    data_model.load_datasource(name, reload=True)
    keys = ["id", Project.load(name).roles.x]
    return {t: data_model.get_all_cells(name, keys, float, t=t)
            for t in range(FRAMES)}


def test_a_node_tables_cells_for_one_frame_are_that_frames_rows_only(
        client, tmp_path, node_process):
    """get_all_cells(t=<frame>) on a time series' table held on a node returns
    that frame's five rows -- the same values the same table read locally
    gives -- not both frames' ten rows with every id twice."""
    from plexora.server.models import data_model

    csv = _series_csv(tmp_path / "table")

    _import(client, tmp_path, "series-local", str(csv))
    _with_frame_role("series-local")
    local = _frame_reads("series-local")
    assert data_model._remote_table() is None
    # The premise, on the local read: one frame's rows, each id once.
    for t in range(FRAMES):
        assert local[t].size == 2 * CELLS, (t, local[t].tolist())

    node = node_process(f"table:series={csv}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})
    _import(client, tmp_path, "series-node", "node://laptop/series")
    _with_frame_role("series-node")
    remote = _frame_reads("series-node")
    assert data_model._remote_table() is not None

    for t in range(FRAMES):
        rows = remote[t].reshape(-1, 2)
        assert len(rows) == CELLS, (
            f"frame {t} of a time series' table on a node came back with "
            f"{len(rows)} rows, not its {CELLS}: (id, X)={rows.tolist()}")
        np.testing.assert_array_equal(remote[t], local[t])
