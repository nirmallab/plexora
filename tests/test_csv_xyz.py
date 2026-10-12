"""A z-stack with a cell table from a CSV that carries X, Y and Z.

The table is the user's own (not one Plexora measured from a 3-D mask), so
everything that places a cell in depth has to come from the z role the import
guessed. What has to hold: the Z column is a role and never a marker; the viewer
is told which column it is, and the centroid dots it asks for are one plane's
cells; and the crops gating reads a cell's pixels from are cut at the cell's own
plane, not the stack's default one.
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "centroid_planes_probe.mjs"

DEPTH = 10
#: Planes the cells are centred on: even cells low in the stack, odd ones high.
#: Neither is the default plane (the middle, 5), which holds no cell at all.
LOW, HIGH = 2, 7


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _plane(i):
    return HIGH if i % 2 else LOW


def _stack_with_csv(tmp_path, name="xyz", z=True):
    """Sixteen cells, three planes deep, on planes 1-3 or 6-8 of ten; a 3-D
    mask; a CSV naming each cell's X, Y and Z (or, `z=False`, only X and Y),
    attached the way the UI does."""
    import tifffile

    from plexora import datasets
    from plexora.datasource import register_image_datasource

    z_column = z
    rng = np.random.default_rng(3)
    image = rng.normal(100, 5, (2, DEPTH, 96, 96)).clip(0).astype(np.uint16)
    labels = np.zeros((DEPTH, 96, 96), dtype=np.uint16)
    rows = []
    for i in range(16):
        y, x, z = 6 + (i // 4) * 22, 6 + (i % 4) * 22, _plane(i)
        labels[z - 1:z + 2, y:y + 8, x:x + 8] = i + 1
        image[0, z - 1:z + 2, y:y + 8, x:x + 8] = 2000
        image[1, z - 1:z + 2, y:y + 8, x:x + 8] = 3000
        rows.append(f"{i + 1},{x + 3.5},{y + 3.5},{float(z)},2000,3000")
    if not z_column:
        rows = [",".join(row.split(",")[:3] + row.split(",")[4:]) for row in rows]
    path = tmp_path / f"{name}.ome.tif"
    tifffile.imwrite(path, image, ome=True, metadata={
        "axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
        "Channel": {"Name": ["DNA", "CD8"]}})
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    csv = tmp_path / f"{name}_cells.csv"
    header = "CellID,X_centroid,Y_centroid,Z_centroid,DNA,CD8" if z_column \
        else "CellID,X_centroid,Y_centroid,DNA,CD8"
    csv.write_text(header + "\n" + "\n".join(rows) + "\n")
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    datasets.add_layers(name, csv, wait=True)
    return name


def test_the_z_column_of_a_csv_is_a_role_and_not_a_marker(tmp_path, client):
    from plexora.server.models.project import Project

    name = _stack_with_csv(tmp_path)
    project = Project.find(name)
    assert (project.roles.x, project.roles.y, project.roles.z) == \
        ("X_centroid", "Y_centroid", "Z_centroid")
    assert "Z_centroid" not in project.dataset.columns.markers
    assert client.get(f"/volume/{name}/info").get_json()["table_z"] == "Z_centroid"


def test_a_plane_range_keeps_only_that_planes_centroids(tmp_path):
    """What the viewer asks for on plane k: the z column within half a plane."""
    from plexora.server.models import data_model

    name = _stack_with_csv(tmp_path)
    data_model.load_datasource(name, reload=True)
    tile = [{"x": 0, "y": 0}]
    every = data_model.get_centroid_tiles(name, 0, tile, {})
    assert sorted(int(i) for i in every["id"]) == list(range(1, 17))
    for plane in (LOW, HIGH):
        got = data_model.get_centroid_tiles(name, 0, tile,
                                            {"Z_centroid": [plane - 0.5, plane + 0.5]})
        assert sorted(int(i) for i in got["id"]) == \
            [i + 1 for i in range(16) if _plane(i) == plane], plane
    middle = data_model.get_centroid_tiles(name, 0, tile, {"Z_centroid": [4.5, 5.5]})
    assert middle.size == 0
    # With a gate as well, both narrow it.
    gated = data_model.get_centroid_tiles(
        name, 0, tile, {"Z_centroid": [HIGH - 0.5, HIGH + 0.5], "CD8": [2999, 3000]})
    assert gated.size == 8


def test_gating_crops_read_each_cell_at_its_own_plane(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import crops as cropmod
    from plexora.server.utils import source_image

    name = _stack_with_csv(tmp_path)
    session = AgentSession()
    record = session.project(name)
    cells = [{"cell_id": i + 1, "x": 6 + (i % 4) * 22 + 3.5, "y": 6 + (i // 4) * 22 + 3.5}
             for i in range(16)]
    channels = {n: source_image.channel_key(c) for n, c in
                ((c.get("fullname") or c.get("name"), c) for c in record.image.real_channels)}
    got, info = cropmod.read_cell_crops(session, record, cells, channels=channels,
                                        crop_px=16, tile_px=64, level=0)
    assert info["planes"] == [LOW, HIGH]
    for i in range(16):
        crop = got[i + 1]
        assert crop.z == _plane(i), i
        # The middle plane holds background (~100); the cell's plane holds it.
        assert float(np.median(cropmod.in_disc(crop, "CD8", 3.0))) > 2000, i
        centre = crop.labels.shape[0] // 2
        assert int(crop.labels[centre, centre]) == i + 1, i


def test_a_flat_image_reads_crops_as_before(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import crops as cropmod
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    session = AgentSession()
    record = session.project("synth")
    assert cropmod.cell_planes(session, record, [{"cell_id": 1, "x": 1, "y": 1}]) == {}


def test_a_table_registered_from_python_keeps_its_z_role(tmp_path):
    from plexora.datasource import flat_table_spec

    schema = [{"name": n, "dtype": "float64"} for n in
              ("CellID", "X_centroid", "Y_centroid", "Z_centroid", "CD8")]
    spec = flat_table_spec(tmp_path / "cells.csv", schema)
    assert spec.roles.z == "Z_centroid"
    assert "Z_centroid" not in spec.columns.markers


@pytest.mark.parametrize("column", ["Z", "z_centroid", "Centroid_Z", "Z_position",
                                    "z_plane", "ZSlice", "z_index", "plane"])
def test_a_plane_column_is_recognised_as_z(column):
    from plexora.server.models.adapters.classify import classify_columns

    out = classify_columns([{"name": n, "dtype": "float64"} for n in
                            ("CellID", "X", "Y", column, "CD8")])
    assert out["roles"].get("z") == column
    assert column not in out["markers"]


def _keys(client, name, tool="gating"):
    needs = client.get(f"/{name}/tools/{tool}/requirements").get_json()
    return {kind: [r["key"] for r in needs.get(kind, [])]
            for kind in ("missing", "confirm", "optional")}


def test_on_a_z_stack_thresholding_asks_for_z_with_x_and_y(tmp_path, client):
    """No Z column was found: on a z-stack that is a question, asked where X
    and Y are, and it blocks the tool like they do."""
    name = _stack_with_csv(tmp_path, z=False)
    keys = _keys(client, name)
    assert "role:z" in keys["missing"], keys
    needs = client.get(f"/{name}/tools/gating/requirements").get_json()
    label = next(r["label"] for r in needs["missing"] if r["key"] == "role:z")
    assert label == "Z coordinate column"


def test_a_flat_image_is_never_asked_for_z(tmp_path, client):
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    keys = _keys(client, "synth")
    assert not any("role:z" in found for found in keys.values()), keys


def test_a_guessed_z_column_is_shown_once_for_confirmation(tmp_path, client):
    name = _stack_with_csv(tmp_path)
    keys = _keys(client, name)
    assert "role:z" not in keys["missing"]
    assert "role:z" in keys["confirm"], keys
    client.post(f"/{name}/requirements", json={"tool": "gating", "confirm": ["role:z"]})
    assert "role:z" not in _keys(client, name)["confirm"]


def test_saying_the_cells_have_no_z_answers_the_question(tmp_path, client):
    """A 2-D table over a stack has no Z to give; that is an answer, recorded
    as one, or the question would block forever. Naming a column later
    displaces it."""
    from plexora.server.models import manifest
    from plexora.server.models.project import Project

    name = _stack_with_csv(tmp_path, z=False)
    client.post(f"/{name}/requirements", json={"tool": "gating", "flat_cells": True})
    project = Project.load(name)
    assert project.dataset.flat_cells is True and project.roles.z is None
    assert "role:z" not in _keys(client, name)["missing"]
    assert manifest.status(project, "role:z")["value"] == "No Z (2-D cells)"

    client.post(f"/{name}/requirements", json={"roles": {"z": "DNA"}})
    project = Project.load(name)
    assert project.roles.z == "DNA" and project.dataset.flat_cells is False


def test_an_optional_x_and_y_bring_an_optional_z(tmp_path, client):
    """Cell Explorer offers X and Y without demanding them; on a z-stack Z is
    offered beside them on the same terms."""
    name = _stack_with_csv(tmp_path, z=False)
    keys = _keys(client, name, tool="cell_explorer")
    assert "role:z" in keys["optional"] and "role:z" not in keys["missing"], keys


def test_z_is_part_of_the_project_vocabulary():
    import inspect

    from plexora import cli, datasets

    assert "z" in datasets.PROJECT_SPEC_KEYS
    assert "z" in inspect.signature(datasets.create_project).parameters
    args = cli.build_parser("project").parse_args(
        ["create", "stack.ome.tif", "--x", "X", "--y", "Y", "--z", "Z"])
    assert args.z == "Z"
    assert datasets._column_payload({"z": "Z"})["roles"] == {"z": "Z"}


def _probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_the_viewer_draws_the_planes_on_screens_cells_without_blinking():
    report = _probe()
    assert report["filter"] == {"CD8": [1, 2], "Z": [6.5, 7.5]}, report
    assert report["slab_filter"] == {"CD8": [1, 2], "Z": [1.5, 4.5]}, report
    assert report["flat_filter"] == {"CD8": [1, 2]}, report
    # A plane change refetches every visible tile, keeps the old dots until the
    # new ones land, then replaces them.
    assert report["refetched_cached_tile"] is True, report
    assert report["while_loading"] == [1, 2], report
    assert report["after_loading"] == [3], report
    # A reply for planes no longer on screen is dropped.
    assert report["superseded"] == [5], report
    assert report["same_planes_is_free"] is True, report


def test_the_viewer_follows_the_plane_when_the_table_has_a_z_role():
    source = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "main.js").read_text()
    assert "viewState.info?.table_z" in source
    assert "seaDragonViewer.setCentroidPlanes(" in source
