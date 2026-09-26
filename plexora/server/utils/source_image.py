"""Reading a rectangle of a project's image straight off its pyramid.

Moved here from Figure Builder, whose export and Quick Edit were the first
things that needed it, because they are no longer the only ones: an agent's
rendered evidence (plexora/agent/render.py) reads the same pixels the same way,
and core cannot import a plugin. Figure Builder re-exports every name, so its
own code and tests read exactly as they did.

**Nothing here goes through `data_model`.** That module holds ONE loaded
datasource behind a lock, and it is the one the user is looking at; a read that
called `load_datasource` would evict their session. Opening the file directly
is what lets a figure span four images and an agent inspect a project nobody
has open.

The compositing is the numpy transcription of `client/src/shaders/frag.glsl`
plus the `lighter` blend the viewer composites channels with:

    t   = clip((raw - lo) / (hi - lo), 0, 1)      # the shader's range_clamp
    rgb = colour * t * ALPHA                      # u_tile_color * pixel_val
    accumulate, then clip                         # canvas "lighter"
"""

from __future__ import annotations

import contextlib
import math
import threading
from collections import OrderedDict

import numpy as np


#: The alpha every channel is drawn with, from frag.glsl's `u8_r_range(0.9)` /
#: `u16_rg_range(0.9)`. Not a style choice here -- it is what the user was
#: looking at when they chose their windows.
CHANNEL_ALPHA = 0.9

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
        from plexora.api.dataset import ProjectData, project_data
        from plexora.server.utils import brightfield

        # A name, or a handle set already in hand -- an agent session holds its
        # own, and building another from the name would re-read the registry
        # for an answer it already has.
        if isinstance(datasource, ProjectData):
            dataset, datasource = datasource, datasource.name
        else:
            dataset = project_data(datasource)
        self.datasource = datasource
        self.channels = list(dataset.image.channels)
        width, height = dataset.image.size
        self.width = int(width or 0)
        self.height = int(height or 0)
        self._file = None
        self._remote = None
        self._pyramid = None
        #: Whether this panel is one colour picture rather than a stack to
        #: colorize. Read off the layer's own tile key, which is the sentinel
        #: `rgb` for exactly this case -- the same string the tile route
        #: dispatches on, so a figure and the viewer cannot disagree.
        self.is_brightfield = any(
            str(channel.get("src") or "").rstrip("/").rsplit("/", 1)[-1]
            == brightfield.RGB_CHANNEL_KEY
            for channel in self.channels)

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

        from plexora.server.utils import brightfield

        source = dataset.image.source
        if source is None or not source.path:
            raise RenderError(f"{datasource} has no image file on disk")

        # Dispatched on the file's layout, in the order LocalImageProvider.open
        # takes -- the viewer's own reader for each format, handed the
        # project's derived coarse levels (`image.pyramid`) the same way. Every
        # one of these returns levels indexed `[channel, rows, cols]` (and
        # `.rgb[rows, cols]` for colour), which is all `read`/`read_rgb` ask.
        #
        # A Xenium focus folder first: every test below takes a file path.
        # DICOM before the colour test: a DICOM H&E carries the brightfield
        # kind, and OpenSlide would flatten it. An interleaved-RGB slide read
        # by the tifffile branch would report its own height as its channel
        # count and every panel drawn from it would be a one-pixel-wide strip.
        from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD
        from plexora.server.utils import dicom_wsi, ome_zarr, xenium_focus

        spec = dataset.project.image
        # Only when it is there: the viewer rebuilds missing derived levels on
        # open (LocalImageProvider._missing_pyramid), which is minutes of work
        # a render must not set off. Without them the source's own levels are
        # read, which is fewer levels and the same pixels.
        from pathlib import Path

        extension = spec.pyramid if spec.pyramid and Path(spec.pyramid).exists() else None
        as_colour = spec.kind == IMAGE_TYPE_BRIGHTFIELD
        pyramid = None
        if xenium_focus.is_focus_dir(source.path):
            pyramid = xenium_focus.open_focus(source.path, extension=extension)
        elif ome_zarr.is_zarr_image_path(source.path):
            pyramid = ome_zarr.open_image(source.path, extension=extension)
        elif dicom_wsi.is_dicom_path(source.path):
            pyramid = dicom_wsi.open_image(source.path, extension=extension, rgb=as_colour)
        elif as_colour or brightfield.is_rgb_layout(source.path):
            pyramid = brightfield.open_rgb(source.path, extension=extension)
        if pyramid is not None:
            self._file = None
            self._pyramid = pyramid
            self._zarr = pyramid
            self._is_array = False
            self.levels = len(pyramid)
            self._level_shapes = [tuple(shape) for shape in pyramid.level_shapes]
            return

        # Axes-aware, the same read the viewer's own path takes: a figure
        # exported from a hyperstack has to be the panel the user was looking
        # at, not a different slicing of the same file.
        from plexora.server.utils import tiff_series

        self._file = tifffile.TiffFile(source.path, is_ome=False)
        self._zarr = zarr.open(
            tiff_series.channel_series(self._file).aszarr(), mode="r")
        self._is_array = hasattr(self._zarr, "shape")
        self.levels = 1 if self._is_array else len(list(self._zarr))
        self._level_shapes = None

    def close(self):
        pyramid = getattr(self, "_pyramid", None)
        if pyramid is not None:
            # DICOM holds a WsiDicom handle; a focus folder holds one TIFF
            # handle per channel file. Both lazily read through them, so they
            # are released here rather than left to the garbage collector --
            # on Windows a held handle keeps the file from being deleted.
            closer = getattr(pyramid, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            for handle in getattr(pyramid, "_handles", ()) or ():
                try:
                    handle.close()
                except Exception:
                    pass
            self._pyramid = None
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
        height, width = array.shape[-2], array.shape[-1]

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
        # Channel and rectangle in ONE subscript. `array[channel]` first would
        # be the whole plane: free on a zarr view of a TIFF, but a full-level
        # decode on the lazy DICOM and OME-Zarr levels, for a panel that needs
        # a few hundred pixels of it.
        if array.ndim == 3:
            return np.asarray(array[channel_index, y0:y1, x0:x1]), (x0, y0, x1, y1)
        return np.asarray(array[y0:y1, x0:x1]), (x0, y0, x1, y1)

    def read_rgb(self, level, box):
        """A rectangle of a brightfield image, as (H, W, 3) uint8.

        The colour counterpart of `read`, and the only place in this module
        that returns three samples at once. It exists because there is nothing
        to composite: a brightfield panel is the pixels, so putting them
        through the per-channel colorize loop would be three passes to arrive
        back where it started.
        """
        plane = self.level(level)
        height, width = plane.shape[-2], plane.shape[-1]
        x0 = max(0, min(int(math.floor(box[0])), width))
        y0 = max(0, min(int(math.floor(box[1])), height))
        x1 = max(x0, min(int(math.ceil(box[2])), width))
        y1 = max(y0, min(int(math.ceil(box[3])), height))
        if x1 <= x0 or y1 <= y0:
            return np.full((1, 1, 3), 255, dtype=np.uint8)
        if (x1 - x0) * (y1 - y0) > MAX_SOURCE_PIXELS:
            raise RenderError(
                "this panel covers more of the image than one render can read; "
                "export it at a lower DPI"
            )
        if hasattr(plane, "rgb"):
            return np.asarray(plane.rgb[y0:y1, x0:x1])
        # A colour image whose levels are plain (3, y, x) planes -- an RGB
        # OME-Zarr store. Stacked the same way `.rgb` would present them.
        return np.stack([np.asarray(plane[c, y0:y1, x0:x1]) for c in range(3)], axis=-1)


def choose_level(source, viewport_width, target_pixels):
    """The cheapest pyramid level that still has the detail being asked for.

    The largest level index whose pixels across the region still meet or exceed
    the target, so a 400-pixel-wide panel of a whole slide reads a few hundred
    kilobytes instead of the level-0 gigabyte that would be downsampled away.
    Level 0 when nothing else is big enough, which is also when the warning
    below applies.

    This is the rule for a RENDER: it never hands back fewer pixels than were
    asked for, because an export is the deliverable and it is written once. A
    view being panned wants the other trade -- see `choose_view_level`.
    """
    best = 0
    for level in range(max(1, source.levels)):
        if viewport_width / (2 ** level) >= target_pixels:
            best = level
        else:
            break
    return best


#: How far short of the pixels it is showing an interactive view may fall
#: before it takes the next finer level. 1/sqrt(2) is the geometric midpoint
#: between two pyramid levels, which turns "never fewer pixels than asked for"
#: into "the NEAREST level" -- see choose_view_level.
VIEW_DETAIL = 0.7071


def choose_view_level(source, viewport_width, target_pixels):
    """The level to LOOK at, as against the level to publish.

    `choose_level` steps to the finer level the moment a coarser one would have
    fewer pixels than the caller asked for. On a screen that means level 0 is
    read for any scale below 2:1 -- and at 1.5:1 that is four 1024x1024 tiles
    of a deflate-compressed slide, measured at 387ms, to produce 636 pixels
    that the resample on the way out immediately reduces to 420. The half
    pixel of extra detail is decoded and thrown away.

    So this one allows a level to be short of the target by VIEW_DETAIL: the
    coarser level is taken when it is the NEARER of the two in the ratio sense.
    The cost is an upsample of at most 1.41x on a view a few hundred pixels
    across; the saving is the 3-4x less decoding that is the difference between
    a mini view that keeps up with a drag and one that does not. At 1:1 and
    above the answer is still level 0, because there the finest level is what
    the zoom is actually asking to see.
    """
    return choose_level(source, viewport_width, target_pixels * VIEW_DETAIL)


def channel_key(channel) -> str:
    """The stable URL key of an image channel record: the last path segment of
    its tile `src`, which is what the tile route and a figure scene both use."""
    return str(channel.get("src") or "").rstrip("/").rsplit("/", 1)[-1]


def composite(source, channels, level, box):
    """The box's pixels composited, as (H, W, 3) uint8, with background where
    the box runs off the image. Returns (pixels, channels_rendered, background).

    `channels` is a list of `{key, color: {r, g, b}, window: [lo, hi],
    visible}` -- the shape a figure scene records. Padded rather than clipped:
    the caller asked for THIS box, and a raster that shrank to the part of it
    that exists would move the middle of the picture. The background is what
    the viewer draws there: black under fluorescence, white under brightfield.
    """
    x0, y0, x1, y1 = box
    height, width = y1 - y0, x1 - x0

    if source.is_brightfield:
        plane = source.level(level)
        full_h, full_w = plane.shape[-2], plane.shape[-1]
        canvas = np.full((height, width, 3), 255, dtype=np.uint8)
        cx0, cy0 = max(0, min(x0, full_w)), max(0, min(y0, full_h))
        cx1, cy1 = max(cx0, min(x1, full_w)), max(cy0, min(y1, full_h))
        if cx1 > cx0 and cy1 > cy0:
            block = source.read_rgb(level, (cx0, cy0, cx1, cy1))
            canvas[cy0 - y0:cy0 - y0 + block.shape[0],
                   cx0 - x0:cx0 - x0 + block.shape[1]] = block[:height, :width]
        return canvas, 1, 255

    accumulator = np.zeros((height, width, 3), dtype=np.float32)
    rendered = 0
    for channel in channels or []:
        if not channel.get("visible", True):
            continue
        index = source.channel_index(channel["key"])
        if index is None:
            continue
        plane, clipped = source.read(index, level, box)
        low, high = float(channel["window"][0]), float(channel["window"][1])
        span = high - low
        if span <= 0:
            continue
        # Where the part that exists sits inside the box. Trimmed to the box,
        # because pyramid levels can disagree with each other by a pixel.
        top, left = clipped[1] - y0, clipped[0] - x0
        plane = plane[:max(0, height - top), :max(0, width - left)]
        if plane.size == 0 or top < 0 or left < 0:
            continue
        scaled = np.clip((plane.astype(np.float32) - low) / span, 0.0, 1.0)
        colour = channel["color"]
        weight = CHANNEL_ALPHA / 255.0
        target = accumulator[top:top + scaled.shape[0], left:left + scaled.shape[1]]
        for offset, key in enumerate(("r", "g", "b")):
            value = float(colour[key])
            if value:
                target[..., offset] += scaled * (value * weight)
        rendered += 1
    return (np.clip(accumulator, 0.0, 1.0) * 255.0).astype(np.uint8), rendered, 0


#: How many pixels the coarsest level is sampled down to for a channel summary.
STATS_SAMPLE = 512


def channel_stats(source, channel_key):
    """min/max and the p01/p999 auto window of one channel, from the COARSEST
    level -- the percentiles of a whole slide are the same to within noise at
    any level, and reading level 0 for them would be a gigabyte."""
    index = source.channel_index(channel_key)
    if index is None:
        raise RenderError(f"{channel_key!r} is not a channel of this image")
    level = max(0, source.levels - 1)
    height, width = source.level_shape(level)
    if height * width > MAX_SOURCE_PIXELS:
        raise RenderError("this image has no level small enough to summarise")
    sample, _clipped = source.read(index, level, (0, 0, width, height))
    sample = np.asarray(sample)
    dtype = str(sample.dtype)
    step = max(1, int(max(sample.shape) / STATS_SAMPLE))
    sample = sample[::step, ::step].astype(np.float32)
    if sample.size == 0:
        return {"min": 0.0, "max": 1.0, "p01": 0.0, "p999": 1.0,
                "dtype": dtype, "level": level}
    low, high = np.percentile(sample, [1.0, 99.9])
    return {"min": float(sample.min()), "max": float(sample.max()),
            "p01": float(low), "p999": float(high),
            "dtype": dtype, "level": level}


#: How many source files the shared shelf keeps open.
READER_LIMIT = 4


class _Held:
    __slots__ = ("source", "identity", "lock")

    def __init__(self, source, identity):
        self.source = source
        self.identity = identity
        self.lock = threading.Lock()


class ReaderShelf:
    """A few open `SourceImage`s, least-recently-used first.

    Keyed by project name and checked against an identity (the image path, or
    the node and resource) on every use, so a project repointed at another file
    is never read through a reader for the old one. Each reader has its own
    lock -- tifffile is not safe to read through one handle from two threads --
    and the map lock is never held across a read.
    """

    def __init__(self, limit=READER_LIMIT):
        self.limit = limit
        self._held: "OrderedDict[str, _Held]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def identity_of(dataset) -> str | None:
        image = dataset.image
        if not image.is_local:
            locator = image.locator
            return f"node://{locator.node}/{locator.resource_id}"
        source = image.source
        return str(source.path) if source is not None and source.path else None

    @contextlib.contextmanager
    def reader(self, dataset):
        """The open reader for a handle set, exclusive for the block."""
        identity = self.identity_of(dataset)
        with self._lock:
            held = self._held.pop(dataset.name, None)
            if held is not None and held.identity != identity:
                with held.lock:
                    held.source.close()
                held = None
            if held is None:
                held = _Held(SourceImage(dataset), identity)
            self._held[dataset.name] = held
            while len(self._held) > self.limit:
                _, evicted = self._held.popitem(last=False)
                with evicted.lock:
                    evicted.source.close()
        with held.lock:
            yield held.source

    def close(self):
        with self._lock:
            while self._held:
                _, held = self._held.popitem()
                with held.lock:
                    held.source.close()


#: The process's shared shelf, for readers outside Figure Builder.
SHELF = ReaderShelf()


def close_readers():
    """Close every reader the shared shelf holds. For shutdown and tests."""
    SHELF.close()
