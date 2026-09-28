"""Composing evidence sheets: panels from `render_region`, captions, a title.

A sheet is one image an agent reads at a glance -- several rendered panels in a
grid, each captioned in plain text. The helpers here are generic (QC's audit
and candidate sheets use them; gating's sheet predates them): fit a picture
into a slot, write text the bundled font can draw, render a region to a PIL
image, and colour a map as a heat map. Everything is a pure function of its
inputs, so a sheet is the same bytes every time.
"""

from __future__ import annotations

import io

import numpy as np

BACKGROUND = (14, 14, 18)
TEXT = (230, 230, 235)
MUTED = (150, 150, 160)


def font(size):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def ascii_text(text):
    """What the bundled font can draw (no µ, no ≈, no ·)."""
    return (str(text).replace("µ", "u").replace("≈", "~").replace("·", "|")
            .replace("–", "-").replace("—", "-"))


def fit(image, width, height, background=BACKGROUND):
    """`image` letterboxed into (width, height), centred."""
    from PIL import Image

    if image.width > width or image.height > height:
        scale = min(width / image.width, height / image.height)
        image = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))), Image.LANCZOS)
    out = Image.new("RGB", (width, height), background)
    out.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return out, ((width - image.width) // 2, (height - image.height) // 2,
                 image.width, image.height)


class Sheet:
    """A grid of equal slots under a title bar."""

    def __init__(self, columns, rows, slot, *, title="", caption_h=18, gap=4, title_h=26):
        from PIL import Image, ImageDraw

        self.columns, self.rows = columns, rows
        self.slot = slot
        self.caption_h = caption_h
        self.gap = gap
        self.title_h = title_h if title else 0
        width = columns * slot + (columns + 1) * gap
        height = self.title_h + rows * (slot + caption_h) + (rows + 1) * gap
        self.image = Image.new("RGB", (width, height), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        if title:
            self.draw.text((gap + 2, 6), ascii_text(title), fill=TEXT, font=font(14))
        self.placed = []

    @property
    def size(self):
        return self.image.size

    def origin(self, index):
        column, row = index % self.columns, index // self.columns
        x = self.gap + column * (self.slot + self.gap)
        y = self.title_h + self.gap + row * (self.slot + self.caption_h + self.gap)
        return x, y

    def place(self, index, picture, caption=""):
        """Put a PIL image in slot `index` (row-major); returns where it landed
        {x, y, width, height} in sheet pixels."""
        x, y = self.origin(index)
        fitted, (ox, oy, w, h) = fit(picture, self.slot, self.slot)
        self.image.paste(fitted, (x, y))
        if caption:
            self.draw.text((x + 2, y + self.slot + 2), ascii_text(caption)[:60], fill=TEXT,
                           font=font(12))
        where = {"x": x + ox, "y": y + oy, "width": w, "height": h}
        self.placed.append(where)
        return where

    def blank(self, index, text=""):
        x, y = self.origin(index)
        if text:
            self.draw.text((x + 8, y + self.slot // 2), ascii_text(text)[:40], fill=MUTED,
                           font=font(12))

    def encode(self, fmt):
        from plexora.agent.evidence.collage import encode

        return encode(self.image, fmt)


def render_panel(session, spec, *, pixel=None):
    """(PIL image, manifest) of a `RenderInput`, not stored."""
    from PIL import Image

    from plexora.agent import render

    rendered = render.render_region(session, spec, store=False, pixel_size=pixel)
    return Image.open(io.BytesIO(rendered["png"])).convert("RGB"), rendered["manifest"]


def heatmap(values, *, size, mask=None, low=(20, 30, 90), high=(255, 210, 40), clip=None):
    """A map as a two-stop colour ramp, NaN and outside `mask` drawn dark."""
    from PIL import Image

    data = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(data) & (mask if mask is not None else True)
    if clip is None:
        lo, hi = (np.percentile(data[finite], [2, 98]) if finite.any() else (0.0, 1.0))
    else:
        lo, hi = clip
    t = np.clip((data - lo) / max(1e-12, hi - lo), 0, 1)
    rgb = np.zeros(data.shape + (3,), dtype=np.float64)
    for channel in range(3):
        rgb[..., channel] = low[channel] + (high[channel] - low[channel]) * t
    rgb[~finite] = (10, 10, 12)
    picture = Image.fromarray(rgb.astype(np.uint8), "RGB")
    return picture.resize(size, Image.NEAREST)


def shapes_onto(picture, manifest, shapes):
    """Draw `ShapeSpec`s over a rendered panel, in the panel's own pixels
    (no cap on how many: the render's MAX_SHAPES bounds a request, not this)."""
    from plexora.agent import render

    bounds = manifest["bounds_fullres"]
    fullres = (bounds["x"], bounds["y"], bounds["x"] + bounds["width"],
               bounds["y"] + bounds["height"])
    return render._draw_shapes(picture, shapes, fullres, picture.size)


def to_panel_px(manifest, picture, x, y):
    """A full-resolution point in a rendered panel's own pixels."""
    bounds = manifest["bounds_fullres"]
    return ((x - bounds["x"]) * picture.width / max(1e-9, bounds["width"]),
            (y - bounds["y"]) * picture.height / max(1e-9, bounds["height"]))
