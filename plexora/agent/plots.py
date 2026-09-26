"""Small, deterministic plots drawn with Pillow -- no matplotlib.

Panel C of a gate check: the marker's distribution over every cell, the fitted
background and positive populations, the gate and its borderline band, and a
rug of the cells in the field being judged. Drawn on the log1p axis when the
values are raw non-negative intensities (they are log-normal; on a linear axis
the whole negative population is one bar), and the axis says which.
"""

from __future__ import annotations

import math

import numpy as np

BG = (18, 20, 24)
AXIS = (150, 156, 166)
BARS = (88, 96, 110)
BACKGROUND_CURVE = (79, 134, 198)
POSITIVE_CURVE = (255, 61, 242)
GATE = (255, 214, 10)
BAND = (255, 214, 10, 48)
RUG_POS = (255, 61, 242)
RUG_NEG = (79, 134, 198)


def _font(size):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def draw_histogram(values, *, gate, band=None, curves=None, rug=None, rug_positive=None,
                   width=512, height=512, log_axis=None, title="", bins=50):
    """An RGB PIL image. `curves` is {"background": [{x,y}], "positive": [...]}
    in the values' own units; `rug` the in-field values."""
    from PIL import Image, ImageDraw

    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if log_axis is None:
        log_axis = bool(values.size) and float(values.min()) >= 0
    fwd = np.log1p if log_axis else (lambda v: np.asarray(v, dtype=np.float64))

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image, "RGBA")
    left, right, top, bottom = 44, width - 14, 30, height - 40
    if title:
        draw.text((left, 8), title, fill=(230, 230, 230), font=_font(13))
    if not values.size:
        draw.text((left, height // 2), "no values", fill=AXIS, font=_font(13))
        return image

    tv = fwd(values)
    lo, hi = float(tv.min()), float(tv.max())
    if not hi > lo:
        hi = lo + 1.0
    counts, edges = np.histogram(tv, bins=bins, range=(lo, hi), density=True)
    ymax = float(counts.max()) or 1.0

    def curve_points(points):
        if not points:
            return []
        xs = np.asarray([p["x"] for p in points], dtype=np.float64)
        ys = np.asarray([p["y"] for p in points], dtype=np.float64)
        if log_axis:
            ys = ys * (1.0 + xs)   # density on the log1p axis
            xs = np.log1p(np.clip(xs, 0, None))
        return list(zip(xs, ys))

    drawn_curves = {k: curve_points(v) for k, v in (curves or {}).items()}
    for pts in drawn_curves.values():
        if pts:
            ymax = max(ymax, max(y for _, y in pts))

    def X(v):
        return left + (float(v) - lo) / (hi - lo) * (right - left)

    def Y(v):
        return bottom - float(v) / ymax * (bottom - top)

    if band is not None:
        b0, b1 = float(fwd(band[0])), float(fwd(band[1]))
        draw.rectangle((X(max(lo, b0)), top, X(min(hi, b1)), bottom), fill=BAND)
    for count, e0, e1 in zip(counts, edges[:-1], edges[1:]):
        if count > 0:
            draw.rectangle((X(e0) + 0.5, Y(count), X(e1) - 0.5, bottom), fill=BARS)
    for name, colour in (("background", BACKGROUND_CURVE), ("positive", POSITIVE_CURVE)):
        pts = drawn_curves.get(name) or []
        if len(pts) > 1:
            draw.line([(X(x), Y(y)) for x, y in pts if lo <= x <= hi], fill=colour, width=2)
    g = float(fwd(gate))
    if lo <= g <= hi:
        draw.line((X(g), top, X(g), bottom), fill=GATE, width=2)
    if rug is not None:
        rug = np.asarray(rug, dtype=np.float64)
        flags = (np.asarray(rug_positive, dtype=bool) if rug_positive is not None
                 else np.zeros(rug.shape, bool))
        for value, positive in zip(fwd(rug), flags):
            if np.isfinite(value) and lo <= value <= hi:
                x = X(value)
                draw.line((x, bottom + 3, x, bottom + 13),
                          fill=RUG_POS if positive else RUG_NEG, width=1)
    draw.line((left, bottom, right, bottom), fill=AXIS)
    draw.line((left, top, left, bottom), fill=AXIS)
    font = _font(11)
    for frac in (0.0, 0.5, 1.0):
        t = lo + frac * (hi - lo)
        label = f"{math.expm1(t):.3g}" if log_axis else f"{t:.3g}"
        x = X(t) - (0 if frac == 0.0 else 44 if frac == 1.0 else 16)
        draw.text((x, bottom + 16), label, fill=AXIS, font=font)
    axis = "log1p axis (labels in raw units)" if log_axis else "linear axis"
    draw.text((right - 190, top - 16), axis, fill=AXIS, font=font)
    return image


def montage(images, *, gap=6, background=BG):
    """Images side by side, top-aligned."""
    from PIL import Image

    width = sum(im.width for im in images) + gap * (len(images) - 1)
    height = max(im.height for im in images)
    out = Image.new("RGB", (width, height), background)
    x = 0
    for im in images:
        out.paste(im, (x, 0))
        x += im.width + gap
    return out


def header(image, text, *, height=26, background=(30, 33, 40)):
    """`image` with a strip of text above it."""
    from PIL import Image, ImageDraw

    out = Image.new("RGB", (image.width, image.height + height), background)
    out.paste(image, (0, height))
    ImageDraw.Draw(out).text((8, 6), text, fill=(235, 235, 235), font=_font(13))
    return out
