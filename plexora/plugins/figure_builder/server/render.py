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

from plexora import api

#: The alpha every channel is drawn with, from frag.glsl's `u8_r_range(0.9)` /
#: `u16_rg_range(0.9)`. Not a style choice here -- it is what the user was
#: looking at when they chose their windows.
CHANNEL_ALPHA = 0.9

#: Ceiling on one rendered panel, in pixels per side. A 300 mm page at 1200 DPI
#: is ~14,000 px; past this a single panel is gigabytes of float32 and the
#: request is a mistake rather than a figure.
MAX_PANEL_PIXELS = 16_000

#: Most source pixels one panel may read before it is refused. A whole-slide
#: overview at level 0 is 10^10 pixels; the level chooser normally avoids that,
#: and this catches the cases where it cannot.
MAX_SOURCE_PIXELS = 120_000_000


class RenderError(Exception):
    """This panel cannot be rendered, with a reason worth showing the user."""


class SourceImage:
    """One image file, opened once and read from many times.

    Held open across a whole export rather than reopened per panel: a figure is
    routinely eight panels from one slide, and reopening a pyramidal TIFF eight
    times is eight directory walks for the same answer.
    """

    def __init__(self, datasource):
        dataset = api.dataset(datasource)
        self.datasource = datasource
        self.channels = list(dataset.image.channels)
        width, height = dataset.image.size
        self.width = int(width or 0)
        self.height = int(height or 0)
        self._file = None
        self._remote = None

        if not dataset.image.is_local:
            # The pixels are on a data node. Nothing is opened here and nothing
            # is downloaded up front: `read` asks for exactly the rectangle a
            # panel covers, at the level it chose, which is the same few
            # hundred kilobytes a local read would have taken off the pyramid.
            self._remote = dataset.image
            geometry = self._remote.geometry()
            self.levels = max(1, int(geometry.get("levels") or 1))
            self._level_shapes = [tuple(shape) for shape
                                  in (geometry.get("level_shapes") or [])]
            return

        import tifffile
        import zarr

        source = dataset.image.source
        if source is None or not source.path:
            raise RenderError(f"{datasource} has no image file on disk")

        self._file = tifffile.TiffFile(source.path, is_ome=False)
        self._zarr = zarr.open(self._file.series[0].aszarr(), mode="r")
        self._is_array = hasattr(self._zarr, "shape")
        self.levels = 1 if self._is_array else len(list(self._zarr))
        self._level_shapes = None

    def close(self):
        if self._file is None:
            return
        try:
            self._file.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def level(self, index):
        """One pyramid level as an array-like, level 0 being full resolution.

        Local images only -- there is no array to hand back for one on a node,
        and materializing a level to pretend otherwise is exactly the transfer
        the whole arrangement exists to avoid. Callers that only need a
        rectangle should use `read`, which works either way.
        """
        if self._remote is not None:
            raise RenderError(
                f"{self.datasource}'s image is on a data node, so its pyramid "
                "cannot be handed over whole. Read a region instead.")
        if self._is_array:
            return self._zarr
        return self._zarr[str(index)]

    def level_shape(self, index):
        """(height, width) of one level, without reading it."""
        if self._level_shapes:
            return self._level_shapes[min(index, len(self._level_shapes) - 1)]
        plane = self.level(index)
        return (plane.shape[-2], plane.shape[-1])

    def channel_index(self, key):
        """Where a channel sits in the pyramid, from its stable URL key.

        The pyramid holds only real image channels -- 'Area' is a Plexora-side
        placeholder for a segmentation mask and was never part of the file -- so
        the index is the position among those, which is exactly what
        `ImageHandle.channels` gives.
        """
        for index, channel in enumerate(self.channels):
            src = str(channel.get("src") or "").rstrip("/")
            if src.rsplit("/", 1)[-1] == key:
                return index
        return None

    def read(self, channel_index, level, box):
        """A rectangle of one channel, at one level, as a 2-D array.

        `box` is in THAT LEVEL's pixels. Clipped to the array rather than
        refused: a capture that runs a few pixels off the edge of the slide is
        an ordinary thing to have drawn, and the result is the region that
        exists with black where the image does not.
        """
        if self._remote is not None:
            # The node clips against the level's real dimensions and enforces
            # the pixel budget there, so the refusal happens before anything is
            # read rather than after a gigabyte has crossed a network.
            from plexora.server.providers.base import ResourceError

            try:
                stack, clipped = self._remote.read_region(
                    level,
                    (int(math.floor(box[0])), int(math.floor(box[1])),
                     int(math.ceil(box[2])), int(math.ceil(box[3]))),
                    [channel_index], max_pixels=MAX_SOURCE_PIXELS)
            except ResourceError as exc:
                raise RenderError(str(exc)) from exc
            return np.asarray(stack[0]), tuple(clipped)

        array = self.level(level)
        plane = array[channel_index] if array.ndim == 3 else array
        height, width = plane.shape[-2], plane.shape[-1]

        x0 = max(0, min(int(math.floor(box[0])), width))
        y0 = max(0, min(int(math.floor(box[1])), height))
        x1 = max(x0, min(int(math.ceil(box[2])), width))
        y1 = max(y0, min(int(math.ceil(box[3])), height))
        if x1 <= x0 or y1 <= y0:
            return np.zeros((1, 1), dtype=np.float32), (x0, y0, x1, y1)
        if (x1 - x0) * (y1 - y0) > MAX_SOURCE_PIXELS:
            raise RenderError(
                "this panel covers more of the image than one render can read; "
                "export it at a lower DPI"
            )
        return np.asarray(plane[y0:y1, x0:x1]), (x0, y0, x1, y1)


def choose_level(source, viewport_width, target_pixels):
    """The cheapest pyramid level that still has the detail being asked for.

    The largest level index whose pixels across the region still meet or exceed
    the target, so a 400-pixel-wide panel of a whole slide reads a few hundred
    kilobytes instead of the level-0 gigabyte that would be downsampled away.
    Level 0 when nothing else is big enough, which is also when the warning
    below applies.
    """
    best = 0
    for level in range(max(1, source.levels)):
        if viewport_width / (2 ** level) >= target_pixels:
            best = level
        else:
            break
    return best


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
    level = choose_level(source, viewport["w"], target_width)
    divisor = 2 ** level
    box = (viewport["x"] / divisor, viewport["y"] / divisor,
           (viewport["x"] + viewport["w"]) / divisor,
           (viewport["y"] + viewport["h"]) / divisor)

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


def panel_report(source, scene, width_mm, dpi):
    """What is worth telling the user about this panel before they export it.

    Computed separately from the render so the export dialog can show it without
    reading a single pixel.
    """
    viewport = scene["viewport"]
    missing = [channel.get("fullname_at_capture") or channel["key"]
               for channel in scene.get("channels") or []
               if source.channel_index(channel["key"]) is None]
    overlays = []
    for layer in (scene.get("core_overlays") or {}).get("cell_layers") or []:
        if layer.get("visible") and layer.get("mode") not in (None, "", "none"):
            overlays.append(layer["name"])
    overlays.extend(name for name in (scene.get("plugins") or {}) if name not in overlays)

    return {
        "effective_dpi": round(effective_dpi(viewport["w"], width_mm), 1),
        "requested_dpi": dpi,
        "missing_channels": missing,
        # Named, not silently dropped: an export that omits the phenotype
        # colouring a figure was made to show has to say so.
        "missing_overlays": sorted(set(overlays)),
    }
