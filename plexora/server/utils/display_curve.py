"""One display curve for every renderer: a raw value to a brightness in [0, 1].

Two kinds, both anchored on a window [black, white] in raw units:

- linear: `t = clip((x - black) / (white - black), 0, 1)`;
- asinh:  `t = clip(asinh((x - black) / c) / asinh((white - black) / c), 0, 1)`,
  with a cofactor `c > 0` in raw units. Near black it is linear with slope
  1/c, far above it logarithmic: a dim cell is drawn visibly above the
  background while a bright one still has room before white. As c grows the
  curve becomes the linear one.

The canvas computes the same numbers (`frag.glsl` `range_clamp`,
`client/src/js/services/displayCurve.js`); every renderer works in its own
normalised units `d = (raw - offset) / scale`, where the cofactor is only
scaled, `c_d = c / scale`. `tests/golden/display_curve_vectors.json` pins the
Python, JavaScript and shader arithmetic to the same values.

A curve is `None` (linear) or `{"kind": "asinh", "cofactor": c}`.
"""

from __future__ import annotations

import math

import numpy as np

KINDS = ("linear", "asinh")


def normalise(curve):
    """The curve as `{"kind": "asinh", "cofactor": c}`, or None for linear (a
    missing curve, kind linear, or a cofactor that is not a positive number)."""
    if not curve:
        return None
    if isinstance(curve, str):
        return None
    kind = curve.get("kind", "asinh")
    try:
        cofactor = float(curve.get("cofactor"))
    except (TypeError, ValueError):
        return None
    if kind != "asinh" or not math.isfinite(cofactor) or cofactor <= 0:
        return None
    return {"kind": "asinh", "cofactor": cofactor}


def transfer(values, window, curve=None):
    """`values` (any array) as float32 brightness in [0, 1]; zeros when the
    window is empty. The linear branch is the arithmetic every renderer used
    before curves existed, operation for operation."""
    low, high = float(window[0]), float(window[1])
    span = high - low
    plane = np.asarray(values).astype(np.float32)
    if span <= 0:
        return np.zeros(plane.shape, dtype=np.float32)
    curve = normalise(curve)
    if curve is None:
        return np.clip((plane - low) / span, 0.0, 1.0)
    c = curve["cofactor"]
    top = math.asinh(span / c)
    return np.clip(np.arcsinh((plane - np.float32(low)) / np.float32(c)) / np.float32(top),
                   0.0, 1.0).astype(np.float32)


def level(value, window, curve=None) -> float:
    """One value's brightness (a float), as `transfer` draws it."""
    low, high = float(window[0]), float(window[1])
    span = high - low
    if span <= 0:
        return 0.0
    curve = normalise(curve)
    if curve is None:
        t = (float(value) - low) / span
    else:
        c = curve["cofactor"]
        t = math.asinh((float(value) - low) / c) / math.asinh(span / c)
    return min(1.0, max(0.0, t))


def inverse(t, window, curve=None) -> float:
    """The raw value drawn at brightness `t` (0..1) under `window` and `curve`."""
    low, high = float(window[0]), float(window[1])
    t = min(1.0, max(0.0, float(t)))
    curve = normalise(curve)
    if curve is None:
        return low + t * (high - low)
    c = curve["cofactor"]
    return low + c * math.sinh(t * math.asinh((high - low) / c))


def white_for(black, value, at, curve=None) -> float:
    """The white point that draws `value` at brightness `at` (0 < at <= 1),
    black fixed, the cofactor kept."""
    black, value, at = float(black), float(value), float(at)
    if not value > black or not 0 < at <= 1:
        return value if value > black else black + 1.0
    curve = normalise(curve)
    if curve is None:
        return black + (value - black) / at
    c = curve["cofactor"]
    return black + c * math.sinh(math.asinh((value - black) / c) / at)


def scaled(curve, factor):
    """The curve for a window `factor` times as wide: the cofactor scales with
    it, so values placed proportionally draw at the same brightness."""
    curve = normalise(curve)
    if curve is None or not factor or factor <= 0:
        return curve
    return {"kind": "asinh", "cofactor": curve["cofactor"] * float(factor)}


def solve(low_gap, high_gap, low_at, high_at):
    """The curve and white point that draw two values at two brightnesses.

    `low_gap`, `high_gap` are the values' distances above black (raw units,
    0 < low_gap < high_gap); `low_at` < `high_at` their brightnesses. Returns
    (curve, white_gap): `curve` None when a linear window already draws the
    low value at least as bright as asked (then `white_gap` puts the high
    value at `high_at` and the low one lands where it does); else the asinh
    cofactor found by bisection on its logarithm (the ratio
    asinh(low/c)/asinh(high/c) rises monotonically from low/high to 1 as c
    falls), and the white point from it."""
    a, b = float(low_gap), float(high_gap)
    ratio = float(low_at) / float(high_at)
    if not (0 < a < b) or not (0 < ratio < 1):
        return None, (b / float(high_at) if b > 0 and high_at > 0 else max(b, 1.0))
    if a / b >= ratio:
        return None, b / float(high_at)
    lo, hi = math.log(b) - 30.0, math.log(b) + 30.0     # small c: ratio -> 1; large: a/b

    def r(log_c):
        c = math.exp(log_c)
        return math.asinh(a / c) / math.asinh(b / c)

    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if r(mid) > ratio:
            lo = mid        # too log-like: raise c
        else:
            hi = mid
    c = math.exp(0.5 * (lo + hi))
    white_gap = c * math.sinh(math.asinh(b / c) / float(high_at))
    return {"kind": "asinh", "cofactor": c}, white_gap
