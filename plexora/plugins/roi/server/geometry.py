"""What counts as a storable ROI geometry.

Every geometry that reaches the store passes through `validate_geometry`,
whether it came from the drawing tools, an import, or a request somebody wrote
by hand. The client validates too -- it has to, since it decides what to do
about a bad shape while the user is still holding the mouse -- but the client's
answer is advice and this one is the rule.

Two things are deliberately NOT done here:

**No repair.** A self-intersecting polygon is stored as drawn, flagged rather
than corrected. `make_valid` and friends change topology, and a bow-tie silently
becoming two triangles is a different annotation from the one the user drew.
The flag is what surfaces it; the user decides.

**No projection, no scaling, no clamping.** Coordinates are full-resolution
image pixels and are stored exactly as given. A shape that hangs off the edge of
the image is a real thing to have drawn (a region continuing past the tissue
edge), and quietly trimming it would lose that.

The one normalization that does happen is closing rings. GeoJSON requires the
first and last position of a ring to be identical; [A,B,C] and [A,B,C,A] denote
the same polygon to every geometry library there is, so accepting both and
storing one is not a repair -- it just means nothing downstream has to handle
two spellings of the same shape.
"""

from __future__ import annotations

import math

POLYGON = "Polygon"
MULTI_POLYGON = "MultiPolygon"
GEOMETRY_TYPES = (POLYGON, MULTI_POLYGON)

#: Vertices in one feature, across every ring. A hand-drawn region is a few
#: hundred points and a simplified freehand trace a few thousand; an imported
#: tissue contour can legitimately be tens of thousands. Past this, a request is
#: a mistake or an attempt to make the server chew on something -- and a feature
#: that big is unusable in the editor anyway, since it is one undo step and one
#: Path2D rebuild per drag frame.
MAX_VERTICES = 100_000

#: Rings in one polygon (the outer ring plus interior holes), and polygons in
#: one MultiPolygon. Both only ever arrive from an import; the editor authors a
#: single ring.
MAX_RINGS = 1_000
MAX_POLYGONS = 1_000

#: The smallest closed ring that encloses anything: three distinct corners plus
#: the repeated first point.
MIN_RING_POSITIONS = 4


def validate_geometry(geometry, *, max_vertices=MAX_VERTICES):
    """Return a normalized copy of `geometry`, or raise ValueError.

    Normalized means: type is one of GEOMETRY_TYPES, every position is a pair of
    finite floats, and every ring is explicitly closed.
    """
    if not isinstance(geometry, dict):
        raise ValueError("geometry must be an object")

    kind = geometry.get("type")
    if kind not in GEOMETRY_TYPES:
        raise ValueError(
            f"unsupported geometry type {kind!r}: expected one of {list(GEOMETRY_TYPES)}"
        )

    coordinates = geometry.get("coordinates")
    budget = _Budget(max_vertices)

    if kind == POLYGON:
        normalized = _polygon(coordinates, budget)
    else:
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("MultiPolygon coordinates must be a non-empty list of polygons")
        if len(coordinates) > MAX_POLYGONS:
            raise ValueError(f"MultiPolygon has more than {MAX_POLYGONS} polygons")
        normalized = [_polygon(part, budget) for part in coordinates]

    return {"type": kind, "coordinates": normalized}


class _Budget:
    """Vertex allowance for one feature, shared across all its rings.

    Counted for the whole geometry rather than per ring: a thousand rings of a
    hundred points each is the same amount of work as one ring of a hundred
    thousand, and only a per-feature total sees both.
    """

    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def spend(self, count):
        self.used += count
        if self.used > self.limit:
            raise ValueError(f"geometry has more than {self.limit} vertices")


def _polygon(rings, budget):
    if not isinstance(rings, list) or not rings:
        raise ValueError("polygon coordinates must be a non-empty list of rings")
    if len(rings) > MAX_RINGS:
        raise ValueError(f"polygon has more than {MAX_RINGS} rings")
    return [_ring(ring, budget) for ring in rings]


def _ring(ring, budget):
    if not isinstance(ring, list):
        raise ValueError("a ring must be a list of positions")
    budget.spend(len(ring))

    points = [_position(p) for p in ring]
    points = close_ring(points)

    if len(points) < MIN_RING_POSITIONS:
        raise ValueError(
            "a ring needs at least three distinct positions "
            f"(got {len(points) - 1 if points else 0})"
        )
    # Three points that happen to be the same point are four positions once
    # closed, and enclose nothing. Counted on the open ring so the deliberate
    # closing duplicate is not mistaken for a degenerate one.
    if len({(x, y) for x, y in points[:-1]}) < 3:
        raise ValueError("a ring needs at least three distinct positions")
    return points


def _position(position):
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise ValueError("a position must be a [x, y] pair")
    x, y = position[0], position[1]
    # bool is an int; a JSON `true` reaching a coordinate is a malformed payload
    # rather than the number 1.
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"coordinate {value!r} is not a number")
        if not math.isfinite(value):
            # NaN and Infinity survive json.loads by default, so this is the
            # check that keeps them out of the store -- and out of every
            # consumer that would otherwise get them back as invalid JSON.
            raise ValueError("coordinates must be finite numbers")
    return [float(x), float(y)]


def close_ring(points):
    """A ring whose last position repeats its first."""
    if not points:
        return points
    if points[0] != points[-1]:
        return [*points, list(points[0])]
    return points


def geometry_bounds(geometry):
    """(min_x, min_y, max_x, max_y) over every position, or None if empty."""
    xs, ys = [], []
    for ring in iter_rings(geometry):
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def iter_rings(geometry):
    """Every ring in a Polygon or MultiPolygon, outer and interior alike."""
    coordinates = (geometry or {}).get("coordinates") or []
    if (geometry or {}).get("type") == MULTI_POLYGON:
        for polygon in coordinates:
            yield from polygon
    else:
        yield from coordinates


def vertex_count(geometry):
    return sum(len(ring) for ring in iter_rings(geometry))
