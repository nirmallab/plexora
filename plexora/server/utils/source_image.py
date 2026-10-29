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


def open_project_volume(project):
    """A project's image opened as a volume the way its viewer opens it, or None.

    The project's z/t reading (a stack's z axis is depth or imaging cycles --
    a decision, see volume/sources/tiff.py), its derived coarse levels when
    they are on disk, and any spacing somebody typed laid over what the file
    states (a section's row height follows the z step). None for a planar
    image, which every 2-D reader keeps serving as before. The caller closes
    what comes back.
    """
    from pathlib import Path

    from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD
    from plexora.server.volume.sources import is_volume_only_path, open_volume

    spec = project.image
    if not spec.src:
        return None
    extension = spec.pyramid if spec.pyramid and Path(spec.pyramid).exists() else None
    source = open_volume(spec.src, extension=extension,
                         rgb=spec.kind == IMAGE_TYPE_BRIGHTFIELD,
                         z_semantics=spec.z_semantics, t_semantics=spec.t_semantics)
    if source is None:
        return None
    if source.volume.is_planar and not is_volume_only_path(spec.src):
        source.close()
        return None
    from dataclasses import replace

    spacing = source.volume.spacing.overlaid(
        pixel_size=spec.pixel_size, pixel_size_z=spec.pixel_size_z,
        frame_interval=spec.frame_interval, frames=source.volume.frames)
    if spacing is not source.volume.spacing:
        source.volume = replace(source.volume, spacing=spacing)
    return source


class SourceImage:
    """One image file, opened once and read from many times.

    Held open across a whole export rather than reopened per panel: a figure is
    routinely eight panels from one slide, and reopening a pyramidal TIFF eight
    times is eight directory walks for the same answer.

    **Views.** `view` reads one view of a volume -- a plane (`z12`), a slab
    projection (`z4-19max`) or a section (`xzy1200`), in the token grammar of
    plexora/server/volume/viewspec.py -- through the same `[channel, rows,
    cols]` levels, so `read` and everything built on it need no change. A
    section's levels are its own shape (rows are z). A volume opened without a
    view reads its default plane, the one the viewer opens on. A view of a
    planar image is a RenderError, never the image under the view's name.
    `provenance(level, channels)` is what a picture read from here says it is.
    """

    # Read exactly unless asked otherwise, for a reader set up field by field
    # (a stand-in over an in-memory stack) as much as for one `__init__` built.
    _budgeted = False
    spec = None
    coarsened = 0

    def __init__(self, datasource, view=None, *, budgeted=False):
        """`budgeted`: a projection or section read keeps to the tile routes'
        budgets (`plane_view.SERVED_SLAB_BYTES`, `SECTION_READ_BUDGET`) --
        computed at a coarser level and drawn up past them, `coarsened` saying
        by how many levels -- instead of reading every plane of a gigabyte
        rectangle. For an agent's render; an export reads exactly."""
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
        #: The volume this reads a view of, its `ViewSpec`, and the open source
        #: (local only) -- all None for a planar image.
        self.volume = None
        self.spec = None
        self._volume_source = None
        self._budgeted = bool(budgeted)
        #: Levels coarser than asked the most coarsened read was computed at
        #: (0: every read exact). Only a budgeted view read coarsens.
        self.coarsened = 0
        wanted = "" if view is None else str(view).strip()
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
            # A volume reads its default plane without a view, as locally; the
            # node is told which plane either way (providers/node.py).
            described = self._remote.volume()
            if wanted or described is not None:
                self._choose_view(described, wanted)
            geometry = self._remote.geometry()
            self.levels = max(1, int(geometry.get("levels") or 1))
            self._level_shapes = [tuple(shape) for shape
                                  in (geometry.get("level_shapes") or [])]
            if self.spec is not None and self.spec.kind == "section":
                # Rows are z: a section's levels are not the planes' shapes.
                from plexora.server.volume.plane_view import section_geometry

                self._level_shapes = [tuple(shape) for shape in
                                      section_geometry(self.volume, self.spec)["level_shapes"]]
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
        if wanted or spec.is_volumetric:
            # A volume is read through its views, as the viewer reads it; a
            # planar image falls through to the readers below, untouched.
            if self._open_view(dataset.project, wanted):
                return
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

    def _choose_view(self, described, wanted):
        """Settle which view is read: `described` is a `Volume`, its
        `to_dict()` (a node-backed image's, via `ImageHandle.volume`), or None
        for a planar image."""
        from plexora.server.volume.model import Volume
        from plexora.server.volume.viewspec import BadView, ViewSpec

        volume = (described if isinstance(described, Volume) or described is None
                  else Volume.from_dict(described))
        if volume is None or volume.is_planar:
            raise RenderError(
                f"{self.datasource}'s image has a single plane, so it has no view "
                f"{wanted!r} to read")
        try:
            spec = ViewSpec.parse(wanted, volume)
        except BadView as exc:
            raise RenderError(f"{self.datasource}: {exc}") from None
        self.volume = volume
        self.spec = spec

    def _open_view(self, project, wanted):
        """Open the local volume and its view's pyramid; False when planar."""
        from plexora.server.volume.model import is_single_array
        from plexora.server.volume.plane_view import plane_pyramid

        source = open_project_volume(project)
        if source is None:
            if wanted:
                raise RenderError(
                    f"{self.datasource}'s image has a single plane, so it has no view "
                    f"{wanted!r} to read")
            return False
        try:
            self._choose_view(source.volume, wanted)
            pyramid = plane_pyramid(source, self.spec, served=self._budgeted)
        except Exception:
            source.close()
            raise
        self._volume_source = source
        self._zarr = pyramid
        self._is_array = is_single_array(pyramid)
        if self._is_array:
            self.levels = 1
            self._level_shapes = [(int(pyramid.shape[-2]), int(pyramid.shape[-1]))]
        else:
            keys = sorted(list(pyramid), key=int)
            self.levels = len(keys)
            self._level_shapes = [(int(pyramid[key].shape[-2]), int(pyramid[key].shape[-1]))
                                  for key in keys]
        return True

    def at_plane(self, t: int, z: int):
        """A reader of plane (t, z) of the same volume, borrowing this one's open
        source (closing it leaves the source open): how a run over many planes
        -- an outline carried through a stack -- reads them without opening the
        volume once per plane. A node-backed image just asks for that plane."""
        import copy

        from plexora.server.volume.model import is_single_array
        from plexora.server.volume.viewspec import ViewSpec

        if self.volume is None:
            raise RenderError(f"{self.datasource}'s image has a single plane")
        token = (f"t{int(t)}" if self.volume.frames > 1 else "") + f"z{int(z)}"
        twin = copy.copy(self)
        twin.__dict__.pop("_level_arrays", None)      # the parent plane's arrays
        twin.spec = ViewSpec.parse(token, self.volume)
        twin._volume_source = None
        twin._pyramid = None
        twin._file = None
        if self._volume_source is not None:
            pyramid = self._volume_source.plane_pyramid(twin.spec.t, twin.spec.z)
            twin._zarr = pyramid
            twin._is_array = is_single_array(pyramid)
        return twin

    @property
    def view(self):
        """The canonical token of the view read (`z12`, `z4-19max`), or None
        for a planar image."""
        return self.spec.token() if self.spec is not None else None

    def provenance(self, level, channels=()):
        """What a picture read from here at `level` is: the view, its planes,
        the spacing, and whether it is exact (a slab is only at level 0) --
        `volume.provenance.provenance_for`. None for a planar image, which is
        a picture of itself."""
        if self.spec is None:
            return None
        from plexora.server.volume.provenance import provenance_for

        return provenance_for(self.datasource, self.spec, level=level, channels=channels,
                              volume=self.volume, produced_by="region")

    def close(self):
        self.__dict__.pop("_level_arrays", None)
        source = getattr(self, "_volume_source", None)
        if source is not None:
            # The view's levels read through the source's handles; it owns them.
            self._volume_source = None
            try:
                source.close()
            except Exception:
                pass
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
        # A zarr group lookup per read costs as much as decoding a small
        # rectangle: each level's array is looked up once per reader.
        levels = self.__dict__.setdefault("_level_arrays", {})
        found = levels.get(index)
        if found is None:
            found = levels[index] = self._zarr[str(index)]
        return found

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
                    [channel_index], max_pixels=MAX_SOURCE_PIXELS,
                    **({"view": self.view} if self.spec is not None else {}))
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
            if self._budgeted and self.spec is not None:
                from plexora.server.volume.plane_view import served_steps

                served_steps()
                block = np.asarray(array[channel_index, y0:y1, x0:x1])
                self.coarsened = max(self.coarsened, served_steps())
                return block, (x0, y0, x1, y1)
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
    visible, curve?}` -- the shape a figure scene records (`curve` as
    `display_curve`; none is linear). Padded rather than clipped:
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
        blend_plane(accumulator, plane, (low, high), channel["color"], top=top, left=left,
                    curve=channel.get("curve"))
        rendered += 1
    return finish(accumulator), rendered, 0


def blend_plane(accumulator, plane, window, colour, *, top=0, left=0, curve=None):
    """Add one channel's plane into a float32 (H, W, 3) accumulator, in place.

    frag.glsl's arithmetic, once: the display curve (`display_curve.transfer`:
    linear `t = clip((raw - lo) / (hi - lo), 0, 1)` unless `curve` says
    asinh), times the channel colour and CHANNEL_ALPHA, accumulated as the
    canvas's `lighter` blend does. `colour` is `{r, g, b}` in 0-255. Split out
    of `composite` so a caller that already holds the planes (a batch of cell
    crops read one tile at a time) blends them exactly as a render would.
    """
    from plexora.server.utils import display_curve

    low, high = float(window[0]), float(window[1])
    span = high - low
    if span <= 0:
        return accumulator
    scaled = display_curve.transfer(plane, (low, high), curve)
    weight = CHANNEL_ALPHA / 255.0
    target = accumulator[top:top + scaled.shape[0], left:left + scaled.shape[1]]
    scaled = scaled[:target.shape[0], :target.shape[1]]
    for offset, key in enumerate(("r", "g", "b")):
        value = float(colour[key])
        if value:
            target[..., offset] += scaled * (value * weight)
    return accumulator


def finish(accumulator):
    """The accumulator as the (H, W, 3) uint8 picture the viewer shows."""
    return (np.clip(accumulator, 0.0, 1.0) * 255.0).astype(np.uint8)


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
#: How many readers one of them may have open at once: one file handle each,
#: read by one thread at a time, so that rectangles read together (a panel's
#: cells, two markers' panels) are decoded side by side, not one after another.
READERS_PER_IMAGE = 4


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
    and the map lock is never held across a read. A key holds up to
    `per_image` readers of the same file: a second thread asking while the
    first reads gets one of its own rather than waiting.
    """

    def __init__(self, limit=READER_LIMIT, per_image=READERS_PER_IMAGE):
        self.limit = limit
        self.per_image = max(1, int(per_image))
        self._held: "OrderedDict[str, list[_Held]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def identity_of(dataset) -> str | None:
        image = dataset.image
        if not image.is_local:
            locator = image.locator
            return f"node://{locator.node}/{locator.resource_id}"
        source = image.source
        return str(source.path) if source is not None and source.path else None

    @staticmethod
    def _open(dataset, view):
        return SourceImage(dataset, view=view) if view else SourceImage(dataset)

    @contextlib.contextmanager
    def reader(self, dataset, view=None):
        """An open reader for a handle set, exclusive for the block.

        `view` is a plane or projection of a z-stack (volume/viewspec.py);
        each view a caller reads is its own reader, so a flat image and the
        default view keep the one key they always had.
        """
        identity = self.identity_of(dataset)
        key = dataset.name if not view else f"{dataset.name}\x00{view}"
        closing = []
        with self._lock:
            helds = self._held.pop(key, None)
            if helds and helds[0].identity != identity:
                closing.extend(helds)
                helds = None
            if not helds:
                helds = [_Held(self._open(dataset, view), identity)]
            self._held[key] = helds
            while len(self._held) > self.limit:
                _, evicted = self._held.popitem(last=False)
                closing.extend(evicted)
            held = next((h for h in helds if h.lock.acquire(blocking=False)), None)
            if held is None and len(helds) < self.per_image:
                held = _Held(self._open(dataset, view), identity)
                held.lock.acquire()
                helds.append(held)
        for gone in closing:
            with gone.lock:
                gone.source.close()
        if held is None:
            # Every reader of this image is busy: wait for the first.
            held = helds[0]
            held.lock.acquire()
        try:
            yield held.source
        finally:
            held.lock.release()

    def close(self):
        with self._lock:
            helds = [h for group in self._held.values() for h in group]
            self._held.clear()
        for held in helds:
            with held.lock:
                held.source.close()


#: The process's shared shelf, for readers outside Figure Builder.
SHELF = ReaderShelf()


def close_readers():
    """Close every reader the shared shelf holds. For shutdown and tests."""
    SHELF.close()
