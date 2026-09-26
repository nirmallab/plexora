"""Re-rendering a captured panel from the source pixels.

This is the half of the product claim that the preview raster is not. A panel
captured at 300 screen pixels exports at whatever the page size and the DPI ask
for -- two thousand, four thousand -- because the scene records the region in
full-resolution image coordinates and the windows in raw units, and both survive
the screen they were chosen on.

**Nothing here goes through `data_model`.** That module holds ONE loaded
datasource behind a lock, and it is the one the user is looking at; a render
that called `load_datasource` would evict their session to draw a figure, and a
figure spanning four images would evict it four times. Reading the file directly
is what makes exporting a multi-image figure possible at all.

## The compositing, and why it looks like this

It is the numpy transcription of `client/src/shaders/frag.glsl` plus the
`lighter` blend `viewerManager.js` composites channels with. Per channel:

    t   = clip((raw - lo) / (hi - lo), 0, 1)      # the shader's range_clamp
    rgb = colour * t * ALPHA                      # u_tile_color * pixel_val
    accumulate, then clip                         # canvas "lighter"

Written out rather than approximated because an export that does not match what
the user was looking at is worse than no export: they chose those windows by
eye, against that arithmetic.

## What this does NOT re-render

Overlays -- coloured cells, ROI outlines -- are not reproduced. They cannot be,
from what a figure stores: a cell layer's colours are a lookup table over every
cell in the image, and a figure deliberately holds no derived data of that size.
Reproducing them would mean re-running the plugin that computed them, server
side, which is a different feature.

So an export reports them, per panel, instead of quietly dropping them. See
`missing_overlays`. The microscopy image itself is fully re-rendered, which is
the part that could not be recovered any other way.
"""

from __future__ import annotations

import math

import numpy as np

# The reader and the level rules moved to core (server/utils/source_image.py),
# because an agent's rendered evidence reads pixels the same way and core
# cannot import a plugin. Re-exported under the names this module always had.
from plexora.server.utils.source_image import (  # noqa: F401
    CHANNEL_ALPHA,
    MAX_SOURCE_PIXELS,
    VIEW_DETAIL,
    RenderError,
    SourceImage,
    choose_level,
    choose_view_level,
    composite,
)

#: Ceiling on one rendered panel, in pixels per side. A 300 mm page at 1200 DPI
#: is ~14,000 px; past this a single panel is gigabytes of float32 and the
#: request is a mistake rather than a figure.
MAX_PANEL_PIXELS = 16_000


def effective_dpi(viewport_width_px, width_mm):
    """How many dots per inch this panel's SOURCE can actually supply.

    Reported rather than enforced: a panel below the threshold still exports,
    because a reviewer's deadline is a real thing and a slightly soft inset is
    usually the right trade. Silently upscaling and saying nothing is not.
    """
    if width_mm <= 0:
        return 0.0
    return viewport_width_px / (width_mm / 25.4)


def render_panel(source, scene, target_width, target_height):
    """One panel's image, as an (H, W, 3) uint8 array.

    `target_width`/`target_height` are the pixels the page asks for at the
    chosen DPI. The level is picked from the width; the result is resampled to
    exactly the requested size, so the page composition never has to reason
    about what the pyramid happened to hold.
    """
    from PIL import Image

    if max(target_width, target_height) > MAX_PANEL_PIXELS:
        raise RenderError(
            f"a panel of {target_width}x{target_height} pixels is past what one "
            f"render can produce (max {MAX_PANEL_PIXELS} a side)")

    viewport = scene["viewport"]
    if viewport.get("orientation"):
        return _render_oriented(source, scene, viewport["orientation"],
                                target_width, target_height)
    level = choose_level(source, viewport["w"], target_width)
    divisor = 2 ** level
    box = (viewport["x"] / divisor, viewport["y"] / divisor,
           (viewport["x"] + viewport["w"]) / divisor,
           (viewport["y"] + viewport["h"]) / divisor)

    if source.is_brightfield:
        # Nothing to composite and no window to apply -- the samples are the
        # picture. Taken before the channel loop rather than inside it because
        # a brightfield scene carries no channel entries at all: there is no
        # sidebar row to have captured one from.
        block = source.read_rgb(level, box)
        image = Image.fromarray(np.ascontiguousarray(block), "RGB")
        if image.size != (target_width, target_height):
            image = image.resize((max(1, target_width), max(1, target_height)),
                                 Image.LANCZOS)
        return image, {"level": level, "channels_rendered": 1}

    accumulator = None
    rendered = 0
    for channel in scene.get("channels") or []:
        if not channel.get("visible", True):
            continue
        index = source.channel_index(channel["key"])
        if index is None:
            # Reported by the caller through `missing_channels`; nothing is
            # substituted, because a substituted channel produces a panel that
            # looks right and is wrong.
            continue
        plane, _ = source.read(index, level, box)
        if accumulator is None:
            accumulator = np.zeros((plane.shape[0], plane.shape[1], 3), dtype=np.float32)
        elif plane.shape != accumulator.shape[:2]:
            # Levels of a pyramid can be off by a pixel against each other.
            plane = plane[:accumulator.shape[0], :accumulator.shape[1]]

        low, high = float(channel["window"][0]), float(channel["window"][1])
        span = high - low
        if span <= 0:
            continue
        # The shader's range_clamp, in raw units -- identical arithmetic to
        # dividing both sides by 65535 first, and without the rounding.
        scaled = np.clip((plane.astype(np.float32) - low) / span, 0.0, 1.0)
        colour = channel["color"]
        weight = CHANNEL_ALPHA / 255.0
        for offset, key in enumerate(("r", "g", "b")):
            value = float(colour[key])
            if value:
                accumulator[:scaled.shape[0], :scaled.shape[1], offset] += scaled * (value * weight)
        rendered += 1

    if accumulator is None:
        # Every channel was gone, or the panel had none. Black, which is what
        # the viewer shows for the same state, rather than an error: the panel's
        # labels and scale bar are still worth exporting.
        accumulator = np.zeros((max(1, target_height), max(1, target_width), 3),
                               dtype=np.float32)

    image = Image.fromarray((np.clip(accumulator, 0.0, 1.0) * 255.0).astype(np.uint8), "RGB")
    if image.size != (target_width, target_height):
        # LANCZOS both ways. Downsampling a bright-on-black fluorescence image
        # with a box filter loses thin structures; upsampling with nearest
        # produces the blocky look that gives "exported from a screenshot" away.
        image = image.resize((max(1, target_width), max(1, target_height)), Image.LANCZOS)
    return image, {"level": level, "channels_rendered": rendered}


def _composite_padded(source, scene, level, box):
    """The box's pixels composited, as (H, W, 3) uint8, with background where
    the box runs off the image.

    Padded rather than clipped, unlike the upright path: a turned frame is cut
    from the MIDDLE of this raster, and a raster that shrank to the part of the
    box that exists would move that middle -- the panel would show a field a
    little way from the one that was framed. The compositing itself is core's
    (`source_image.composite`), shared with an agent's rendered evidence.
    """
    return composite(source, scene.get("channels") or [], level, box)


def orient_offset(orientation, dx, dy):
    """Where a vector in the image lands after the viewer's turn and mirror.

    Clockwise by `degrees` in image axes (y down), then negated on each
    mirrored screen axis -- `screen = Fh . Fv . R(degrees)`, as
    services/viewTransform.js defines it.
    """
    radians = math.radians(orientation["degrees"])
    x = dx * math.cos(radians) - dy * math.sin(radians)
    y = dx * math.sin(radians) + dy * math.cos(radians)
    return (-x if orientation["flip_h"] else x, -y if orientation["flip_v"] else y)


def orient_raster(image, orientation, background):
    """Turn and mirror a raster the way the viewer turned the image.

    PIL's angle is counter-clockwise, so the viewer's clockwise turn is its
    negative; `expand` keeps the whole turned raster, centred where it was.
    A right angle goes through PIL's exact transpose, so a quarter-turned
    panel is the source pixels rearranged, not resampled.
    """
    from PIL import Image

    degrees = orientation["degrees"] % 360.0
    if degrees:
        fill = (background,) * 3
        image = image.rotate(-degrees, resample=Image.BICUBIC, expand=True, fillcolor=fill)
    if orientation["flip_v"]:
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
    if orientation["flip_h"]:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    return image


def _render_oriented(source, scene, orientation, target_width, target_height):
    """A panel framed on a turned or mirrored view, as the viewer showed it.

    Read the box around the frame, composite it exactly as the upright path
    does, turn and mirror the result as the viewer did, and cut the frame out
    of the middle. The level is chosen from the FRAME's width, which is what
    the panel's pixels are spread across.
    """
    from PIL import Image

    viewport = scene["viewport"]
    frame_w, frame_h = orientation["frame_w"], orientation["frame_h"]
    level = choose_level(source, frame_w, target_width)
    divisor = 2 ** level
    x0 = int(math.floor(viewport["x"] / divisor))
    y0 = int(math.floor(viewport["y"] / divisor))
    x1 = max(x0 + 1, int(math.ceil((viewport["x"] + viewport["w"]) / divisor)))
    y1 = max(y0 + 1, int(math.ceil((viewport["y"] + viewport["h"]) / divisor)))
    if (x1 - x0) * (y1 - y0) > MAX_SOURCE_PIXELS:
        raise RenderError(
            "this panel covers more of the image than one render can read; "
            "export it at a lower DPI"
        )

    pixels, rendered, background = _composite_padded(source, scene, level, (x0, y0, x1, y1))
    raster = Image.fromarray(np.ascontiguousarray(pixels), "RGB")

    # The frame's true middle, relative to the raster's: the box was widened
    # to whole pixels, so the two differ by up to a pixel, and the turn moves
    # that difference with everything else.
    centre_x = (viewport["x"] + viewport["w"] / 2) / divisor - x0
    centre_y = (viewport["y"] + viewport["h"] / 2) / divisor - y0
    shift = orient_offset(orientation, centre_x - raster.width / 2,
                          centre_y - raster.height / 2)
    turned = orient_raster(raster, orientation, background)

    middle_x = turned.width / 2 + shift[0]
    middle_y = turned.height / 2 + shift[1]
    half_w, half_h = frame_w / divisor / 2, frame_h / divisor / 2
    frame = turned.crop((int(round(middle_x - half_w)), int(round(middle_y - half_h)),
                         int(round(middle_x + half_w)), int(round(middle_y + half_h))))
    if frame.size != (target_width, target_height):
        frame = frame.resize((max(1, target_width), max(1, target_height)), Image.LANCZOS)
    return frame, {"level": level, "channels_rendered": rendered}


def panel_report(source, scene, width_mm, dpi):
    """What is worth telling the user about this panel before they export it.

    Computed separately from the render so the export dialog can show it without
    reading a single pixel.
    """
    viewport = scene["viewport"]
    missing = [channel.get("fullname_at_capture") or channel["key"]
               for channel in scene.get("channels") or []
               if source.channel_index(channel["key"]) is None]
    core = scene.get("core_overlays") or {}
    overlays = []
    for layer in core.get("cell_layers") or []:
        if layer.get("visible") and layer.get("mode") not in (None, "", "none"):
            overlays.append(layer["name"])
    # Anything in the stack that is NOT the reference image. The export
    # re-renders this source's channels and nothing else, so a second image
    # layer, a transcript layer and a boundary layer are all absent from the
    # deliverable however faithfully they were recorded. Named, for the same
    # reason the cell layers above are: an export that omits what a figure was
    # made to show has to say so.
    for layer in core.get("layers") or []:
        if not layer.get("visible") or layer.get("id") == "__image__":
            continue
        # The mask already arrives through cell_layers, under the plugin's name.
        if layer.get("id") == "__mask__" and overlays:
            continue
        overlays.append(layer.get("label") or layer.get("id"))
    overlays.extend(name for name in (scene.get("plugins") or {}) if name not in overlays)

    return {
        # Across the panel: a turned capture's frame, not the box around it.
        "effective_dpi": round(effective_dpi(
            (viewport.get("orientation") or {}).get("frame_w") or viewport["w"], width_mm), 1),
        "requested_dpi": dpi,
        "missing_channels": missing,
        # Named, not silently dropped: an export that omits the phenotype
        # colouring a figure was made to show has to say so.
        "missing_overlays": sorted(set(overlays)),
    }
