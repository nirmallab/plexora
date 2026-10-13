"""Regions that ARE cells of a 3-D mask, and outlines edited one plane at a time.

What has to hold: a region made of mask cells is outlined plane by plane from
the mask and holds exactly those cells (by id, not by outline); its planes may
have gaps where none of the cells lies; a plane's outline replaced by hand
re-derives the footprint on the server; and the agent can make one by naming
the cells.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.agent import AgentSession, registry
from plexora.api.dataset import project_data
from plexora.datasource import register_image_datasource
from plexora.plugins.roi.server import mapping, schema, service
from plexora.plugins.roi.server.repository import ROIRepository
from plexora.server.models.project import Project
from tests import volume_fixtures as vf

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Paid test licence.
pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _core():
    registry.discover(["roi"])


@pytest.fixture
def client():
    return plexora.app.test_client()


def _cells_project(tmp_path, name="cells", measure=True):
    from plexora.server.volume import objects

    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=1,
                                  depth=8, height=64, width=64, names=["DNA"], physical_z=2.0)
    labels = np.zeros((8, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5          # planes 1-3
    labels[6:8, 30:40, 30:46] = 9          # planes 6-7: a gap at 4-5 between them
    labels[2:5, 44:54, 4:12] = 3
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    if measure:
        objects.measure_project_objects(Project.find(name), units="physical")
    return labels


def test_a_region_of_cells_is_outlined_from_the_mask_and_holds_exactly_them(tmp_path, client):
    _cells_project(tmp_path)
    made = client.post("/plugins/roi/api/from_cells",
                       json={"datasource": "cells", "ids": [5, 9]})
    assert made.status_code == 200, made.get_data(as_text=True)
    roi = made.get_json()["roi"]
    _, feature = schema.find(ROIRepository("cells").load()["images"]["default"]["features"],
                             roi["id"])
    solid = feature["solid"]
    assert solid["kind"] == "labels" and solid["ids"] == [5, 9]
    assert sorted(int(z) for z in solid["planes"]) == [1, 2, 3, 6, 7]   # the gap kept
    assert feature["extent"] == {"kind": "slab", "z0": 1, "z1": 7, "t": 0}
    counted = service.cells_in_roi(project_data("cells"), roi["id"])
    assert sorted(counted["cell_ids"]) == [5, 9] and counted["n_cells"] == 2


def test_mapping_assigns_a_region_of_cells_by_id_not_by_outline():
    square = {"type": "Polygon", "coordinates": [[[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]]]}
    feature = {"id": "r", "name": "Two cells", "category_id": "c", "geometry": square,
               "solid": schema.normalize_solid({"kind": "labels", "ids": [7, 11],
                                                "planes": {"0": square}})}
    xs, ys = np.array([1.0, 100.0, 1.0]), np.array([1.0, 100.0, 1.0])
    _, names = mapping.assign([feature], [{"id": "c", "label": "Cells"}], xs, ys,
                              zs=np.zeros(3), cell_ids=np.array([7, 11, 2]))
    # Cell 11 lies outside the outline and is in the region; cell 2 inside it is not.
    assert names == ["Two cells", "Two cells", mapping.UNASSIGNED]


def test_one_planes_outline_edited_by_hand_re_derives_the_footprint(tmp_path):
    _cells_project(tmp_path, "edit", measure=False)
    base, _, roi = service.create_roi(
        project_data("edit"), category="Cells", name="carried",
        geometry={"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20],
                                                      [10, 20], [10, 10]]]},
        solid={"kind": "contours", "planes": {
            str(z): {"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20],
                                                         [10, 20], [10, 10]]]}
            for z in (1, 2, 3)}})
    repo = ROIRepository("edit")
    state = repo.load()
    _, feature = schema.find(state["images"]["default"]["features"], roi["id"])
    planes = dict(feature["solid"]["planes"])
    planes["2"] = {"type": "Polygon", "coordinates": [[[10, 10], [40, 10], [40, 20],
                                                       [10, 20], [10, 10]]]}
    repo.apply(state["revision"], [{"op": "roi.set_solid", "image": "default", "id": roi["id"],
                                    "solid": {**feature["solid"], "planes": planes, "keys": [2]}}])
    _, after = schema.find(repo.load()["images"]["default"]["features"], roi["id"])
    xs = [x for ring in after["geometry"]["coordinates"] for x, _ in ring]
    assert max(xs) == pytest.approx(40, abs=0.5), "the footprint reaches the edited plane"
    assert after["solid"]["keys"] == [2]


def test_the_agent_makes_a_region_of_cells_by_naming_them(tmp_path):
    _cells_project(tmp_path, "agentcells")
    answer = registry.invoke(AgentSession(), "create_roi", {
        "project": "agentcells", "category": "Cells", "cells": [3], "name": "one cell"})
    assert answer["ok"], answer
    roi = answer["result"]
    summary = roi.get("roi", roi)
    solid = summary.get("solid") or {}
    assert solid.get("kind") == "labels" and solid.get("cells") == 1, summary
    refused = registry.invoke(AgentSession(), "create_roi", {
        "project": "agentcells", "category": "Cells", "cells": [12345]})
    assert not refused["ok"] and "none of those cells" in refused["error"]["message"]
