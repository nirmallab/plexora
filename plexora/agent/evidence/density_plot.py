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


def draw_density(result, *, size=512, log_axes=True):
    """An RGB PIL image for one `bivariate_numbers` result with a density grid."""
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
    left, top, right, bottom = 48, 26, size - 12, size - 40
    plot = Image.fromarray(_colour(grid.T[::-1]), "RGB").resize(
        (right - left, bottom - top), Image.NEAREST)
    image.paste(plot, (left, top))
    (a_lo, a_hi), (b_lo, b_hi) = density["a_range"], density["b_range"]

    def fwd(v):
        return math.log1p(max(v, 0.0)) if log_axes else v

    def X(v):
        return left + (fwd(v) - a_lo) / max(a_hi - a_lo, 1e-12) * (right - left)

    def Y(v):
        return bottom - (fwd(v) - b_lo) / max(b_hi - b_lo, 1e-12) * (bottom - top)

    gx, gy = X(result["gates"]["a"]), Y(result["gates"]["b"])
    if left <= gx <= right:
        draw.line((gx, top, gx, bottom), fill=GATE, width=2)
    if top <= gy <= bottom:
        draw.line((left, gy, right, gy), fill=GATE, width=2)
    q = result["quadrants"]
    font = _font(12)
    draw.text((right - 90, top + 4), f"both {q['both']}", fill=TEXT, font=font)
    draw.text((left + 6, top + 4), f"{result['b']} only {q['b_only']}", fill=TEXT, font=font)
    draw.text((right - 110, bottom - 18), f"{result['a']} only {q['a_only']}", fill=TEXT,
              font=font)
    draw.text((left + 6, bottom - 18), f"neither {q['neither']}", fill=TEXT, font=font)
    draw.text((left, 6), f"{result['a']} (x) vs {result['b']} (y) - {result['relation']}, "
                         f"contradiction {result['contradiction']:.2f}", fill=TEXT, font=font)
    draw.text((left, bottom + 18), "log1p axes" if log_axes else "linear axes", fill=AXIS,
              font=_font(11))
    return image
