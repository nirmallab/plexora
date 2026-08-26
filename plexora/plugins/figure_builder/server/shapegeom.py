"""A shape's path, in the two forms the renderers need.

A shape is stored as nodes in its own box (see `schema.normalize_shape`). This
turns that into absolute millimetres on the page for the PDF writer, which has
curve primitives, and into a polyline for the raster writer, which does not.

Nothing here imports reportlab, and nothing here may. PNG and TIFF export must
work in an install that never had it, and a module both paths call is the
easiest place to lose that by accident.

The JS half of this lives in `static/figureShapeGeometry.js`, and the two are
kept honest by one case table in `tests/js/figure_shape_probe.mjs` that pytest
re-runs through this module. Every number that exists in both languages belongs
in that table rather than being restated in either test.
"""

from __future__ import annotations

import math

#: The circle constant for cubic beziers: a quarter arc of radius 1 is drawn by
#: handles this long. Every rounded preset (ellipse, capsule, rounded rect) is
#: built from it, which is why an ellipse is four nodes and not forty.
KAPPA = 0.5522847498307936

#: How far a flattening subdivision may recurse. A cubic halves its flatness
#: error four times per level, so this is a backstop against a degenerate curve
#: (coincident control points, cusps) rather than a working limit.
MAX_FLATTEN_DEPTH = 16


def segments_mm(shape, geometry):
    """The path in absolute page millimetres.

    Every instruction `compose` emits is absolute mm from the page's top-left,
    and a path is no different -- the backends turn the page under a rotated
    shape and never touch its coordinates, so these pass through all three
    renderers untouched.
    """
    nodes = shape.get("nodes") or []
    if len(nodes) < 2:
        return []
    closed = bool(shape.get("closed"))
    x_mm = geometry["x_mm"]
    y_mm = geometry["y_mm"]
    w_mm = geometry["w_mm"]
    h_mm = geometry["h_mm"]

    def place(point):
        return (x_mm + point["x"] * w_mm, y_mm + point["y"] * h_mm)

    out = [("move",) + place(nodes[0])]
    for start, end in _edges(nodes, closed):
        handle_out = start.get("out")
        handle_in = end.get("in")
        if handle_out is None and handle_in is None:
            out.append(("line",) + place(end))
            continue
        # A missing handle degenerates to its own anchor, which is exactly the
        # cubic that draws the straight half of a half-curved segment. One
        # branch fewer than treating it as its own case, and the same curve.
        out.append(("curve",) + place(handle_out or start)
                   + place(handle_in or end) + place(end))
    if closed:
        out.append(("close",))
    return out


def flatten(segments, tolerance_mm=0.1):
    """The path as a polyline, within `tolerance_mm` of the curve.

    Adaptive rather than a fixed step count: a fixed count spends the same
    points on a straight edge as on a tight corner, so it is either coarse
    where it shows or enormous where it does not. The tolerance is a distance
    in the same millimetres everything else is in, so a caller that knows its
    output resolution can ask for a quarter of a device pixel and get it.
    """
    points = []
    current = origin = (0.0, 0.0)
    for segment in segments:
        kind = segment[0]
        if kind == "move":
            current = origin = (segment[1], segment[2])
            points.append(current)
        elif kind == "line":
            current = (segment[1], segment[2])
            points.append(current)
        elif kind == "curve":
            end = (segment[5], segment[6])
            _flatten_cubic(points, current, (segment[1], segment[2]),
                           (segment[3], segment[4]), end, tolerance_mm, 0)
            current = end
        elif kind == "close":
            if points and points[-1] != origin:
                points.append(origin)
            current = origin
    return points


def ink_bounds(nodes, closed):
    """The tight box the path's ink occupies, in node space: (x, y, w, h).

    Exact rather than sampled. A cubic's extreme is where its derivative
    crosses zero and there are at most two of those per axis, so the answer is
    arithmetic. Sampling instead leaves the box a hair inside the ink, which is
    invisible on screen and then clips the stroke in the PDF -- and because the
    box is what all three renderers rotate ABOUT, a box that is off by a hair
    moves a rotated shape rather than merely cropping it.
    """
    if not nodes:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [nodes[0]["x"]]
    ys = [nodes[0]["y"]]
    for start, end in _edges(nodes, closed):
        xs.append(end["x"])
        ys.append(end["y"])
        handle_out = start.get("out")
        handle_in = end.get("in")
        if handle_out is None and handle_in is None:
            continue
        control_1 = handle_out or start
        control_2 = handle_in or end
        xs.extend(_cubic_extrema(start["x"], control_1["x"], control_2["x"], end["x"]))
        ys.extend(_cubic_extrema(start["y"], control_1["y"], control_2["y"], end["y"]))
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _edges(nodes, closed):
    """Every (start, end) pair the path draws, closing edge included."""
    if len(nodes) < 2:
        return
    for index in range(len(nodes) if closed else len(nodes) - 1):
        yield nodes[index], nodes[(index + 1) % len(nodes)]


def _flatten_cubic(out, p0, p1, p2, p3, tolerance, depth):
    if depth >= MAX_FLATTEN_DEPTH or _flat_enough(p0, p1, p2, p3, tolerance):
        out.append(p3)
        return
    # De Casteljau at t = 0.5, which is the whole subdivision: six midpoints and
    # the two halves are exact cubics of their own.
    p01, p12, p23 = _mid(p0, p1), _mid(p1, p2), _mid(p2, p3)
    p012, p123 = _mid(p01, p12), _mid(p12, p23)
    middle = _mid(p012, p123)
    _flatten_cubic(out, p0, p01, p012, middle, tolerance, depth + 1)
    _flatten_cubic(out, middle, p123, p23, p3, tolerance, depth + 1)


def _flat_enough(p0, p1, p2, p3, tolerance):
    """Whether the chord stands in for the curve within `tolerance`.

    The usual bound on how far a cubic can stray from the line between its
    endpoints, measured from the control points rather than from the curve --
    conservative, cheap, and with no square roots, which matters because it
    runs on every subdivision of every segment of every shape on the page.
    """
    ux = (3.0 * p1[0] - 2.0 * p0[0] - p3[0]) ** 2
    uy = (3.0 * p1[1] - 2.0 * p0[1] - p3[1]) ** 2
    vx = (3.0 * p2[0] - 2.0 * p3[0] - p0[0]) ** 2
    vy = (3.0 * p2[1] - 2.0 * p3[1] - p0[1]) ** 2
    return max(ux, vx) + max(uy, vy) <= 16.0 * tolerance * tolerance


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _cubic_extrema(p0, p1, p2, p3):
    """The curve's value at each interior turning point, on one axis."""
    a = -p0 + 3.0 * p1 - 3.0 * p2 + p3
    b = 2.0 * (p0 - 2.0 * p1 + p2)
    c = p1 - p0
    out = []
    for t in _quadratic_roots(a, b, c):
        if 0.0 < t < 1.0:
            s = 1.0 - t
            out.append(s * s * s * p0 + 3.0 * s * s * t * p1
                       + 3.0 * s * t * t * p2 + t * t * t * p3)
    return out


def _quadratic_roots(a, b, c):
    if abs(a) < 1e-12:
        return () if abs(b) < 1e-12 else (-c / b,)
    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return ()
    root = math.sqrt(discriminant)
    return ((-b + root) / (2.0 * a), (-b - root) / (2.0 * a))
