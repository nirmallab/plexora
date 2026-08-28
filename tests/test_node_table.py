"""A project whose cell table lives on another machine.

Every test here starts a real second process and talks to it over a socket --
see `tests/node_harness.py` for why a stub would prove nothing.

The claim under test is equivalence, not merely function: a project reading its
table through a node must answer the viewer's questions with the same bytes a
project reading it from disk answers with. So most of these compare a node read
against a local read of the same file rather than against a hand-written
expectation, which is the only comparison that stays honest when the local path
changes.
"""

from __future__ import annotations

import gzip

import numpy as np
import polars as pl
import pytest

from tests.helpers import ALL_CONFIRMED, csv_spec, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture


CELL_COUNT = 40


def _table_file(directory):
    """A small quantification table with everything a role can name.

    `imageid` is text on purpose: it is the column that decides whose cells an
    ROI export writes onto, and the compact copy the primary keeps has to carry
    it as text rather than as a float that used to be text.
    """
    frame = pl.DataFrame({
        "CellID": list(range(1, CELL_COUNT + 1)),
        "X_centroid": [float(i * 3.5) for i in range(CELL_COUNT)],
        "Y_centroid": [float(i * 1.25) for i in range(CELL_COUNT)],
        "imageid": ["slide-a"] * CELL_COUNT,
        "phenotype": ["Tumor" if i % 2 else "Stroma" for i in range(CELL_COUNT)],
        "CD3": [float(i) * 1.5 for i in range(CELL_COUNT)],
        "CD8": [float(CELL_COUNT - i) for i in range(CELL_COUNT)],
    })
    path = directory / "cells.csv"
    frame.write_csv(path)
    return path


def _image_file(directory):
    """A tiny two-channel OME-TIFF, so a project can open at all.

    The image is beside the point here -- these tests are about the table --
    but `load_datasource` opens it on every load, and a project with no image
    is not the case under test.
    """
    import tifffile

    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _spec(src):
    return csv_spec(
        src,
        cell_id="CellID", x="X_centroid", y="Y_centroid",
        celltype="phenotype", image_id="imageid",
        markers=("CD3", "CD8"),
        metadata=("CellID", "X_centroid", "Y_centroid", "imageid", "phenotype"),
        single_image=False,
    )


def _local_project(tmp_path, name, path):
    record = project(name, dataset=_spec(path), confirmed=ALL_CONFIRMED,
                     src=str(_image_file(tmp_path)))
    record.save()
    return record


@pytest.fixture
def node_table(tmp_path, node_process):
    """A project called `remote` whose table is served by a real node."""
    from plexora.nodes import attach_table

    path = _table_file(tmp_path)
    node = node_process(f"table:cells={path}")
    register("testnode", node)

    record = project("remote", dataset=_spec(path), confirmed=ALL_CONFIRMED,
                     src=str(_image_file(tmp_path)))
    record.save()
    attached = attach_table("remote", node="testnode", resource_id="cells")
    return node, attached, path


# -- the handshake --------------------------------------------------------


def test_a_node_refuses_a_request_with_no_token(node_process, tmp_path):
    import urllib.error
    import urllib.request

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(node.url("/node/v1/health"), timeout=30)
    # Health included. A same-machine neighbour is exactly who the token keeps
    # out, so an unauthenticated liveness probe would be a way to enumerate
    # what somebody is working on.
    assert raised.value.code == 403


def test_hello_lists_what_the_node_serves(node_process, tmp_path):
    node = node_process(f"table:cells={_table_file(tmp_path)}")
    hello = node.get("/node/v1/hello")

    assert hello["api_version"] == 1
    assert [r["id"] for r in hello["resources"]] == ["cells"]
    assert hello["resources"][0]["kind"] == "table"
    # A node advertises which file-side operations it can run, so the primary
    # can decline to offer a control rather than offering one that 501s.
    assert "roi.map_to_cells" in hello["capabilities"]


def test_a_node_serving_nothing_refuses_to_start():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "plexora", "node", "serve", "--token", "x"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    output = result.stderr + result.stdout
    assert "nothing to serve" in output
    # And it names both ways out. "Pass --serve" stopped being the only answer
    # when a node could be given files while it runs, and a refusal that hides
    # the other one sends somebody copying paths they did not need to copy.
    assert "--dynamic" in output


# -- the project opens ----------------------------------------------------


def test_attaching_a_table_records_the_node_not_a_path(node_table):
    _node, attached, _path = node_table

    binding = attached.resource("table")
    assert binding is not None and binding.is_node
    assert binding.node == "testnode"
    assert binding.resource_id == "cells"
    # The fingerprint is what the write paths check before touching the user's
    # file, so it has to survive the attach.
    assert binding.fingerprint and binding.fingerprint["size"] > 0
    # And the recorded source is the node, never the node's filesystem layout.
    assert attached.dataset.src == "node://testnode/cells"


def test_the_primary_keeps_ids_coordinates_and_roles_and_nothing_else(node_table):
    from plexora.server.models import data_model

    _node, _attached, _path = node_table
    data_model.load_datasource("remote", reload=True)
    frame = data_model.get_datasource_df()

    assert frame.height == CELL_COUNT
    assert set(frame.columns) == {"id", "CellID", "X_centroid", "Y_centroid",
                                  "imageid", "phenotype"}
    # The markers are the whole point of not copying the table.
    assert "CD3" not in frame.columns
    # Text stays text: `imageid` is what decides whose cells an export writes
    # onto, and a float cast would make every value NaN and every comparison
    # fall through.
    assert frame["imageid"].to_list() == ["slide-a"] * CELL_COUNT
    assert frame["phenotype"][1] == "Tumor"


def test_the_whole_frame_is_refused_rather_than_half_answered(node_table):
    from plexora import api
    from plexora.api import ResourceNotLocal

    _node, _attached, _path = node_table
    table = api.dataset("remote").table

    # geometry() is the honest answer and works.
    assert table.geometry().height == CELL_COUNT
    # frame() would have to hand back a copy with no markers in it, which reads
    # as a table and is not one.
    with pytest.raises(ResourceNotLocal):
        table.frame()


# -- equivalence with a local read ---------------------------------------


def test_all_cells_is_byte_identical_to_a_local_read(node_table, tmp_path):
    from plexora.server.models import data_model

    node, _attached, path = node_table
    _local_project(tmp_path, "here", path)

    data_model.load_datasource("here", reload=True)
    local = data_model.get_all_cells("here", ["CD3"], float)
    data_model.load_datasource("remote", reload=True)
    remote = data_model.get_all_cells("remote", ["CD3"], float)

    assert remote.tobytes() == local.tobytes()

    # And the node's own wire shape is the one the browser already receives --
    # a gzipped float32 buffer -- so the primary can forward it untouched.
    raw = node.get("/node/v1/table/cells/all_cells?columns=CD3&dtype=float", raw=True)
    assert np.frombuffer(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw,
                         dtype=np.float32).tobytes() == local.tobytes()


def test_the_description_matches_a_local_read(node_table, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_table
    _local_project(tmp_path, "here", path)

    data_model.load_datasource("here", reload=True)
    local = data_model.get_datasource_description("here")
    data_model.load_datasource("remote", reload=True)
    remote = data_model.get_datasource_description("remote")

    assert sorted(remote) == sorted(local)
    for column in local:
        assert remote[column]["mean"] == pytest.approx(local[column]["mean"])
        assert len(remote[column]["histogram"]) == len(local[column]["histogram"])


def test_filter_columns_and_gating_masks_match(node_table, tmp_path):
    from plexora.server.models import data_model

    _node, _attached, path = node_table
    _local_project(tmp_path, "here", path)

    gates = {"CD3": (10.0, 40.0)}
    data_model.load_datasource("here", reload=True)
    local = data_model.apply_range_mask(
        data_model.get_filter_columns("here", ["CD3"]), gates)
    data_model.load_datasource("remote", reload=True)
    remote = data_model.apply_range_mask(
        data_model.get_filter_columns("remote", ["CD3"]), gates)

    assert (remote == local).all()
    assert local.any()


def test_a_metadata_column_keeps_its_values(node_table):
    from plexora.server.models import data_model

    _node, _attached, _path = node_table
    data_model.load_datasource("remote", reload=True)

    column = data_model.get_metadata_column("remote", "phenotype")
    assert list(column.values[:4]) == ["Stroma", "Tumor", "Stroma", "Tumor"]


def test_a_hover_pulls_one_whole_row_from_the_node(node_table):
    from plexora.server.models import data_model

    _node, _attached, _path = node_table
    data_model.load_datasource("remote", reload=True)

    row = data_model.query_for_closest_cell(3.5, 1.25, "remote")
    # The markers are in the row even though they are not in the primary's copy
    # of the table -- which is the whole point of fetching by id.
    assert row["CellID"] == 2
    assert row["CD3"] == pytest.approx(1.5)


# -- work that has to happen where the file is ---------------------------


def test_mapping_rois_to_cells_writes_on_the_node(node_table):
    from plexora import api

    _node, _attached, path = node_table
    dataset = api.dataset("remote")

    # A box around the first few cells, in image pixels -- the space ROI
    # geometry is stored in.
    features = [{
        "id": "roi-1", "name": "box", "category_id": "cat-1",
        "geometry": {"type": "Polygon",
                     "coordinates": [[[-1, -1], [11, -1], [11, 5], [-1, 5], [-1, -1]]]},
    }]
    result = dataset.table.run("roi.map_to_cells", {
        "features": features,
        "categories": [{"id": "cat-1", "label": "Tumor", "color": "#ff0000"}],
        "x_column": "X_centroid", "y_column": "Y_centroid",
        "prefix": "rois", "replace": False,
    })

    assert result["ok"] is True
    assert result["n_cells"] == CELL_COUNT
    assert result["n_assigned"] > 0

    # The columns are in the file on the NODE's disk, written by the node.
    written = pl.read_csv(path)
    assert "rois_category" in written.columns
    assert written["rois_category"][0] == "Tumor"
    assert written["rois_category"][CELL_COUNT - 1] == ""


def test_a_refusal_survives_the_wire_as_a_refusal(node_table):
    from plexora import api
    from plexora.plugins.roi.server import tableops

    _node, _attached, _path = node_table
    dataset = api.dataset("remote")
    payload = {
        "features": [],
        "categories": [],
        "x_column": "X_centroid", "y_column": "Y_centroid",
        "prefix": "rois", "replace": False,
    }
    assert dataset.table.run("roi.map_to_cells", payload)["ok"] is True

    # Writing the same columns again without `replace` is refused, and the
    # refusal has to arrive as data the panel can act on -- the names already
    # taken and a free one -- not as a stack trace or a bare 500.
    again = dataset.table.run("roi.map_to_cells", payload)
    assert again["ok"] is False
    assert again["reason"] == tableops.COLUMN_EXISTS
    assert "rois_category" in again["existing"]
    assert again["suggestion"]


def test_exporting_a_csv_streams_from_the_node(node_table):
    from plexora import api

    _node, _attached, _path = node_table
    dataset = api.dataset("remote")

    chunks = list(dataset.table.stream("gating.export_csv", {
        "gates": {"CD3": [10.0, 40.0]},
        "channels": {"CD3": [10.0, 40.0]},
        "selection_ids": [],
        "encoding": "binary",
    }))
    text = b"".join(
        chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        for chunk in chunks).decode("utf-8")

    lines = [line for line in text.splitlines() if line]
    assert lines[0].split(",")[:2] == ["id", "CellID"]
    # Every row of the table, gated in place -- the export is the whole table
    # by construction, which is why it streams.
    assert len(lines) == CELL_COUNT + 1


# -- failure ---------------------------------------------------------------


def test_a_node_that_goes_away_is_reported_as_unavailable(node_table):
    from plexora.api import ResourceUnavailable
    from plexora.server.models import data_model

    node, _attached, _path = node_table
    data_model.load_datasource("remote", reload=True)
    node.stop()

    with pytest.raises(ResourceUnavailable) as raised:
        data_model.get_all_cells("remote", ["CD3"], float)
    # Named, because "something is unreachable" is not something a user can act
    # on and "the node called testnode" is.
    assert "testnode" in str(raised.value)


def test_the_project_still_opens_when_its_node_is_gone(node_table):
    """A node that is asleep must not cost the user their project.

    Their ROIs, figures and gates are all on this machine and all still there;
    what is missing is a table. So the load succeeds with the table absent and
    says which node is not answering, rather than failing the page.
    """
    from plexora import app
    from plexora.server.models import data_model

    node, _attached, _path = node_table
    node.stop()

    data_model.load_datasource("remote", reload=True)
    assert data_model.get_datasource_df() is None
    assert data_model.get_current_ball_tree() is None
    # The image is local and unaffected -- a project does not become unopenable
    # because one of its three resources went away.
    assert data_model.get_current_channels() is not None

    answer = app.test_client().get("/resource_status?datasource=remote").get_json()
    assert "table" in answer["unavailable"]
    assert answer["nodes"] == ["testnode"]
    assert "testnode" in answer["unavailable"]["table"]


def test_a_project_with_everything_present_reports_nothing_unavailable(
        node_table):
    from plexora import app
    from plexora.server.models import data_model

    _node, _attached, _path = node_table
    data_model.load_datasource("remote", reload=True)

    answer = app.test_client().get("/resource_status?datasource=remote").get_json()
    # `reconnect` is how to bring a missing node back, and there is nothing
    # missing -- so it is present and empty rather than absent, which is what
    # lets the banner read it without checking whether the key exists.
    assert answer == {"unavailable": {}, "nodes": [], "reconnect": None}
