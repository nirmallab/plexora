"""Regions with a shape through a z-stack: contours and ellipsoids.

A region keeps one 2-D `geometry` -- its footprint, what it covers seen from
above -- so everything that reads one outline per region, and main's reader
(which drops the unknown field), keeps working. `solid` says what it is on
each plane: one outline per plane (contours, usually carried through the planes
by magic select), or an ellipsoid tapering from the footprint on its middle
plane. Its planes (`extent`) always follow from the solid. Cells are counted by
their own plane's outline when the table says where they are in z.
"""

import math

import numpy as np
import pytest

from plexora.plugins.roi.server import geojson, mapping, operations, schema

pytest.importorskip("shapely")


def _square(x0, y0, side):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + side, y0],
                                                [x0 + side, y0 + side], [x0, y0 + side],
                                                [x0, y0]]]}


FOOTPRINT = _square(0, 0, 64)
CONTOURS = {"kind": "contours", "keys": [3],
            "planes": {"2": _square(16, 16, 32), "3": _square(0, 0, 64), "4": _square(24, 24, 16)}}


def _feature(**extra):
    return {"id": "r1", "category_id": "c1", "geometry": FOOTPRINT, **extra}


def _state():
    state = schema.default_state(128, 128)
    state["categories"] = [schema.normalize_category(
        {"id": "c1", "label": "Nucleus", "color": "#e04c4c"})]
    return state


def _features(state):
    return state["images"][schema.DEFAULT_IMAGE]["features"]


# -- the field -------------------------------------------------------------------


def test_contours_set_the_planes_the_region_covers():
    feature = schema.normalize_feature(_feature(solid=CONTOURS, extent={"kind": "all"}))
    assert feature["solid"]["kind"] == "contours"
    assert sorted(feature["solid"]["planes"]) == ["2", "3", "4"]
    assert feature["solid"]["keys"] == [3]
    assert feature["extent"] == {"kind": "slab", "z0": 2, "z1": 4, "t": 0}


def test_an_ellipsoid_covers_the_planes_strictly_inside_it():
    feature = schema.normalize_feature(_feature(solid={"kind": "ellipsoid", "z": 10, "rz": 3}))
    # |p - 10| < 3: planes 8..12.
    assert feature["extent"] == {"kind": "slab", "z0": 8, "z1": 12, "t": 0}
    assert schema.solid_scale(feature["solid"], 10) == 1.0
    assert schema.solid_scale(feature["solid"], 12) == pytest.approx(math.sqrt(1 - 4 / 9))
    assert schema.solid_scale(feature["solid"], 13) == 0.0
    # A projection's range z0..z1 made an ellipsoid: exactly those planes.
    made = schema.normalize_feature(_feature(solid={"kind": "ellipsoid", "z": 6.5, "rz": 2.5}))
    assert made["extent"] == {"kind": "slab", "z0": 5, "z1": 8, "t": 0}


@pytest.mark.parametrize("raw", [
    {"kind": "cylinder"},
    {"kind": "contours", "planes": {}},
    {"kind": "contours", "planes": {"2": FOOTPRINT, "4": FOOTPRINT}},     # a gap
    {"kind": "contours", "planes": {"two": FOOTPRINT}},
    {"kind": "contours", "planes": {"2": {"type": "Point", "coordinates": [1, 2]}}},
    {"kind": "ellipsoid", "z": 3},
    {"kind": "ellipsoid", "z": 3, "rz": 0},
    "solid",
])
def test_a_bad_solid_is_refused(raw):
    with pytest.raises(ValueError):
        schema.normalize_solid(raw)


def test_a_flat_region_has_no_solid():
    assert "solid" not in schema.normalize_feature(_feature())


# -- operations ---------------------------------------------------------------------


def test_set_solid_is_one_step_and_its_undo_puts_back_the_flat_region():
    state = operations.apply_operations(_state(), [{"op": "roi.create", "feature": _feature(
        extent={"kind": "plane", "z": 3})}])
    state = operations.apply_operations(state, [{
        "op": "roi.set_solid", "id": "r1", "solid": CONTOURS, "geometry": FOOTPRINT}])
    feature = _features(state)[0]
    assert feature["solid"]["kind"] == "contours"
    assert feature["extent"] == {"kind": "slab", "z0": 2, "z1": 4, "t": 0}
    # The undo the client sends: flat again, on its old plane.
    state = operations.apply_operations(state, [{
        "op": "roi.set_solid", "id": "r1", "solid": None, "geometry": FOOTPRINT,
        "extent": {"kind": "plane", "z": 3}}])
    feature = _features(state)[0]
    assert "solid" not in feature
    assert feature["extent"] == {"kind": "plane", "z": 3, "t": 0}


def test_a_locked_region_cannot_be_given_a_solid():
    state = operations.apply_operations(_state(), [{"op": "roi.create", "feature": _feature(
        locked=True)}])
    with pytest.raises(ValueError, match="locked"):
        operations.apply_operations(state, [{"op": "roi.set_solid", "id": "r1",
                                             "solid": CONTOURS, "geometry": FOOTPRINT}])


def test_contours_cannot_be_reshaped_through_their_footprint():
    state = operations.apply_operations(_state(), [{"op": "roi.create", "feature": _feature(
        solid=CONTOURS)}])
    with pytest.raises(ValueError, match="every plane"):
        operations.apply_operations(state, [{"op": "roi.update_geometry", "id": "r1",
                                             "geometry": _square(4, 4, 30)}])
    # An ellipsoid's footprint IS its shape: reshaping it is fine.
    state = operations.apply_operations(_state(), [{"op": "roi.create", "feature": _feature(
        solid={"kind": "ellipsoid", "z": 5, "rz": 2})}])
    state = operations.apply_operations(state, [{"op": "roi.update_geometry", "id": "r1",
                                                 "geometry": _square(4, 4, 30)}])
    assert _features(state)[0]["solid"]["kind"] == "ellipsoid"


# -- which cells -----------------------------------------------------------------------


def test_cells_are_held_by_their_own_planes_outline():
    feature = schema.normalize_feature(_feature(solid=CONTOURS))
    category = schema.normalize_category({"id": "c1", "label": "Nucleus", "color": "#e04c4c"})
    xs = [8, 8, 32, 32, 32, 32]
    ys = [8, 8, 32, 32, 32, 32]
    zs = [3, 2, 2, 4, 5, 3.4]
    labels, _names = mapping.assign([feature], [category], xs, ys, zs)
    # (8, 8) is inside plane 3's outline only; (32, 32) is inside planes 2-4,
    # not on plane 5, and z 3.4 is plane 3.
    assert labels == ["Nucleus", mapping.UNASSIGNED, "Nucleus", "Nucleus",
                      mapping.UNASSIGNED, "Nucleus"]
    # Without a z column, a solid region is its footprint.
    labels, _ = mapping.assign([feature], [category], xs, ys)
    assert labels == ["Nucleus"] * 6


def test_an_ellipsoid_shrinks_towards_its_poles():
    feature = schema.normalize_feature(_feature(
        geometry=_square(0, 0, 100), solid={"kind": "ellipsoid", "z": 10, "rz": 4}))
    category = schema.normalize_category({"id": "c1", "label": "Nucleus", "color": "#e04c4c"})
    # On plane 13 the square is scaled by sqrt(1 - 9/16) ~ 0.66 about (50, 50):
    # it spans about 17..83.
    labels, _ = mapping.assign([feature], [category], [10, 30, 30, 50], [50, 50, 50, 50],
                               [10, 13, 10, 14])
    assert labels == ["Nucleus", "Nucleus", "Nucleus", mapping.UNASSIGNED]
    labels, _ = mapping.assign([feature], [category], [10], [50], [13])
    assert labels == [mapping.UNASSIGNED]


def test_a_solid_survives_a_geojson_round_trip():
    state = operations.apply_operations(_state(), [{"op": "roi.create", "feature": _feature(
        solid=CONTOURS)}])
    document = geojson.export_document(state, "x", "v")
    exported = document["features"][0]["properties"]
    assert exported["solid"]["kind"] == "contours"
    op, _summary = geojson.import_features(_state(), document)
    imported = schema.normalize_feature(op["features"][0])
    assert imported["solid"]["planes"].keys() == CONTOURS["planes"].keys()
    assert imported["extent"]["z0"] == 2
