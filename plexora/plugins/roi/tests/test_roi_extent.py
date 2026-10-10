"""Regions on a z-stack: which planes one covers, and which cells that holds.

Every region drawn before z-stacks existed, and every region on a flat image,
covers every plane -- and must keep reading exactly as it did, so "all" is
stored as no extent at all. A region limited to one plane or a range holds
only the cells whose nearest plane is in it, when the table says where each
cell is in z; when it does not, the count says so rather than pretending.
"""

import json

import numpy as np
import pytest

from plexora.plugins.roi.server import mapping, operations, schema

pytest.importorskip("shapely")

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [64, 0], [64, 64], [0, 64], [0, 0]]]}


def _feature(**extra):
    return {"id": "r1", "category_id": "c1", "geometry": SQUARE, **extra}


# -- the field ---------------------------------------------------------------


@pytest.mark.parametrize("raw, stored", [
    (None, None),
    ({"kind": "all"}, None),
    ({"kind": "plane", "z": 3}, {"kind": "plane", "z": 3, "t": 0}),
    ({"kind": "slab", "z0": 9, "z1": 4, "t": 2}, {"kind": "slab", "z0": 4, "z1": 9, "t": 2}),
    ({"kind": "plane", "z": 3.0}, {"kind": "plane", "z": 3, "t": 0}),
])
def test_an_extent_is_normalised(raw, stored):
    assert schema.normalize_extent(raw) == stored


@pytest.mark.parametrize("raw", [
    {"kind": "cube"}, {"kind": "plane"}, {"kind": "plane", "z": -1},
    {"kind": "plane", "z": 1.5}, {"kind": "slab", "z0": 1}, "z3", {"kind": "plane", "z": True},
])
def test_a_bad_extent_is_refused(raw):
    with pytest.raises(ValueError):
        schema.normalize_extent(raw)


def test_a_flat_region_stores_no_extent():
    assert "extent" not in schema.normalize_feature(_feature())
    assert "extent" not in schema.normalize_feature(_feature(extent={"kind": "all"}))
    assert schema.normalize_feature(_feature(extent={"kind": "plane", "z": 2}))["extent"] == {
        "kind": "plane", "z": 2, "t": 0}


def _state():
    state = schema.default_state(128, 128)
    state["categories"] = [schema.normalize_category(
        {"id": "c1", "label": "Tumor", "color": "#e04c4c"})]
    return state


def _apply(state, ops):
    return operations.apply_operations(state, ops)


def test_planes_are_set_changed_and_cleared_by_operations():
    state = _apply(_state(), [{"op": "roi.create", "feature": _feature(
        extent={"kind": "plane", "z": 2})}])
    feature = state["images"][schema.DEFAULT_IMAGE]["features"][0]
    assert feature["extent"]["z"] == 2
    state = _apply(state, [{"op": "roi.update_properties", "id": "r1",
                            "changes": {"extent": {"kind": "slab", "z0": 1, "z1": 3}}}])
    feature = state["images"][schema.DEFAULT_IMAGE]["features"][0]
    assert feature["extent"] == {"kind": "slab", "z0": 1, "z1": 3, "t": 0}
    state = _apply(state, [{"op": "roi.update_properties", "id": "r1",
                            "changes": {"extent": {"kind": "all"}}}])
    assert "extent" not in state["images"][schema.DEFAULT_IMAGE]["features"][0]


def test_a_locked_region_keeps_its_planes():
    state = _apply(_state(), [{"op": "roi.create", "feature": _feature(locked=True)}])
    with pytest.raises(ValueError, match="locked"):
        _apply(state, [{"op": "roi.update_properties", "id": "r1",
                        "changes": {"extent": {"kind": "plane", "z": 0}}}])


# -- which cells ---------------------------------------------------------------


def test_cells_are_held_only_on_the_regions_planes():
    features = [
        {"id": "a", "category_id": "c1", "name": "one plane", "geometry": SQUARE,
         "extent": {"kind": "plane", "z": 2, "t": 0}},
        {"id": "b", "category_id": "c1", "name": "a slab", "geometry": SQUARE,
         "extent": {"kind": "slab", "z0": 4, "z1": 6, "t": 0}},
        {"id": "c", "category_id": "c1", "name": "everywhere", "geometry": SQUARE},
    ]
    categories = [{"id": "c1", "label": "Tumor"}]
    xs = [10, 10, 10, 10, 10, 200]
    ys = [10, 10, 10, 10, 10, 200]
    zs = [2.0, 2.4, 5.6, 9.0, np.nan, 2.0]
    _, names = mapping.assign(features, categories, xs, ys, zs)
    both = mapping.JOINER.join
    assert names[0] == both(["one plane", "everywhere"])  # plane 2
    assert names[1] == both(["one plane", "everywhere"])  # nearest plane 2
    assert names[2] == both(["a slab", "everywhere"])     # nearest plane 6
    assert names[3] == "everywhere"                       # plane 9: neither limited one
    assert names[4] == "everywhere"                       # no z: only the unlimited one
    assert names[5] == mapping.UNASSIGNED                 # outside in x/y


def test_without_a_z_column_every_region_covers_every_plane():
    features = [{"id": "a", "category_id": "c1", "name": "one plane", "geometry": SQUARE,
                 "extent": {"kind": "plane", "z": 2, "t": 0}}]
    _, names = mapping.assign(features, [{"id": "c1", "label": "Tumor"}], [10], [10])
    assert names == ["one plane"]


# -- end to end on a 3-D object table --------------------------------------------


@pytest.fixture()
def objects_project(tmp_path):
    """Two 3-D objects one above the other: label 5 centred on plane 2, label 9
    on plane 4, both under one square."""
    import tifffile

    from plexora import datasets
    from plexora.agent import AgentSession, invoke, registry
    from plexora.datasource import register_image_datasource
    from tests import volume_fixtures as vf

    registry.discover(["roi"])
    path, _ = vf.write_ome_volume(tmp_path / "obj.ome.tif", axes="CZYX", channels=1,
                                  depth=6, height=64, width=64, names=["DNA"])
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    labels[3:6, 12:22, 12:22] = 9
    tifffile.imwrite(tmp_path / "mask.tif", labels)
    register_image_datasource("obj", path)
    datasets.add_layers("obj", tmp_path / "mask.tif", wait=True)
    session = AgentSession()
    started = invoke(session, "measure_objects", {"project": "obj", "units": "voxel"})
    assert started["ok"], started
    done = invoke(session, "job_wait", {"job_id": started["result"]["job_id"],
                                        "timeout_s": 60})
    assert done["result"]["job"]["status"] == "done", done
    return session


def test_counting_a_region_follows_its_planes(objects_project):
    from plexora.agent import invoke

    session = objects_project
    created = invoke(session, "create_roi", {
        "project": "obj", "category": "Tumor", "points": [[0, 0], [40, 0], [40, 40], [0, 40]],
        "extent": {"kind": "plane", "z": 2}})
    assert created["ok"], created
    roi = created["result"]["roi"]
    assert roi["extent"] == {"kind": "plane", "z": 2, "t": 0}
    counted = invoke(session, "count_cells_in_roi", {"project": "obj", "roi_id": roi["id"]})
    assert counted["ok"], counted
    result = counted["result"]
    assert sorted(result["cell_ids"]) == [5] and "z_ignored" not in result

    updated = invoke(session, "update_roi", {"project": "obj", "roi_id": roi["id"],
                                             "extent": {"kind": "all"}})
    assert updated["ok"], updated
    hint = updated["result"]["receipt"]["undo_hint"]["arguments"]
    assert hint["extent"] == {"kind": "plane", "z": 2, "t": 0}
    counted = invoke(session, "count_cells_in_roi", {"project": "obj", "roi_id": roi["id"]})
    assert sorted(counted["result"]["cell_ids"]) == [5, 9]


def test_geojson_carries_the_planes(tmp_path):
    from plexora.plugins.roi.server import geojson

    state = _apply(_state(), [{"op": "roi.create", "feature": _feature(
        extent={"kind": "slab", "z0": 1, "z1": 3})}])
    document = geojson.export_document(state, "x", "v")
    properties = document["features"][0]["properties"]
    assert properties["extent"] == {"kind": "slab", "z0": 1, "z1": 3, "t": 0}
    op, _ = geojson.import_features(_state(), json.loads(json.dumps(document)))
    assert op["features"][0]["extent"] == {"kind": "slab", "z0": 1, "z1": 3, "t": 0}
    flat = _apply(_state(), [{"op": "roi.create", "feature": _feature()}])
    assert "extent" not in geojson.export_document(flat, "x", "v")["features"][0]["properties"]
