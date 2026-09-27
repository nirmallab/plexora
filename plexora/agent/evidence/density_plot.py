"""A two-marker density, drawn only when the numbers say it is worth a look.

The bivariate evidence (`gating.autogate.bivariate`) already carries a 64x64
log-density grid, so drawing needs no second pass over the table: the grid is
upscaled, coloured, and the two gates and the four quadrant counts are drawn
over it. Pillow only, like every plot the agent layer makes.
"""

from __future__ import annotations

import base64
import math

import numpy as np

BG = (18, 20, 24)
AXIS = (150, 156, 166)
GATE = (255, 214, 10)
EXTRA_GATE = (120, 220, 255)
TEXT = (235, 235, 235)


def _font(size):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def _colour(grid):
    """A dark-to-bright ramp (blue -> magenta -> yellow) over uint8 values."""
    t = grid.astype(np.float32) / 255.0
    r = np.clip(1.6 * t, 0, 1)
    g = np.clip(2.0 * t - 1.0, 0, 1)
    b = np.clip(0.9 - 0.9 * np.abs(t - 0.35) * 2, 0, 1)
    rgb = np.stack([r, g, b], axis=-1) * 255
    rgb[grid == 0] = BG
    return rgb.astype(np.uint8)


def _axis(space):
    """(forward, label) for one axis: a log1p axis is labelled in raw units,
    a table's own axis in its own units."""
    if space == "log1p":
        return (lambda v: math.log1p(max(v, 0.0))), (lambda t: f"{math.expm1(t):.3g}")
    return (lambda v: float(v)), (lambda t: f"{t:.3g}")


def draw_density(result, *, size=512, log_axes=None, compact=False, extra_a_gates=()):
    """An RGB PIL image for one `bivariate_numbers` result with a density grid.

    Each axis is drawn in the space its grid was binned in (`density["spaces"]`;
    `log_axes` only for a grid that does not say), over the body of each
    column, with three labelled ticks. `compact` (a small panel of a larger
    sheet): a short title, two ticks per axis, no quadrant counts (they travel
    in the JSON). `extra_a_gates`: `(value, label)` candidate thresholds on the
    x axis, drawn dashed."""
    from PIL import Image, ImageDraw

    density = result.get("density")
    image = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(image)
    if not density:
        draw.text((12, size // 2), "no density", fill=AXIS, font=_font(13))
        return image
    bins = density["bins"]
    grid = np.frombuffer(base64.b64decode(density["log_density_u8"]), dtype=np.uint8)
    grid = grid.reshape(bins, bins)
    left, top, right, bottom = (44, 18, size - 8, size - 30) if compact else \
        (60, 26, size - 12, size - 52)
    plot = Image.fromarray(_colour(grid.T[::-1]), "RGB").resize(
        (right - left, bottom - top), Image.NEAREST)
    image.paste(plot, (left, top))
    (a_lo, a_hi), (b_lo, b_hi) = density["a_range"], density["b_range"]
    fallback = "log1p" if log_axes or log_axes is None else "values"
    spaces = density.get("spaces") or {"a": fallback, "b": fallback}
    fwd_a, label_a = _axis(spaces["a"])
    fwd_b, label_b = _axis(spaces["b"])

    def X(v):
        return left + (fwd_a(v) - a_lo) / max(a_hi - a_lo, 1e-12) * (right - left)

    def Y(v):
        return bottom - (fwd_b(v) - b_lo) / max(b_hi - b_lo, 1e-12) * (bottom - top)

    for value, label in extra_a_gates or ():
        ex = X(value)
        if left <= ex <= right:
            for y0 in range(int(top), int(bottom), 6):
                draw.line((ex, y0, ex, min(bottom, y0 + 3)), fill=EXTRA_GATE, width=1)
            draw.text((ex + 2, top + 2), str(label)[:4], fill=EXTRA_GATE, font=_font(10))
    gx, gy = X(result["gates"]["a"]), Y(result["gates"]["b"])
    if left <= gx <= right:
        draw.line((gx, top, gx, bottom), fill=GATE, width=2)
    if top <= gy <= bottom:
        draw.line((left, gy, right, gy), fill=GATE, width=2)
    small = _font(10 if compact else 11)
    draw.line((left, bottom, right, bottom), fill=AXIS)
    draw.line((left, top, left, bottom), fill=AXIS)
    if compact:
        for frac in (0.0, 1.0):
            ta, tb = a_lo + frac * (a_hi - a_lo), b_lo + frac * (b_hi - b_lo)
            x = left + frac * (right - left)
            draw.text((x - (0 if frac == 0.0 else 30), bottom + 4), label_a(ta), fill=AXIS,
                      font=small)
            y = bottom - frac * (bottom - top)
            draw.text((2, y - (0 if frac == 1.0 else 10)), label_b(tb), fill=AXIS, font=small)
        draw.text((left + (right - left) // 2 - 20, bottom + 16), f"{result['a']} (x)",
                  fill=TEXT, font=small)
        draw.text((left, 3), f"{result['a']} x {result['b']} (y) · {result['relation']}",
                  fill=TEXT, font=small)
        return image
    for frac in (0.0, 0.5, 1.0):
        ta, tb = a_lo + frac * (a_hi - a_lo), b_lo + frac * (b_hi - b_lo)
        x = left + frac * (right - left)
        draw.line((x, bottom, x, bottom + 4), fill=AXIS)
        draw.text((x - (0 if frac == 0.0 else 40 if frac == 1.0 else 16), bottom + 6),
                  label_a(ta), fill=AXIS, font=small)
        y = bottom - frac * (bottom - top)
        draw.line((left - 4, y, left, y), fill=AXIS)
        draw.text((4, y - (12 if frac == 1.0 else 0 if frac == 0.0 else 6)), label_b(tb),
                  fill=AXIS, font=small)
    q = result["quadrants"]
    font = _font(12)
    draw.text((right - 90, top + 4), f"both {q['both']}", fill=TEXT, font=font)
    draw.text((left + 6, top + 4), f"{result['b']} only {q['b_only']}", fill=TEXT, font=font)
    draw.text((right - 110, bottom - 18), f"{result['a']} only {q['a_only']}", fill=TEXT,
              font=font)
    draw.text((left + 6, bottom - 18), f"neither {q['neither']}", fill=TEXT, font=font)
    draw.text((left, 6), f"{result['a']} (x) vs {result['b']} (y) - {result['relation']}, "
                         f"contradiction {result['contradiction']:.2f}", fill=TEXT, font=font)

    def describe(name, space):
        return f"{name}: {'log1p axis, raw labels' if space == 'log1p' else 'table units'}"

    draw.text((left, bottom + 24), describe(result["a"], spaces["a"]) + " | "
              + describe(result["b"], spaces["b"]), fill=AXIS, font=small)
    return image
