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

## The readers are kept open

Opening a pyramidal TIFF is a directory walk and a zarr store built over it,
and Quick Edit asks for one region per visible channel on every framing change
-- so a user panning across a slide was paying that open several times a
second, for the same file, having changed nothing about it. `_reader` keeps a
few open instead. See its docstring for what keeps them honest.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict

import numpy as np

from plexora import api
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

#: How many source files stay open. A figure spanning more images than this
#: still works -- it just reopens -- and each held reader is a file handle plus
#: a zarr store, not pixels, so this is small on purpose.
READER_LIMIT = 4

#: datasource -> _Reader, least-recently-used first.
_READERS: "OrderedDict[str, _Reader]" = OrderedDict()

#: Guards the map above, and nothing else. Never held across a read.
_READERS_LOCK = threading.Lock()


class _Reader:
    """One held-open `SourceImage`, with the path it was opened from.

    Its own lock because a `SourceImage` is one file handle over one zarr
    store: two threads reading different channels of the same file through it
    at once is a data race in tifffile, not merely a slow spot. Different
    datasources hold different locks and still overlap, which is the case that
    matters -- a figure spanning two slides fetches from both at once.
    """

    __slots__ = ("source", "path", "lock")

    def __init__(self, source, path):
        self.source = source
        self.path = path
        self.lock = threading.Lock()


def _image_path(datasource):
    """Where this datasource's image file is, right now.

    Read per request and compared against what the held reader was opened
    from, so a project repointed at a different file -- or reregistered under
    the same name in a test -- is never served out of a reader for the old one.
    It is a project-record read, which is what `SourceImage.__init__` was doing
    anyway before it opened the TIFF on top of it.
    """
    source = api.dataset(datasource).image.source
    return str(source.path) if source is not None and source.path else None


@contextlib.contextmanager
def _reader(datasource):
    """The open reader for a datasource, made once and kept.

    Exclusive for the duration of the block: callers may read concurrently
    across datasources but never within one -- see `_Reader`.
    """
    with _READERS_LOCK:
        held = _READERS.pop(datasource, None)
        if held is not None and held.path != _image_path(datasource):
            held.source.close()
            held = None
        if held is None:
            path = _image_path(datasource)
            held = _Reader(SourceImage(datasource), path)
        # Re-inserted at the end, which is what makes this an LRU.
        _READERS[datasource] = held
        while len(_READERS) > READER_LIMIT:
            _, evicted = _READERS.popitem(last=False)
            # Waited for rather than closed underneath: a reader in the middle
            # of a read is a file another thread is still holding. Nothing
            # acquires the map lock while holding a reader's, so this cannot
            # deadlock.
            with evicted.lock:
                evicted.source.close()

    with held.lock:
        yield held.source


def close_readers():
    """Close every held reader. For shutdown and for tests."""
    with _READERS_LOCK:
        while _READERS:
            _, held = _READERS.popitem()
            with held.lock:
                held.source.close()


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

    with _reader(datasource) as source:
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
    with _reader(datasource) as source:
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
