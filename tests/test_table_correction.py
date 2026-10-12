"""A table read the way its mask needs it, without writing the file.

The case these exist for is a real project: a 3-D quantification table whose
ids are row positions (0..N-1) against a mask labelled 1..N, and whose
centroids are microns at 0.07 um/px over an image that states 0.14 um/px.
Both warnings fired (consistency.py), and the obvious fixes were both wrong:
subtracting 1 from the ids puts every cell two labels off, and scaling by the
image's stated pixel size puts every cell at half its position. Only looking
the cells up in the mask tells the right correction from the plausible one --
which is what `table_correction.propose` does, and what most of this checks.
"""

from __future__ import annotations

import time

import numpy as np
import polars as pl
import pytest
import tifffile

from plexora.server.models import table_correction as tc
from plexora.server.models.project import ColumnRoles, DataSpec

from tests.helpers import ALL_CONFIRMED, csv_spec, project


# -- a mask and a table written the wrong way --------------------------------

def _grid_mask(width=400, height=300, cell=10, gap=8):
    """Square cells labelled 1..N, and each one's centre in pixels."""
    labels = np.zeros((height, width), np.uint32)
    centres = []
    for top in range(6, height - cell, cell + gap):
        for left in range(6, width - cell, cell + gap):
            labels[top:top + cell, left:left + cell] = len(centres) + 1
            centres.append((left + cell / 2, top + cell / 2))
    return labels, np.asarray(centres)


def _reader(labels, planes_read=None):
    """The mask's look-up, as table_correction.mask_window_reader makes it:
    a small square, on the planes around `plane` for a label volume."""
    def read(col, row, plane=None):
        window = labels[..., max(0, row - 1):row + 2, max(0, col - 1):col + 2]
        if labels.ndim == 3:
            assert plane is not None, "a column through every plane was asked for"
            window = window[plane:plane + 1]
            if planes_read is not None:
                planes_read.append(window.shape[0])
        return window
    return read


def _table(centres, um_per_px, *, z=None):
    """Row k is label k+1 -- the positional-id case -- in microns."""
    columns = {"id": np.arange(len(centres), dtype=np.int64),
               "X": centres[:, 0] * um_per_px, "Y": centres[:, 1] * um_per_px}
    if z is not None:
        columns["Z"] = z
    return pl.DataFrame(columns)


ROLES = ColumnRoles(cell_id="id", x="X", y="Y")


def test_the_mask_tells_the_right_pixel_size_from_the_stated_one():
    """The image says 0.14 um/px; the table was measured at 0.07. Both put the
    cells in the corner, and the mask is the only thing that says which."""
    labels, centres = _grid_mask()
    proposal = tc.propose(_table(centres, 0.07), ROLES, width=400, height=300,
                          pixel_size=0.14, read_window=_reader(labels),
                          reasons=["ids_below_one", "cells_in_a_corner"])

    correction = proposal["correction"]
    assert correction["cell_id"] == {"from": "id", "offset": 1, "column": "mask_label"}
    assert correction["scale"]["x"] == pytest.approx(1 / 0.07)
    assert correction["scale"]["y"] == pytest.approx(1 / 0.07)
    assert "the image says 0.14" in correction["units"]["xy"]
    checked = correction["checked"]
    assert checked["matched"] == checked["cells"] and checked["before"] <= 2
    assert "Your table file is unchanged" in proposal["message"]


def test_the_stated_pixel_size_wins_when_it_is_the_right_one():
    labels, centres = _grid_mask()
    correction = tc.propose(_table(centres, 0.14), ROLES, width=400, height=300,
                            pixel_size=0.14, read_window=_reader(labels))["correction"]

    assert correction["scale"]["x"] == pytest.approx(1 / 0.14)
    assert correction["units"]["xy"] == "microns at 0.14 µm per pixel"


def test_ids_alone_are_corrected_when_the_positions_are_already_pixels():
    labels, centres = _grid_mask()
    correction = tc.propose(_table(centres, 1.0), ROLES, width=400, height=300,
                            pixel_size=0.5, read_window=_reader(labels))["correction"]

    assert correction["cell_id"]["offset"] == 1
    assert "scale" not in correction


def test_a_table_that_already_lines_up_is_left_alone():
    labels, centres = _grid_mask()
    frame = _table(centres, 1.0).with_columns((pl.col("id") + 1).alias("CellID"))

    proposal = tc.propose(frame, ColumnRoles(cell_id="CellID", x="X", y="Y"),
                          width=400, height=300, read_window=_reader(labels))

    assert proposal["correction"] is None
    assert "already lines up" in proposal["message"]


def test_a_table_of_another_mask_is_not_forced_onto_this_one():
    """Nothing tried matches, so nothing is applied -- a plausible-looking
    scale must not be recorded on a table that belongs to something else."""
    labels, centres = _grid_mask()
    rng = np.random.default_rng(3)
    frame = _table(rng.permutation(centres), 0.07)

    proposal = tc.propose(frame, ROLES, width=400, height=300, pixel_size=0.14,
                          read_window=_reader(labels))

    assert proposal["correction"] is None
    assert "different mask or image" in proposal["message"]


def test_depth_is_found_from_the_same_column_reads():
    """A label volume: each cell spans a few planes and the table's z is
    microns at the stated z step. Each cell is looked up on the plane its z
    names, so the x/y and z scales are tried together."""
    plane, centres = _grid_mask(width=200, height=150)
    depth = 12
    volume = np.zeros((depth,) + plane.shape, np.uint32)
    rng = np.random.default_rng(0)
    centre_z = rng.integers(2, depth - 2, size=len(centres))
    for label, cz in enumerate(centre_z, start=1):
        volume[cz - 1:cz + 2][:, plane == label] = label
    frame = _table(centres, 0.07, z=(centre_z + 0.5) * 2.0)

    planes_read = []
    correction = tc.propose(
        frame, ColumnRoles(cell_id="id", x="X", y="Y", z="Z"), width=200, height=150,
        depth=depth, pixel_size=0.14, z_step=2.0,
        read_window=_reader(volume, planes_read))["correction"]

    # A plane to a chunk on disk: a look-up that read the whole column would
    # be the stack's depth in chunks for every cell tried.
    assert planes_read and max(planes_read) == 1
    assert correction["cell_id"]["offset"] == 1
    assert correction["scale"]["x"] == pytest.approx(1 / 0.07)
    assert correction["scale"]["z"] == pytest.approx(0.5)


def test_a_label_volume_is_read_a_few_planes_at_a_time(monkeypatch):
    """What the reader asks the volume for: the one plane named, or three
    spread through the stack for a table with no z -- never a column through
    all of them (a plane is a chunk on disk, and on a node a request)."""
    from plexora.server.volume import labels as volume_labels

    calls = []

    class Volume:
        depth, height, width = 40, 50, 60

        def read(self, level, z0, z1, rows, cols):
            calls.append((level, z0, z1, rows.start, rows.stop, cols.start, cols.stop))
            return np.zeros((z1 - z0 + 1, rows.stop - rows.start, cols.stop - cols.start),
                            np.uint32)

    monkeypatch.setattr(volume_labels, "project_label_volume", lambda project: Volume())
    read, workers = tc.mask_window_reader(project("p", segmentation="/tmp/mask.zarr"))

    assert read(10, 20, 5).shape == (1, 3, 3)
    assert read(0, 0, None).shape == (3, 2, 2)
    assert calls == [(0, 5, 5, 19, 22, 9, 12),
                     (0, 10, 10, 0, 2, 0, 2), (0, 20, 20, 0, 2, 0, 2), (0, 30, 30, 0, 2, 0, 2)]
    assert workers == 1


def test_without_a_mask_only_the_coordinates_are_estimated():
    _labels, centres = _grid_mask()
    proposal = tc.propose(_table(centres, 0.5), ROLES, width=400, height=300,
                          pixel_size=0.5, reasons=["cells_in_a_corner"])

    correction = proposal["correction"]
    assert "cell_id" not in correction
    assert correction["scale"]["x"] == pytest.approx(2.0)
    assert correction["estimated"] is True
    assert "no mask to check it against" in proposal["message"]


# -- applying it -------------------------------------------------------------

def test_applying_writes_a_column_of_its_own_and_scales_in_place():
    """The positional `id` stays the row's identity -- hover and row lookups
    use it -- and the corrected ids live beside it."""
    frame = pl.DataFrame({"id": [0, 1, 2], "X": [1.0, 2.0, 3.0], "Y": [4.0, 5.0, 6.0],
                          "Z": [1.0, 1.5, 2.0], "CD3": [7.0, 8.0, 9.0]})
    correction = {"cell_id": {"from": "id", "offset": 1, "column": "mask_label"},
                  "scale": {"x": 2.0, "y": 2.0, "z": 0.5}}

    out = tc.apply_frame(frame, correction, {"x": "X", "y": "Y", "z": "Z"})

    assert out["id"].to_list() == [0, 1, 2]
    assert out["mask_label"].to_list() == [1, 2, 3]
    assert out["X"].to_list() == [2.0, 4.0, 6.0]
    assert out["Z"].to_list() == [0.5, 0.75, 1.0]
    assert out["CD3"].to_list() == [7.0, 8.0, 9.0]


def test_a_correction_survives_the_project_record_and_undoes_cleanly(tmp_path):
    spec = csv_spec(tmp_path / "cells.csv", cell_id="id", x="X", y="Y")
    record = project("p", dataset=spec)
    correction = {"cell_id": {"from": "id", "offset": 1, "column": "mask_label"},
                  "scale": {"x": 2.0, "y": 2.0}}

    fixed = tc.corrected_project(record, correction)
    assert fixed.roles.cell_id == "mask_label"
    again = DataSpec.from_dict(fixed.dataset.to_dict())
    assert again.correction == correction and again.roles.cell_id == "mask_label"

    undone = tc.uncorrected_project(fixed)
    assert undone.roles.cell_id == "id" and undone.dataset.correction == {}
    assert "correction" not in undone.dataset.to_dict()


def test_undo_keeps_a_cell_id_somebody_chose_since():
    roles = ColumnRoles(cell_id="CellID", x="X", y="Y")
    correction = {"cell_id": {"from": "id", "offset": 1, "column": "mask_label"}}
    assert tc._uncorrected_roles(roles, correction).cell_id == "CellID"


def test_the_signature_changes_with_what_the_values_become():
    assert tc.signature({}) is None
    assert tc.signature({"scale": {"x": 1.0, "y": 1.0}}) is None
    a = tc.signature({"scale": {"x": 2.0, "y": 2.0}})
    b = tc.signature({"scale": {"x": 2.0, "y": 2.0}, "checked": {"cells": 5}})
    c = tc.signature({"scale": {"x": 4.0, "y": 4.0}})
    assert a == b and a != c


# -- through the routes, on a real project -----------------------------------

UM_PER_PX = 0.5


@pytest.fixture
def mislabelled(tmp_path, monkeypatch):
    """A 256 px image stating 0.5 um/px, a mask labelled 1..N, and a table of
    the same cells with positional ids and centroids in microns at 0.25 um/px
    -- half the stated size, the case the metadata alone gets wrong."""
    import plexora
    from plexora.server.models import data_model
    from plexora.server.routes.import_routes import attach_segmentation

    for name in ("ball_tree", "source", "config", "seg", "zarray", "channels",
                 "metadata", "_loaded_source", "datasource"):
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8), ome=True,
                     metadata={"axes": "CYX", "PhysicalSizeX": UM_PER_PX,
                               "PhysicalSizeY": UM_PER_PX})
    labels, centres = _grid_mask(width=256, height=256, cell=8, gap=6)
    mask = tmp_path / "mask.tif"
    tifffile.imwrite(mask, labels)
    table = tmp_path / "cells.csv"
    pl.DataFrame({"X": centres[:, 0] * UM_PER_PX / 2, "Y": centres[:, 1] * UM_PER_PX / 2,
                  "CD3": np.linspace(0, 1, len(centres))}).write_csv(table)
    spec = csv_spec(table, cell_id="id", x="X", y="Y", markers=("CD3",),
                    metadata=("X", "Y"))
    project("mis", dataset=spec, confirmed=ALL_CONFIRMED, src=str(image),
            width=256, height=256).save()
    attach_segmentation("mis", str(mask))
    deadline = time.monotonic() + 60
    while data_model.get_segmentation_job_status("mis").get("status") not in ("ready", "error"):
        assert time.monotonic() < deadline, "the mask did not convert"
        time.sleep(0.1)
    data_model.load_datasource("mis", reload=True)
    return plexora.app.test_client(), table, len(centres)


def _codes(client):
    return {f["code"]: f for f in client.get("/get_consistency_report?datasource=mis").get_json()}


def test_fix_reads_the_table_onto_its_mask_and_undo_puts_it_back(mislabelled):
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    client, table, cells = mislabelled
    before = table.read_bytes()
    findings = _codes(client)
    assert findings["ids_below_one"]["fix"] == "table"
    assert findings["cells_in_a_corner"]["fix"] == "table"

    reply = client.post("/fix_table", json={"datasource": "mis"}).get_json()

    assert reply["success"] and reply["applied"] and reply["reloaded"]
    assert "Your table file is unchanged" in reply["message"]
    assert table.read_bytes() == before
    record = Project.find("mis")
    assert record.roles.cell_id == "mask_label"
    assert record.dataset.correction["scale"]["x"] == pytest.approx(4.0)
    findings = _codes(client)
    assert "ids_below_one" not in findings and "cells_in_a_corner" not in findings
    assert findings[tc.APPLIED_CODE]["undo"] is True
    assert "Your table file is unchanged" in findings[tc.APPLIED_CODE]["message"]

    values = data_model.get_all_cells("mis", ["mask_label", "X"], float).reshape(-1, 2)
    raw = pl.read_csv(table)
    assert values[:, 0].tolist() == list(range(1, cells + 1))
    assert np.allclose(values[:, 1], raw["X"].to_numpy() * 4.0)

    reply = client.post("/undo_table_fix", json={"datasource": "mis"}).get_json()

    assert reply["success"] and reply["reloaded"]
    record = Project.find("mis")
    assert record.roles.cell_id == "id" and not record.dataset.correction
    assert "ids_below_one" in _codes(client)
    assert table.read_bytes() == before


def test_the_caches_built_from_positions_are_keyed_by_the_correction(mislabelled):
    from plexora.server.models import centroid_tiles, data_model
    from plexora.server.models.project import Project

    client, _table_path, _cells = mislabelled
    config = data_model.get_current_config()
    plain = centroid_tiles._expected_manifest(config, "mis")
    client.post("/fix_table", json={"datasource": "mis"})
    fixed = centroid_tiles._expected_manifest(data_model.get_current_config(), "mis")

    assert plain["correction"] is None
    assert fixed["correction"] == tc.signature(Project.find("mis").dataset.correction)


# -- on a data node ----------------------------------------------------------

from tests.node_harness import node_process, register  # noqa: E402,F401 - fixture


def test_a_node_reads_its_table_through_the_correction(tmp_path, node_process):
    """The node loads the table itself, so it must apply the record itself:
    every node route -- all_cells, rows, ROI mapping, gating -- then serves the
    corrected values with no per-route translation on the primary."""
    from plexora.nodes import attach_table
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    path = tmp_path / "cells.csv"
    pl.DataFrame({"X": [1.0, 2.0, 3.0], "Y": [4.0, 5.0, 6.0],
                  "CD3": [0.1, 0.2, 0.3]}).write_csv(path)
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    node = node_process(f"table:cells={path}")
    register("testnode", node)
    assert tc.APPLIED_CODE and "table.correction" in node.get("/node/v1/hello")["capabilities"]

    spec = csv_spec(path, cell_id="id", x="X", y="Y", markers=("CD3",), metadata=("X", "Y"))
    project("remote", dataset=spec, confirmed=ALL_CONFIRMED, src=str(image)).save()
    attach_table("remote", node="testnode", resource_id="cells")
    record = Project.find("remote")
    assert data_model.table_correction_blocked(record) is None
    tc.corrected_project(record, {"cell_id": {"from": "id", "offset": 1,
                                              "column": "mask_label"},
                                  "scale": {"x": 10.0, "y": 10.0}}).save()

    data_model.load_datasource("remote", reload=True)
    values = data_model.get_all_cells("remote", ["mask_label", "X", "Y"], float).reshape(-1, 3)

    assert values.tolist() == [[1.0, 10.0, 40.0], [2.0, 20.0, 50.0], [3.0, 30.0, 60.0]]


def test_a_node_too_old_to_correct_is_said_rather_than_ignored(tmp_path, monkeypatch):
    """An old node would read the record's unknown key as nothing and serve the
    file's own values. Offering the button there would apply a fix that is
    silently not applied."""
    from plexora.server.models import data_model
    from plexora.server.models.project import ResourceBinding
    from plexora.server.providers import http, node as node_provider

    record = project("old", dataset=csv_spec(tmp_path / "c.csv", cell_id="id", x="X", y="Y"))
    record = record.patch(resources={"table": ResourceBinding(
        kind="table", provider="node", node="o2", resource_id="cells")})
    monkeypatch.setattr(node_provider, "node_for", lambda binding: object())
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {"capabilities": ["gating.gmm"]})

    message = data_model.table_correction_blocked(record)

    assert "Update Plexora on data node 'o2'" in message
