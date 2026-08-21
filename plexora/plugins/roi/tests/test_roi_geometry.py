"""What the server will and will not store as a shape.

The client validates too, because it has to decide what to do while the user is
still holding the mouse. This is the copy that decides what ends up on disk, and
the two answer to different pressures: the client's job is to be helpful, this
one's is to be strict.

The distinction these tests keep straight is REJECT versus REPAIR. A ring that
is not explicitly closed is normalized, because [A,B,C] and [A,B,C,A] are the
same polygon and storing one spelling means nothing downstream handles two. A
bow-tie is stored exactly as drawn, because "fix" would mean changing which
pixels the annotation covers.
"""

import json

import pytest

from plexora.plugins.roi.server.geometry import (
    MAX_VERTICES,
    geometry_bounds,
    validate_geometry,
    vertex_count,
)


def polygon(*points):
    return {"type": "Polygon", "coordinates": [list(points)]}


# -- normalization ------------------------------------------------------

def test_an_open_ring_is_closed_rather_than_refused():
    result = validate_geometry(polygon([0, 0], [10, 0], [10, 10]))
    assert result["coordinates"][0] == [[0, 0], [10, 0], [10, 10], [0, 0]]


def test_an_already_closed_ring_is_left_alone():
    ring = [[0, 0], [10, 0], [10, 10], [0, 0]]
    assert validate_geometry(polygon(*ring))["coordinates"][0] == ring


def test_integers_become_floats():
    """Coordinates are continuous -- a vertex lands wherever the cursor was, not
    on a pixel boundary -- so the stored type says so and a round trip through
    JSON does not quietly change it."""
    result = validate_geometry(polygon([0, 0], [10, 0], [10, 10]))
    assert all(isinstance(v, float) for point in result["coordinates"][0] for v in point)


def test_unknown_fields_on_a_geometry_are_dropped():
    result = validate_geometry({**polygon([0, 0], [10, 0], [10, 10]), "crs": "EPSG:4326"})
    assert set(result) == {"type", "coordinates"}


# -- rejection ----------------------------------------------------------

def test_a_shape_needs_three_distinct_corners():
    with pytest.raises(ValueError, match="three distinct"):
        validate_geometry(polygon([0, 0], [10, 10]))
    with pytest.raises(ValueError, match="three distinct"):
        validate_geometry(polygon([5, 5], [5, 5], [5, 5], [5, 5]))


def test_collinear_points_are_still_a_shape():
    """Degenerate in area but not in structure, and the user may be mid-way
    through building something. Rejecting on area is the drawing tool's call,
    made against screen size; this layer only knows the shape is well-formed."""
    assert validate_geometry(polygon([0, 0], [5, 5], [10, 10]))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_coordinates_are_refused(bad):
    """json.loads accepts NaN and Infinity by default, so a payload really can
    carry them -- and stored, they come back out as JSON no other tool can
    parse."""
    with pytest.raises(ValueError, match="finite"):
        validate_geometry(polygon([0, 0], [10, 0], [bad, 10]))


def test_nan_survives_a_json_round_trip_and_is_still_caught():
    """The reason the check is on the value rather than on the request body."""
    body = json.loads('{"type":"Polygon","coordinates":[[[0,0],[1,0],[NaN,1]]]}')
    with pytest.raises(ValueError, match="finite"):
        validate_geometry(body)


@pytest.mark.parametrize("bad", ["1", True, None, {"x": 1}])
def test_a_coordinate_must_be_a_number(bad):
    with pytest.raises(ValueError, match="not a number|finite"):
        validate_geometry(polygon([0, 0], [10, 0], [bad, 10]))


@pytest.mark.parametrize("kind", ["Point", "LineString", "GeometryCollection", None, ""])
def test_only_polygons_are_stored(kind):
    with pytest.raises(ValueError, match="unsupported geometry type"):
        validate_geometry({"type": kind, "coordinates": []})


@pytest.mark.parametrize("bad", [None, [], "polygon", 3])
def test_malformed_structures_are_refused(bad):
    with pytest.raises(ValueError):
        validate_geometry({"type": "Polygon", "coordinates": bad})


def test_a_geometry_may_not_be_unboundedly_large():
    """A ceiling on one feature, counted across all its rings -- a thousand
    rings of a hundred points is the same amount of work as one ring of a
    hundred thousand, and only a per-feature total sees both."""
    huge = [[float(i), float(i % 7)] for i in range(MAX_VERTICES + 2)]
    with pytest.raises(ValueError, match="more than"):
        validate_geometry({"type": "Polygon", "coordinates": [huge]})


# -- shapes the editor cannot author but must not destroy ---------------

def test_holes_are_preserved():
    """v1 cannot draw a hole. That is a limit of the editor, and it is not a
    reason to flatten one that arrives from an import -- the geometry would be
    silently changed to cover pixels its author excluded."""
    with_hole = {"type": "Polygon", "coordinates": [
        [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
        [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
    ]}
    result = validate_geometry(with_hole)
    assert len(result["coordinates"]) == 2
    assert result["coordinates"][1][0] == [40.0, 40.0]


def test_multipolygons_are_preserved():
    multi = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 0]]],
        [[[50, 50], [60, 50], [60, 60], [50, 50]]],
    ]}
    result = validate_geometry(multi)
    assert result["type"] == "MultiPolygon"
    assert len(result["coordinates"]) == 2


def test_a_self_intersecting_shape_is_stored_as_drawn():
    """A bow-tie. Repairing it -- splitting it into two triangles -- produces a
    different annotation from the one the user drew, and doing that invisibly to
    something somebody will publish a measurement from is not a repair. The
    client flags it; nothing here changes it."""
    bowtie = polygon([0, 0], [10, 10], [10, 0], [0, 10])
    assert validate_geometry(bowtie)["coordinates"][0][:4] == [
        [0.0, 0.0], [10.0, 10.0], [10.0, 0.0], [0.0, 10.0]]


def test_shapes_outside_the_image_are_kept():
    """A region continuing past the edge of the tissue is a real thing to have
    recorded. Clipping it here would lose that, and silently."""
    result = validate_geometry(polygon([-50, -50], [10, 0], [10, 10]))
    assert result["coordinates"][0][0] == [-50.0, -50.0]


# -- helpers ------------------------------------------------------------

def test_bounds_and_counts_cover_every_ring():
    multi = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 0]]],
        [[[50, 50], [60, 50], [60, 70], [50, 50]]],
    ]}
    assert geometry_bounds(multi) == (0, 0, 60, 70)
    assert vertex_count(multi) == 8
