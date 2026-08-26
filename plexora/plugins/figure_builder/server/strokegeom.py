"""A stroke's ink: dashes, arrowheads, tapers and fades.

A line is stored as a start point and a SIGNED offset (see
`schema.normalize_annotation`), plus a handful of flat style keys naming what to
draw at each end and how the shaft itself should look. This module turns those
names into geometry -- points, polygons and sub-segments -- so that the browser,
the PDF writer and the raster writer all draw the same arrow.

Nothing here imports reportlab, and nothing here may. PNG and TIFF export must
work in an install that never had it, and a module every renderer calls is the
easiest place to lose that by accident. Nothing here imports `schema` either:
the vocabulary lives there, but every function below falls through to the
"draw nothing special" answer for a name it does not know, which is what lets a
document written by a newer build render instead of raising.

Units are the CALLER's. Everything is proportional: pass millimetres and get
millimetres, pass pixels and get pixels. The one exception is `head_size`, which
is the pt-to-pt rule for how big a head should be before anyone converts it --
one rule in one place, because the canvas sizing a head differently from the
exporter is exactly the bug where an arrow looks right while it is being placed
and wrong in the PDF.

The JS half of this lives in `static/figureStrokeGeometry.js`, and the two are
kept honest by one case table in `tests/js/figure_stroke_probe.mjs` that pytest
re-runs through this module. Every number that exists in both languages belongs
in that table rather than being restated in either test.
"""

from __future__ import annotations

import math

#: Dash patterns, as multiples of the effective stroke width, keyed by
#: `style.line_style`. Scaling with the pen rather than being fixed lengths is
#: what keeps a 4pt dashed rule from looking like a solid one.
#:
#: "dotted" is a zero-length dash: a segment of no length under a round cap is a
#: dot, and it is the only way to get round dots out of a dash array. reportlab
#: accepts it (the pattern's SUM has to be positive, not each entry) and SVG
#: does the same thing with `stroke-linecap="round"`.
DASH_FACTORS = {
    "dashed": (4.0, 2.0),
    "dotted": (0.0, 3.0),
}

#: The thinnest pen a dash pattern is derived from, in points. A hairline would
#: otherwise produce dashes far too fine to read, and -- because reportlab
#: refuses a pattern whose sum is not positive -- a zero-width pen would produce
#: one it refuses outright.
MIN_DASH_WIDTH_PT = 0.75

#: How many constant-alpha pieces a faded solid shaft is cut into. A gradient is
#: native in SVG and does not exist for a PDF or PIL stroke, so both exporters
#: approximate it. Twenty-four is invisible at print resolution and cheap.
FADE_STEPS = 24

#: An upper bound on how many dashes one shaft may be cut into, so a very long
#: line at a very fine pattern cannot turn one annotation into a million draw
#: calls. Far above anything a page can hold: the pattern floor above puts the
#: shortest period at 2.25pt, which is over two thousand dashes per metre.
MAX_DASHES = 10_000

#: The open head's barbs, in degrees off the shaft. Written as the angle from
#: the shaft direction rather than the 160 degrees from the FORWARD direction
#: that `export._arrow_head` used, because the frame below points back down the
#: shaft -- the same two barbs, and the probe pins that they land in the same
#: place as the old formula.
OPEN_HEAD_DEGREES = 20.0

#: Half-width of a closed head, as a fraction of its length.
HEAD_HALF_WIDTH = 0.35

#: How far back along the shaft a closed head reaches before the shaft may
#: stop, as a fraction of head length. Less than 1: the overlap is what hides
#: the seam between a round cap and the flat back edge of a triangle, and
#: without it a fat pen pokes out either side of the head.
HEAD_TRIM = {"filled": 0.85, "diamond": 0.9}

#: Half-width at the narrow end of a taper, as a fraction of the full stroke
#: width. Never zero: a polygon with two coincident points renders as a spike or
#: as nothing at all depending on the rasteriser, and a twentieth of the width
#: is below one device pixel at any resolution a figure is printed at.
TAPER_THIN = 0.05

#: The `style.edge` values this module actually changes anything for. Everything
#: else -- "standard", and any name from a newer build -- draws a plain shaft.
TAPER_EDGES = ("taper_start", "taper_end", "taper_both")
FADE_EDGES = ("fade_start", "fade_end", "fade_both")


def dash_pattern(line_style, width_pt):
    """The dash array for a line style, in the caller's units, or None.

    Derived here from the validated enum and never accepted from a client:
    reportlab raises on a negative entry or a pattern that sums to zero, and an
    exception thrown from inside the PDF writer takes the whole export with it.
    """
    factors = DASH_FACTORS.get(line_style)
    if factors is None:
        return None
    unit = max(float(width_pt), MIN_DASH_WIDTH_PT)
    return [factors[0] * unit, factors[1] * unit]


def head_size(head_size_pt, width_pt):
    """How long a head is, in points, given what the user asked for.

    Zero means "auto", and auto is `max(3, 4 * width)` -- the rule
    `export._arrow_head` used before any of this existed. Every arrow drawn
    before there was a head size stored zero by construction, so keeping that
    branch verbatim is what makes those documents open looking the way they
    looked when they were saved.

    A stored size is otherwise honoured exactly, with one floor: a head shorter
    than the pen drawing it is not a head, it is a blob. That floor is the only
    place head size and stroke width are coupled at all, which is the point --
    the requirement is that the two are independent.
    """
    width = max(float(width_pt), 0.0)
    if float(head_size_pt) <= 0.0:
        return max(3.0, 4.0 * width)
    return max(float(head_size_pt), 1.5 * width)


def head_geometry(head_style, size, width=0.0):
    """One head, in its own frame: tip at the origin, +x back down the shaft.

    Returned as `{"lines", "polygon", "trim", "extent"}`.

    `lines` are stroked at the shaft's own width and `polygon` is filled, which
    is what makes "open" and "bar" read as pen strokes and "filled" and
    "diamond" as solid ink -- the distinction the user is picking between.
    `trim` is how far back the shaft must stop so it does not show through or
    poke past the head, and it is zero for the two open styles because they have
    nothing to hide behind. `extent` is how far the drawn ink reaches from the
    tip, half the pen included, and exists so the canvas can pad the element
    that contains it.

    Tip-local rather than world coordinates so that the whole table is a
    constant: `place_head` is the only thing that needs to know which way the
    line points.
    """
    size = max(float(size), 0.0)
    width = max(float(width), 0.0)
    if head_style == "open":
        angle = math.radians(OPEN_HEAD_DEGREES)
        across = size * math.sin(angle)
        along = size * math.cos(angle)
        lines = [[(0.0, 0.0), (along, across)], [(0.0, 0.0), (along, -across)]]
        polygon = None
        trim = 0.0
    elif head_style == "filled":
        lines = []
        polygon = [(0.0, 0.0), (size, HEAD_HALF_WIDTH * size),
                   (size, -HEAD_HALF_WIDTH * size)]
        trim = HEAD_TRIM["filled"] * size
    elif head_style == "bar":
        lines = [[(0.0, -0.5 * size), (0.0, 0.5 * size)]]
        polygon = None
        trim = 0.0
    elif head_style == "diamond":
        lines = []
        polygon = [(0.0, 0.0), (0.5 * size, HEAD_HALF_WIDTH * size),
                   (size, 0.0), (0.5 * size, -HEAD_HALF_WIDTH * size)]
        trim = HEAD_TRIM["diamond"] * size
    else:
        return {"lines": [], "polygon": None, "trim": 0.0, "extent": 0.0}

    reach = 0.0
    for point in [p for line in lines for p in line] + list(polygon or ()):
        reach = max(reach, math.hypot(point[0], point[1]))
    return {"lines": lines, "polygon": polygon, "trim": trim,
            "extent": reach + (width / 2.0 if lines else 0.0)}


def place_head(tip, other, geom):
    """A head from `head_geometry`, put on the page.

    `tip` is the end it points out of and `other` is the far end of the line,
    which together give the direction; the head's own +x runs from the tip back
    toward `other`, so a head at either end of the same line is the same table
    read through a different frame.
    """
    ux, uy = _direction(tip, other)

    def world(point):
        x, y = point
        return (tip[0] + x * ux - y * uy, tip[1] + x * uy + y * ux)

    return {
        "lines": [[world(a), world(b)] for a, b in geom["lines"]],
        "polygon": [world(p) for p in geom["polygon"]] if geom["polygon"] else None,
    }


def trim_point(p_from, p_toward, distance):
    """`distance` along the line from `p_from` toward `p_toward`."""
    ux, uy = _direction(p_from, p_toward)
    return (p_from[0] + ux * float(distance), p_from[1] + uy * float(distance))


def trimmed_shaft(p1, p2, trim1=0.0, trim2=0.0):
    """Where the shaft starts and stops once both heads have taken their bite.

    Degenerate cases collapse rather than invert. Two heads that between them
    want more room than the line is long would otherwise produce a shaft that
    runs backwards -- which draws, and looks like a line with a bite out of the
    middle of it, at exactly the moment a user is shortening one.
    """
    length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    trim1 = max(float(trim1), 0.0)
    trim2 = max(float(trim2), 0.0)
    if length <= 0.0:
        return (tuple(p1), tuple(p1))
    total = trim1 + trim2
    if total >= length:
        share = length * (trim1 / total) if total > 0.0 else 0.0
        point = trim_point(p1, p2, share)
        return (point, point)
    return (trim_point(p1, p2, trim1), trim_point(p2, p1, trim2))


def taper_outline(p1, p2, width, edge, trim1=0.0, trim2=0.0):
    """A tapered shaft as a closed polygon, or [] if `edge` is not a taper.

    A taper is not a stroke: its width varies along its length, and no renderer
    in this tree has a variable-width pen. So it becomes filled ink, which every
    backend already draws (`compose` emits it as an ordinary `path`
    instruction), and `width` is the FULL width at the fat end rather than a
    half-width, matching what `line_width_pt` means everywhere else.
    """
    if edge not in TAPER_EDGES:
        return []
    a, b = trimmed_shaft(p1, p2, trim1, trim2)
    # The axis comes from the untrimmed line: a shaft trimmed to nothing has no
    # direction of its own, and a taper is still the right shape there.
    ux, uy = _direction(p1, p2)
    nx, ny = -uy, ux
    full = max(float(width), 0.0) / 2.0
    thin = TAPER_THIN * max(float(width), 0.0)

    def side(point, half):
        return (point[0] + nx * half, point[1] + ny * half)

    if edge == "taper_both":
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        return [side(a, thin), side(mid, full), side(b, thin),
                side(b, -thin), side(mid, -full), side(a, -thin)]
    half_a, half_b = (thin, full) if edge == "taper_start" else (full, thin)
    return [side(a, half_a), side(b, half_b), side(b, -half_b), side(a, -half_a)]


def fade_alpha(t, edge):
    """The opacity multiplier a fraction `t` along the shaft, 0 at the start.

    Linear, and over the TRIMMED shaft rather than the whole line: fading over
    the full length would leave a faded stub of shaft sticking out behind a head
    that is drawn at full opacity.
    """
    t = min(1.0, max(0.0, float(t)))
    if edge == "fade_start":
        return t
    if edge == "fade_end":
        return 1.0 - t
    if edge == "fade_both":
        return 1.0 - abs(2.0 * t - 1.0)
    return 1.0


def shaft_render_plan(p1, p2, dash=None, fade=None, steps=FADE_STEPS):
    """The shaft as a list of `(a, b, alpha, is_dot)` pieces to draw.

    What both exporters consume, and what neither of them could do natively: a
    PDF stroke has no gradient and PIL has neither gradients nor dashes. The
    browser ignores this entirely -- it has `stroke-dasharray` and
    `<linearGradient>` and should use them -- and the JS twin exists only so the
    probe can prove the two agree.

    A dashed fade takes each dash's alpha at its own midpoint rather than
    subdividing further: the dashes are already short, and cutting a two-point
    dash into twenty-four pieces is twenty-four draw calls for a difference no
    output device can resolve.
    """
    length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if length <= 0.0:
        return []
    ux, uy = _direction(p1, p2)

    def at(distance):
        return (p1[0] + ux * distance, p1[1] + uy * distance)

    faded = fade in FADE_EDGES
    plan = []
    for start, end, is_dot in _dash_intervals(length, dash):
        if faded and not dash:
            count = max(1, int(steps))
            span = (end - start) / count
            for index in range(count):
                a = start + span * index
                b = a + span
                plan.append((at(a), at(b), fade_alpha((a + b) / 2.0 / length, fade), False))
            continue
        alpha = fade_alpha((start + end) / 2.0 / length, fade) if faded else 1.0
        plan.append((at(start), at(end), alpha, is_dot))
    return plan


def _dash_intervals(length, dash):
    """`(start, end, is_dot)` for every piece of ink a dash array puts down."""
    if not dash:
        return [(0.0, length, False)]
    on, off = float(dash[0]), float(dash[1])
    if on + off <= 0.0:
        return [(0.0, length, False)]
    is_dot = on <= 0.0
    out = []
    position = 0.0
    while position <= length and len(out) < MAX_DASHES:
        end = min(position + on, length)
        # A zero-length piece is ink only when it is meant to be -- a dot. The
        # last dash of a dashed line can land exactly on the end, and drawing it
        # would put a round blob past the final gap.
        if is_dot or end > position:
            out.append((position, end, is_dot))
        position += on + off
    return out


def _direction(a, b):
    """The unit vector from `a` to `b`; +x when there is no distance between
    them, so that a line of no length still has a head to point somewhere."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return (1.0, 0.0)
    return (dx / length, dy / length)
