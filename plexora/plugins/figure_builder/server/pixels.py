"""Raw channel pixels for Quick Edit, read straight off the pyramid.

Quick Edit shows a small live view of the image behind a figure panel so the
user can reframe it and adjust its channels without leaving the canvas. It
needs actual pixels, and there were two ways to get them.

**Not the main viewer's routes.** Those go through `data_model`, which holds ONE
loaded datasource behind a lock and loads the whole cell table, the segmentation
mask and the channel metadata with it ("this can take some time" is in its own
docstring). Quick Edit would pay that per panel, evict whatever the user had
open, and pay it again when they went back to the viewer -- and a figure
legitimately spans several images, so a four-panel figure would do it four
times. Export already refuses to touch that module for exactly this reason, and
`test_the_export_never_loads_a_datasource` pins it.

**So: the same reader export uses.** `render.SourceImage` opens the TIFF
directly, per request, and this hands back one channel of one region as a plain
array of numbers.

## One channel at a time, and no compositing

The response is a single channel's greyscale, uint16 little-endian, and the
browser composites. That is the whole performance argument for the feature:
changing a channel's colour or dragging its contrast slider is then arithmetic
over bytes already in memory and costs NO network at all. A route that returned
a composited RGB would make every contrast tweak a round trip, which is exactly
the interaction Quick Edit exists to make cheap. MiniMap already works this way.

Sixteen bits rather than eight because the windows are chosen in raw units: an
8-bit response would have to be pre-windowed, and then the slider could only
move within whatever window the server happened to pick.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.figure_builder.server.render import (
    MAX_SOURCE_PIXELS, RenderError, SourceImage, choose_level)

#: Biggest region a single request may hand back, per side. The mini viewer is
#: a few hundred pixels across; this is well past what it asks for and well
#: short of what would make a response worth megabytes.
MAX_OUT_PIXELS = 1024

#: How many pixels the coarsest level is sampled down to when computing the
#: intensity summary. Enough for a stable percentile, small enough to be
#: instant on a whole slide.
STATS_SAMPLE = 512


def read_region(datasource, channel_key, box, out_size):
    """One channel of one region, resampled to `out_size`.

    `box` is (x, y, w, h) in FULL-RESOLUTION image pixels -- the same
    coordinate system a panel's `scene.viewport` is in, so a caller never has
    to know which pyramid level was actually read.

    Returns `(array, clipped_box)`: a 2-D uint16 array of exactly `out_size`,
    and the part of `box` that was inside the image. The clipped box is
    returned rather than silently applied, because a region that ran off the
    edge of the slide has to be drawn in the right PLACE, and a caller given
    only the pixels would centre the wrong thing.
    """
    out_w, out_h = int(out_size[0]), int(out_size[1])
    if out_w < 1 or out_h < 1:
        raise RenderError("a region must be at least one pixel")
    if out_w > MAX_OUT_PIXELS or out_h > MAX_OUT_PIXELS:
        raise RenderError(
            f"a region of {out_w}x{out_h} is past what one read returns "
            f"(max {MAX_OUT_PIXELS} a side)")

    with SourceImage(datasource) as source:
        index = source.channel_index(channel_key)
        if index is None:
            raise RenderError(f"{channel_key!r} is not a channel of this image")

        level = choose_level(source, box[2], out_w)
        divisor = 2 ** level
        plane, clipped = source.read(index, level, (
            box[0] / divisor, box[1] / divisor,
            (box[0] + box[2]) / divisor, (box[1] + box[3]) / divisor))

    return _resample(plane, out_w, out_h), tuple(value * divisor for value in clipped)


def _resample(plane, out_w, out_h):
    """To exactly the requested size, in uint16.

    Pillow rather than a stride trick: LANCZOS is what the exporter uses, and a
    mini viewer that resampled differently from the export would show the user
    a slightly different picture from the one they are composing.
    """
    from PIL import Image

    array = np.asarray(plane)
    if array.size == 0:
        return np.zeros((out_h, out_w), dtype=np.uint16)
    # Pillow's "I;16" mode cannot resize, so the arithmetic is done in float32
    # and put back afterwards -- which is also what keeps a 16-bit source from
    # being quantised to 8 bits on the way through.
    image = Image.fromarray(array.astype(np.float32), mode="F")
    if image.size != (out_w, out_h):
        image = image.resize((out_w, out_h), Image.LANCZOS)
    return np.clip(np.asarray(image), 0, 65535).astype(np.uint16)


def channel_stats(datasource, channel_key):
    """What a contrast slider needs to know about a channel.

    Read from the COARSEST pyramid level: the percentiles of a whole slide are
    the same to within noise at any level, and reading level 0 to draw a slider
    would be a gigabyte for a number the user is about to drag past anyway.
    """
    with SourceImage(datasource) as source:
        index = source.channel_index(channel_key)
        if index is None:
            raise RenderError(f"{channel_key!r} is not a channel of this image")

        level = max(0, source.levels - 1)
        array = source.level(level)
        plane = array[index] if array.ndim == 3 else array
        height, width = plane.shape[-2], plane.shape[-1]
        if height * width > MAX_SOURCE_PIXELS:
            raise RenderError("this image has no level small enough to summarise")
        sample = np.asarray(plane)

    step = max(1, int(max(sample.shape) / STATS_SAMPLE))
    sample = sample[::step, ::step].astype(np.float32)
    if sample.size == 0:
        return {"min": 0, "max": 1, "p01": 0, "p999": 1, "dtype": "uint16"}

    low, high = np.percentile(sample, [1.0, 99.9])
    return {
        "min": float(sample.min()),
        "max": float(sample.max()),
        # The default window the viewer's auto-level lands on, so a channel
        # switched on in Quick Edit arrives looking the way it would there.
        "p01": float(low),
        "p999": float(high),
        "dtype": str(np.asarray(sample).dtype),
    }
