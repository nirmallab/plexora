"""Reading rectangles out of a TIFF at a web address, tile by tile.

A local TIFF is read through tifffile's zarr view (`series.aszarr()`), and
that is still how every file on disk is read -- nothing here is on that path.
It cannot be used for a TIFF streamed from a web address: the zarr view reads
the file *inside* zarr's IO loop, and a streamed read has to wait on that same
loop for its bytes, so it would wait on itself forever. (`RemoteFile` refuses
such a read with an error rather than hanging.)

So a remote TIFF is read the way tifffile reads a page itself, one level
down: work out which tiles (or strips) a rectangle touches from the page's
own offsets, fetch exactly those byte ranges -- all of them in one concurrent
request through the chunk cache -- and decode each with the page's own
decoder (`TiffPage.decode`, the closure tifffile's `segments()` uses). The
read happens on the calling thread, never on the loop.

On top of that sit the same two pyramid shapes every other reader in Plexora
produces: `open_image` returns levels indexed `[channel, rows, cols]` (the
`dicom_wsi._MonoLevel` views, over a virtual halving chain), and
`rgb_sources` hands `brightfield.open_rgb` the native levels of a colour
slide. Tile encoding, quantisation, the overview and `build_extension` never
learn the bytes came over a network.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

#: tifffile's PLANARCONFIG.SEPARATE: one plane per sample.
_SEPARATE = 2


# -- opening ---------------------------------------------------------------


def _is_remote(path) -> bool:
    from plexora.server.providers.base import is_remote_locator

    return is_remote_locator(path)


def open_tiff(path):
    """`tf.TiffFile` for a local path or a web address.

    Local: exactly `tf.TiffFile(str(path), is_ome=False)`, the call every
    reader already makes. Remote: the same over a `RemoteFile`, named after the
    file (tifffile tells an NDPI from a TIFF by its extension) and read as a
    single file -- a multi-file OME-TIFF's companions are not followed, the
    same as on disk.
    """
    import tifffile as tf

    if not _is_remote(path):
        return tf.TiffFile(str(path), is_ome=False)
    from plexora.server.utils import remote_store

    store, key = remote_store.open_store(path)
    handle = remote_store.RemoteFile(store, key)
    try:
        tiff = tf.TiffFile(handle, name=remote_store.url_name(path), size=handle.size,
                           is_ome=False, _multifile=False)
    except Exception:
        handle.close()
        raise
    # Page parsing seeks and reads; tiles below are read position-free.
    tiff.filehandle.set_lock(True)
    tiff._plexora_stream = handle
    return tiff


def close_tiff(tiff) -> None:
    """Close a TiffFile from `open_tiff`, and its stream when it has one."""
    if tiff is None:
        return
    try:
        tiff.close()
    except Exception:  # noqa: BLE001
        pass
    stream = getattr(tiff, "_plexora_stream", None)
    if stream is not None:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _fetcher(tiff):
    """`fetch([(offset, count)]) -> [bytes]` for this file."""
    stream = getattr(tiff, "_plexora_stream", None)
    if stream is not None:
        return stream.pread_many
    fh = tiff.filehandle
    lock = threading.RLock()

    def local(spans):
        out = []
        with lock:
            for offset, count in spans:
                fh.seek(int(offset))
                out.append(fh.read(int(count)))
        return out

    return local


# -- one page, one rectangle ------------------------------------------------


def _clip(start, stop, extent) -> tuple[int, int]:
    start = max(0, min(int(start), int(extent)))
    stop = max(start, min(int(stop), int(extent)))
    return start, stop


def read_region(page, y0: int, y1: int, x0: int, x1: int, *, sample=None,
                fetch=None) -> np.ndarray:
    """Pixels `[y0:y1, x0:x1]` of one TIFF page, touching only its tiles.

    `(h, w)` for a one-sample page or one `sample` of a planar one, `(h, w, S)`
    for interleaved samples (and for a planar page when `sample` is None).
    `fetch` is `[(offset, count)] -> [bytes]`; the page's own file handle when
    not given. Empty segments read as the page's `nodata` (0 by default).
    """
    key = page.keyframe
    height, width = int(key.imagelength), int(key.imagewidth)
    y0, y1 = _clip(y0, y1, height)
    x0, x1 = _clip(x0, x1, width)
    samples = int(key.samplesperpixel or 1)
    planar = int(key.planarconfig or 1) == _SEPARATE and samples > 1
    contig = 1 if planar else samples
    if planar and sample is None:
        planes = [read_region(page, y0, y1, x0, x1, sample=s, fetch=fetch)
                  for s in range(samples)]
        return np.stack(planes, axis=-1)
    dtype = np.dtype(key.dtype)
    fill = getattr(key, "nodata", 0) or 0
    out = np.full((y1 - y0, x1 - x0, contig), fill, dtype=dtype)
    if y1 <= y0 or x1 <= x0:
        return out[..., 0] if contig == 1 else out
    if int(getattr(key, "imagedepth", 1) or 1) > 1:
        raise ValueError("volumetric TIFF tiles are not supported at a web address")

    if key.is_tiled:
        tile_h, tile_w = int(key.tilelength), int(key.tilewidth)
    else:
        tile_h = min(int(key.rowsperstrip or height), height) or height
        tile_w = width
    ncol = math.ceil(width / tile_w)
    nrow = math.ceil(height / tile_h)
    plane = int(sample) if planar else 0
    first = plane * nrow * ncol
    indices = [first + r * ncol + c
               for r in range(y0 // tile_h, (y1 - 1) // tile_h + 1)
               for c in range(x0 // tile_w, (x1 - 1) // tile_w + 1)]

    offsets = page.dataoffsets
    counts = page.databytecounts
    wanted = [i for i in indices if i < len(offsets) and counts[i] and offsets[i]]
    if fetch is None:
        fetch = _fetcher(page.parent)
    blobs = dict(zip(wanted, fetch([(offsets[i], counts[i]) for i in wanted])))

    decode = key.decode
    jpegtables = getattr(page, "jpegtables", None)
    jpegheader = getattr(key, "jpegheader", None)
    for index in indices:
        r, c = divmod(index - first, ncol)
        ty, tx = r * tile_h, c * tile_w
        data = blobs.get(index)
        if not data:
            continue
        segment, _, _ = decode(data, index, jpegtables=jpegtables,
                               jpegheader=jpegheader, _fullsize=True)
        if segment is None:
            continue
        segment = np.asarray(segment).reshape(-1, tile_h, tile_w, contig)[0]
        sy0, sy1 = max(y0, ty), min(y1, ty + tile_h)
        sx0, sx1 = max(x0, tx), min(x1, tx + tile_w)
        if sy1 <= sy0 or sx1 <= sx0:
            continue
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = \
            segment[sy0 - ty:sy1 - ty, sx0 - tx:sx1 - tx]
    return out[..., 0] if contig == 1 else out


# -- one level of the channel series ----------------------------------------


class TiffLevel:
    """One native level of a TIFF's channel series, read by rectangle.

    Three layouts, one face. `pages`: a page per channel, how an OME-TIFF
    stack is written. `separate`: one planar page whose samples are the
    channels. `contig`: one page of interleaved samples -- a colour slide.
    """

    def __init__(self, level, fetch):
        self._fetch = fetch
        pages = [page for page in level.pages]
        if not pages or any(page is None for page in pages):
            raise ValueError("a TIFF level with missing pages cannot be streamed")
        axes = str(getattr(level, "axes", "") or "")
        shape = tuple(int(d) for d in level.shape)
        first = pages[0].keyframe
        self.dtype = np.dtype(level.dtype)
        self.height = int(first.imagelength)
        self.width = int(first.imagewidth)
        samples = int(first.samplesperpixel or 1)
        if axes.endswith("S") or (samples > 1 and int(first.planarconfig or 1) != _SEPARATE):
            self.layout, self.count = "contig", samples
        elif samples > 1 and len(pages) == 1:
            self.layout, self.count = "separate", samples
        elif samples == 1:
            self.layout, self.count = "pages", len(pages)
        else:
            raise ValueError(
                f"a TIFF laid out as {axes or shape} cannot be streamed from a web address")
        self._pages = pages

    def read(self, channel: int, y0, y1, x0, x1) -> np.ndarray:
        """One channel's `(h, w)` plane."""
        channel = int(channel)
        if self.layout == "pages":
            return read_region(self._pages[channel], y0, y1, x0, x1, fetch=self._fetch)
        if self.layout == "separate":
            return read_region(self._pages[0], y0, y1, x0, x1, sample=channel,
                               fetch=self._fetch)
        return np.ascontiguousarray(
            read_region(self._pages[0], y0, y1, x0, x1, fetch=self._fetch)[..., channel])

    def read_rgb(self, y0, y1, x0, x1) -> np.ndarray:
        """`(h, w, 3)` of the first three samples or pages."""
        if self.layout == "contig":
            block = read_region(self._pages[0], y0, y1, x0, x1, fetch=self._fetch)
            if block.ndim == 2:
                block = np.repeat(block[..., None], 3, axis=-1)
            return np.ascontiguousarray(block[..., :3])
        planes = [self.read(channel, y0, y1, x0, x1)
                  for channel in range(min(3, self.count))]
        while len(planes) < 3:
            planes.append(planes[-1])
        return np.ascontiguousarray(np.stack(planes, axis=-1))


class _PlaneSource:
    """One channel of a `TiffLevel`, as `dicom_wsi._MonoLevel` reads sources."""

    __slots__ = ("_level", "_channel", "height", "width")

    def __init__(self, level: TiffLevel, channel: int):
        self._level = level
        self._channel = int(channel)
        self.height, self.width = level.height, level.width

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        return self._level.read(self._channel, y0, y1, x0, x1)


class _RgbSource:
    """A `TiffLevel` as `brightfield._Level` reads a native source."""

    __slots__ = ("_level", "height", "width")

    def __init__(self, level: TiffLevel):
        self._level = level
        self.height, self.width = level.height, level.width

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        block = self._level.read_rgb(y0, y1, x0, x1)
        if block.dtype != np.uint8:
            block = block.astype(np.uint8, copy=False)
        return block


def _native_levels(tiff, series=None) -> list[TiffLevel]:
    """The channel series' levels, finest first."""
    from plexora.server.utils import tiff_series

    series = tiff_series.channel_series(tiff) if series is None else series
    fetch = _fetcher(tiff)
    levels = [TiffLevel(level, fetch)
              for level in (getattr(series, "levels", None) or [series])]
    levels.sort(key=lambda level: -level.width)
    return levels


def rgb_sources(path):
    """`(sources, handle)` for `brightfield.open_rgb`, finest first."""
    tiff = open_tiff(path)
    try:
        import tifffile  # noqa: F401 -- the series below needs it loaded

        series = tiff.series[0]
        fetch = _fetcher(tiff)
        levels = [TiffLevel(level, fetch)
                  for level in (getattr(series, "levels", None) or [series])]
        levels.sort(key=lambda level: -level.width)
        return [_RgbSource(level) for level in levels], _Closer(tiff)
    except Exception:
        close_tiff(tiff)
        raise


class _Closer:
    """The handle `RgbPyramid` holds: closing it closes the file and stream."""

    def __init__(self, tiff):
        self._tiff = tiff

    def close(self):
        tiff, self._tiff = self._tiff, None
        close_tiff(tiff)


# -- the channel pyramid -----------------------------------------------------


class TiffPyramid:
    """A remote TIFF's channel levels, shaped like every other pyramid here.

    No `.shape`, `pyramid[str(level)]`, levels `[channel, rows, cols]` -- the
    contract `DicomPyramid` and `NgffPyramid` keep, for the same reasons.
    """

    is_color = False

    def __init__(self, levels, *, path=None, extension=None,
                 base_levels: Optional[int] = None, handle=None):
        self._levels = list(levels)
        self.path = str(path) if path is not None else None
        self.extension = str(extension) if extension is not None else None
        self.base_levels = len(self._levels) if base_levels is None else base_levels
        self._handle = handle

    def __len__(self) -> int:
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key) -> bool:
        try:
            index = int(key)
        except (TypeError, ValueError):
            return False
        return 0 <= index < len(self._levels)

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    @property
    def level_shapes(self) -> list[list[int]]:
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]

    def close(self):
        tiff, self._handle = self._handle, None
        close_tiff(tiff)


def open_image(path, extension=None, rgb: bool = False) -> TiffPyramid:
    """The TIFF at `path` as channel levels over a virtual halving chain.

    The shape `dicom_wsi.open_image` produces, from the same pieces: each
    level reads the nearest native one and resamples in flight, and a file
    written flat gets its coarse levels from `extension` once
    `build_extension` has derived them. `rgb` is accepted for signature
    parity; a colour reading goes through `brightfield.open_rgb`.
    """
    from plexora.server.utils import brightfield, dicom_wsi

    tiff = open_tiff(path)
    try:
        natives = _native_levels(tiff)
        finest = natives[0]
        dtype = finest.dtype
        levels: list[Any] = []
        for height, width in brightfield._dyadic_shapes(finest.height, finest.width):
            native = brightfield._pick_source(natives, height, width)
            if levels and not brightfield._affordable(native, height, width):
                break
            levels.append(dicom_wsi._MonoLevel(
                [_PlaneSource(native, c) for c in range(native.count)],
                height, width, dtype))
        base_levels = len(levels)
        if extension and Path(extension).exists():
            import zarr

            derived = zarr.open_group(str(extension), mode="r")
            index = base_levels
            while str(index) in derived:
                array = derived[str(index)]
                levels.append(dicom_wsi._MonoLevel(
                    [dicom_wsi._ZarrSource(array, channel)
                     for channel in range(int(array.shape[0]))],
                    int(array.shape[-2]), int(array.shape[-1]), dtype))
                index += 1
        return TiffPyramid(levels, path=path, extension=extension,
                           base_levels=base_levels, handle=tiff)
    except Exception:
        close_tiff(tiff)
        raise


def needs_extension(pyramid) -> bool:
    from plexora.server.utils import brightfield

    height, width = pyramid.level_shapes[0]
    return len(pyramid) < len(brightfield._dyadic_shapes(height, width))


def geometry(pyramid) -> dict:
    from plexora.server.utils import dicom_wsi

    return dicom_wsi.geometry(pyramid)


def overview_plane(pyramid, minimum: int = 200, maximum: int = 400) -> np.ndarray:
    from plexora.server.utils import dicom_wsi

    return dicom_wsi.overview_plane(pyramid, minimum, maximum)


def focal_planes(path) -> tuple[int, int]:
    """`tiff_series.focal_planes` for a local or remote TIFF."""
    from plexora.server.utils import tiff_series

    tiff = open_tiff(path)
    try:
        return tiff_series.focal_planes(tiff)
    finally:
        close_tiff(tiff)


#: A thumbnail read is refused past this many pixels at the coarsest level:
#: a flat slide's "coarsest level" is the slide.
_THUMBNAIL_MAX_PIXELS = 4096 * 4096


def thumbnail(path, longest: int = 512) -> Optional[np.ndarray]:
    """What `brightfield._thumbnail` returns, for a remote TIFF, or None."""
    tiff = None
    try:
        tiff = open_tiff(path)
        series = tiff.series[0]
        fetch = _fetcher(tiff)
        level = (getattr(series, "levels", None) or [series])[-1]
        native = TiffLevel(level, fetch)
        if native.height * native.width > _THUMBNAIL_MAX_PIXELS:
            return None
        step = max(1, int(max(native.height, native.width) // longest))
        if native.layout == "contig":
            block = read_region(native._pages[0], 0, native.height, 0, native.width,
                                fetch=fetch)
            return block[::step, ::step, ...]
        planes = [native.read(c, 0, native.height, 0, native.width)[::step, ::step]
                  for c in range(native.count)]
        return planes[0] if len(planes) == 1 else np.stack(planes, axis=-1)
    except Exception:
        return None
    finally:
        close_tiff(tiff)


__all__ = [
    "TiffLevel",
    "TiffPyramid",
    "close_tiff",
    "focal_planes",
    "geometry",
    "needs_extension",
    "open_image",
    "open_tiff",
    "overview_plane",
    "read_region",
    "rgb_sources",
    "thumbnail",
]
