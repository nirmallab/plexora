"""Single-pass conversion of a raster label mask into the tiled, pyramidal
outline OME-TIFF the viewer draws.

This replaces the old three-step pipeline (pyramid_assemble -> pyramid_upgrade
-> ensure_outline_segmentation), which built a full *filled* pyramid only to
discard it: nothing ever served the filled file, because the viewer's label
layer is outlines-only (see imageViewer.js's "Label/segmentation tiles must
stay transparent outside outlines"). Writing outlines directly collapses three
passes into one and drops the throwaway intermediate -- on a 34050x5797 uint32
Orion mask that is ~60s and ~5MB, where step one alone previously cost ~60s and
1.19GB.

Outlines are recomputed from filled labels independently at every pyramid level
rather than by downsampling an outline image, which is what keeps boundaries
continuous (and one pixel wide) as the user zooms out. Labels are downsampled
with nearest-neighbour sampling so integer cell IDs survive exactly, and the
full-resolution mask is never required in RAM: each output tile is produced by
one indexed read of the source.
"""

from __future__ import annotations

import errno
import io
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import numpy as np
import tifffile as tf
import zarr

from plexora.server.utils import ome_zarr

# Suffix for masks this module generates. The old pipeline wrote
# ".fast-outlines.pyramid.ome.tiff" even when the boundaries were exact;
# generated files are now named for what they are. Paths already recorded in
# config.json keep working -- nothing derives this suffix from a stored path.
OUTLINE_SUFFIX = ".outlines.pyramid.ome.tiff"

# The other thing this module can write: a *filled* label pyramid, for the
# viewer's on-the-fly mode where the shader derives boundaries per fragment
# instead of reading them out of the file (see u32_rgba_map in frag.glsl).
# Cheaper to produce than outlines -- no halo reads, no boundary pass -- and it
# keeps a cell ID on every pixel rather than only on boundaries.
FILLED_SUFFIX = ".labels.pyramid.ome.tiff"

# Stamped into the OME XML of every mask we write, so recognising our own
# output is an exact metadata check rather than a statistical guess about
# pixel content (see generated_mask_kind). The two kinds need distinct markers:
# a filled pyramid must never be mistaken for outlines, since serving it to a
# viewer that is not outlining paints solid blobs over the image.
OUTLINE_MARKER = "plexora-outline-mask"
FILLED_MARKER = "plexora-label-pyramid"

# config.json's `segmentationMode`, and this module's `mode=` argument.
MODE_OUTLINES = "outlines"
MODE_FILLED = "filled"

# What an import produces unless told otherwise. Filled, because measuring the
# two against each other on a real 47296x64246 mask found no reason to prefer
# outlines: per tile, end to end (server read + PNG encode + UPNG decode +
# renderLabelTile), filled came out at 33.9 ms against 34.7 ms for outlines and
# 41 KB against 57 KB -- filled labels compress better than scattered boundary
# pixels, which pays back the client-side boundary pass several times over. And
# a mask that is *already* a tiled pyramid is served untouched in this mode,
# where outlines would spend a minute-plus deriving a second copy of it.
#
# MODE_OUTLINES is still fully supported and exercised by the tests; nothing in
# the UI selects it any more. Pass mode= explicitly to get it back.
DEFAULT_MODE = MODE_FILLED

# Ceiling on the level-0 bytes we will pull into RAM in a single read. Reading
# the whole mask at once is dramatically faster than per-tile reads when the
# source lives on a network mount -- measured 60s vs 179s for the Orion mask on
# an SMB share, because hundreds of small scattered reads pay the round-trip
# each time. Above this budget we fall back to streaming tiles, which bounds
# memory at the cost of that speedup.
_DEFAULT_FULL_READ_MAX_BYTES = 8 * 1024 ** 3

_FINGERPRINT_SCAN_LIMIT = 20000


def _full_read_budget() -> int:
    raw = os.environ.get("PLEXORA_MASK_FULL_READ_MAX_BYTES")
    if not raw:
        return _DEFAULT_FULL_READ_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_FULL_READ_MAX_BYTES


def derived_output_path(segmentation_path, data_directory=None, *,
                        mode: str = MODE_OUTLINES) -> Path:
    """The NAME a derived mask takes in `data_directory`, or beside the source
    when that is None.

    Naming only -- deciding *which* directory is `resolve_derived_mask`, and
    callers that have to write something should ask that instead.

    The two modes get distinct names so switching a datasource between them
    neither overwrites nor silently reuses the other kind of file.
    """
    source_path = Path(segmentation_path)
    target_dir = Path(data_directory) if data_directory else source_path.parent
    stem = source_path.name
    # `.parquet` is here because a mask source need not be a raster at all:
    # a table of boundary polygons is one too, and its derived pyramid has to
    # land on the same name every other source's does.
    for extension in (".ome.tiff", ".ome.tif", ".tiff", ".tif", ".png",
                      ".ims", ".zarr", ".parquet"):
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    suffix = FILLED_SUFFIX if mode == MODE_FILLED else OUTLINE_SUFFIX
    return target_dir / f"{stem}{suffix}"


def outline_output_path(segmentation_path, data_directory=None) -> Path:
    """Destination for the outline mask derived from `segmentation_path`."""
    return derived_output_path(segmentation_path, data_directory, mode=MODE_OUTLINES)


class DerivedMask(NamedTuple):
    """Where a mask's derived pyramid is, and where a new one would go.

    `existing` is a pyramid that can be served right now, or None. `target` is
    where one would be built. `writable` is False when nothing can be built
    anywhere, which is a thing to say up front rather than halfway through a
    conversion.
    """

    existing: Optional[Path]
    target: Path
    writable: bool


def _newest_mtime_ns(path: Path) -> Optional[int]:
    """Modification time of a file, or of the newest file under a directory.

    A `.zarr` mask is a directory whose own mtime says nothing about its
    contents on most filesystems, so the tree has to be walked -- bounded the
    same way `source_fingerprint` bounds its scan, since the answer only has to
    be good enough to notice that a mask was regenerated.
    """
    try:
        if not path.is_dir():
            return path.stat().st_mtime_ns
    except OSError:
        return None
    newest = 0
    scanned = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    newest = max(newest, (Path(root) / name).stat().st_mtime_ns)
                except OSError:
                    continue
                scanned += 1
                if scanned >= _FINGERPRINT_SCAN_LIMIT:
                    return newest
    except OSError:
        return None
    return newest


def _is_adoptable(derived: Path, source: Path, mode: str) -> bool:
    """Whether `derived` can be served for `source` without rebuilding it.

    Two conditions, both cheap. It has to be a mask we wrote in the mode this
    datasource wants -- an exact metadata check, not a guess about pixels. And
    it must not be older than the source, which is what stops a mask that was
    regenerated after its pyramid from being served as though nothing had
    happened.

    Deliberately weaker than `_segmentation_mapping_is_current`, which compares
    against the fingerprint config.json recorded at build time and is the right
    check whenever there IS a recorded fingerprint. This one exists for the
    callers that have none: a fresh import, and a data node, which has no
    project and therefore nothing to have recorded.
    """
    if generated_mask_kind(derived) != mode:
        return False
    if generated_mask_scale(derived) is None and is_polygon_source(source):
        # Drawn from polygons before the grid was chosen per run, so always at
        # the image's own pixels -- which for a Visium HD run is a mask that is
        # all outline. Redrawn once; the new file is stamped and adopted after.
        return False
    if _is_remote(source):
        # No mtime to compare against. Staleness of a remote source is the
        # recorded ETag's job (`source_fingerprint`), checked on every load.
        return True
    derived_at = _newest_mtime_ns(derived)
    source_at = _newest_mtime_ns(source)
    if derived_at is None or source_at is None:
        return False
    if derived_at < source_at:
        return False
    # Every mcmicro mask is called `cell.ome.tif`, and the name is all a
    # derived file is found by. A pyramid of a different mask that happens to
    # share a folder or a stem must not be adopted -- it would draw another
    # sample's cells, confidently. Metadata only; "cannot say" does not block.
    derived_size, source_size = plane_size(derived), plane_size(source)
    if derived_size and source_size and derived_size != source_size:
        return False
    return True


def is_polygon_source(path) -> bool:
    """Whether `path` is cell polygons (`boundary_mask`), not a raster mask."""
    if _is_remote(path):
        return False
    from plexora.server.utils import boundary_mask

    return boundary_mask.is_boundary_source(path)


def _is_remote(path) -> bool:
    from plexora.server.providers.base import is_remote_locator

    return is_remote_locator(path)


def _remote_derived_path(url, data_directory, mode) -> Path:
    """Where a remote mask's pyramid goes: the project, named for the store.

    `labels/cells` inside `sample.zarr` becomes `sample_cells` -- the element
    name alone ("0", "cells") is what every store calls its masks.
    """
    from plexora.server.utils import remote_store

    root, sub = remote_store.split_store_url(url)
    parts = [remote_store.display_name(root)] + [
        p for p in sub.split("/") if p and p != "labels"]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", "_".join(parts)) or "mask"
    if remote_store.is_file_url(url):
        # One file (`mask.tif`, `cells.png`): named the way the same file on
        # disk would be, extension dropped. A store keeps the name it has
        # always had, or every remote mask converted before would be rebuilt.
        return derived_output_path(Path(stem), data_directory, mode=mode)
    suffix = FILLED_SUFFIX if mode == MODE_FILLED else OUTLINE_SUFFIX
    return Path(data_directory) / f"{stem}{suffix}"


# -- formats ----------------------------------------------------------------
#
# A mask is one plane of whole-number cell ids. Any file that can store such a
# plane exactly is read as one: TIFF, OME-Zarr labels, PNG -- on disk or at a
# web address -- and an Imaris file on disk (a volume of ids, or one plane;
# volume/sources/ims.ImsLabels). Some formats an IMAGE can be in cannot: they change pixel values
# when they store them, or only ever hold colour. Those are refused up front,
# with the reason, rather than failing halfway through a conversion with a
# reader's error about a file it was never going to understand.

_SLIDE_FORMAT = ("{name} is a slide scanner's format: it holds a colour "
                 "picture, usually JPEG-compressed, and never cell ids.")

#: Formats an image can be in and a mask cannot, by suffix, each with why.
_NOT_A_MASK = {
    ".jpg": "{name} is a JPEG, and JPEG compression changes pixel values, so "
            "it cannot hold cell ids exactly.",
    ".jpeg": "{name} is a JPEG, and JPEG compression changes pixel values, so "
             "it cannot hold cell ids exactly.",
    ".svs": _SLIDE_FORMAT, ".ndpi": _SLIDE_FORMAT, ".scn": _SLIDE_FORMAT,
    ".mrxs": _SLIDE_FORMAT, ".svslide": _SLIDE_FORMAT, ".bif": _SLIDE_FORMAT,
    ".qptiff": "{name} is an Akoya scanner's image, not a label mask.",
    ".dcm": "{name} is a DICOM slide. Plexora reads DICOM images, not DICOM "
            "segmentation objects.",
    ".dicom": "{name} is a DICOM slide. Plexora reads DICOM images, not DICOM "
              "segmentation objects.",
}

#: What every refusal goes on to say: the formats that do work.
_SAVE_AS = (" Save the mask as a TIFF (.tif, .ome.tif) or a PNG of integer "
            "labels, or point at OME-Zarr labels.")

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: PNG colour types (IHDR byte 25) that hold one value per pixel: greyscale
#: (0) and palette (3), whose indices ARE the ids -- palette PNGs are how
#: several annotation tools save labels. 2 (RGB), 4 (grey + alpha) and 6
#: (RGBA) are pictures of a mask, coloured for looking at, with the ids gone.
_PNG_LABEL_COLOUR_TYPES = (0, 3)


class PngHeader(NamedTuple):
    width: int
    height: int
    bit_depth: int
    colour_type: int


def _suffix(path) -> str:
    from plexora.server.utils import remote_image

    return remote_image.suffix_of(path)


def is_png(path) -> bool:
    """Whether `path` names a PNG, here or at a web address. Name only."""
    return _suffix(path) == ".png"


def is_ims(path) -> bool:
    """Whether `path` names an Imaris file. Name only."""
    return _suffix(path) == ".ims"


def _png_bytes(path) -> bytes:
    if _is_remote(path):
        from plexora.server.utils import remote_store

        return remote_store.read_bytes(path)
    return Path(path).read_bytes()


def _png_header_of(data: bytes) -> Optional[PngHeader]:
    if data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR" or len(data) < 26:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return PngHeader(int(width), int(height), data[24], data[25])


def png_header(path) -> Optional[PngHeader]:
    """A PNG's size and pixel layout, from its first 26 bytes, or None.

    No pixel is decoded, so this is what answers "how big is this mask?" and
    "is it one channel?" for a PNG. A web address is read whole -- through the
    cache, so the decode that follows does not fetch it again.
    """
    try:
        if _is_remote(path):
            return _png_header_of(_png_bytes(path))
        with open(path, "rb") as handle:
            return _png_header_of(handle.read(26))
    except (OSError, ValueError):
        return None


def _colour_png(name) -> str:
    return (f"{name} is a colour PNG: a picture of a mask, with the cell ids "
            "turned into colours. A mask PNG is one channel of integer ids "
            "(greyscale, or palette).")


def read_png_labels(path) -> np.ndarray:
    """A PNG mask's ids as a 2-D integer array.

    Decoded whole, because a PNG has no tiles to read a window of. Greyscale
    goes through imagecodecs (no pixel-count ceiling, 16-bit kept as 16-bit --
    Cellpose's `_cp_masks.png`). Palette goes through Pillow, which hands back
    the indices, where imagecodecs expands them to the colours.
    """
    data = _png_bytes(path)
    header = _png_header_of(data)
    name = _display_name(path)
    if header is None:
        raise ValueError(f"{name} is not a PNG file.")
    if header.colour_type not in _PNG_LABEL_COLOUR_TYPES:
        raise ValueError(_colour_png(name) + _SAVE_AS)
    if header.colour_type == 3:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as picture:
            return np.asarray(picture)
    import imagecodecs

    return np.asarray(imagecodecs.png_decode(data))


def _display_name(path) -> str:
    if _is_remote(path):
        from plexora.server.utils import remote_store

        return remote_store.url_name(path)
    return Path(path).name


def mask_format_refusal(path) -> Optional[str]:
    """Why `path` cannot be a segmentation mask, or None when it can be.

    By name for the formats that can never hold ids, and by the PNG header for
    a colour PNG. Says nothing about the pixels of a format that CAN hold ids
    -- whether a particular TIFF is a mask is `looks_like_label_image`'s
    question, and a float TIFF is refused by the conversion with its dtype.
    """
    name = _display_name(path)
    reason = _NOT_A_MASK.get(_suffix(path))
    if reason is not None:
        return reason.format(name=name) + _SAVE_AS
    if is_png(path):
        header = png_header(path)
        if header is not None and header.colour_type not in _PNG_LABEL_COLOUR_TYPES:
            return _colour_png(name) + _SAVE_AS
    return None


def can_serve_in_place(path) -> bool:
    """Whether the mask tile route can read `path` itself, unconverted.

    Only a TIFF or a zarr store on this machine. The tile route opens masks
    with tifffile or zarr, so a PNG or an Imaris file is always converted --
    even one that already looks like outlines, which would otherwise be handed
    to the viewer as it is. A web address is not opened by path: a label
    pyramid there is read through `open_remote_labels` (see
    data_model._servable_as_is).
    """
    return not _is_remote(path) and not is_png(path) and not is_ims(path)


class _Plane2D:
    """A label array's (rows, cols) plane, with any leading axes pinned.

    An NGFF label image is anything from (y, x) to (t, c, z, y, x). The
    pyramidizer wants one 2-D plane; t and c are 0, and z is its middle slice
    -- the same plane a Z-stack reads as elsewhere.
    """

    def __init__(self, array, leading_index):
        self._array = array
        self._leading = tuple(leading_index)
        self.shape = (int(array.shape[-2]), int(array.shape[-1]))
        self.ndim = 2
        self.dtype = array.dtype
        chunks = tuple(getattr(array, "chunks", ()) or self.shape)
        self.chunks = (int(chunks[-2]), int(chunks[-1]))

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        rows = index[0] if len(index) > 0 else slice(None)
        cols = index[1] if len(index) > 1 else slice(None)
        return self._array[self._leading + (rows, cols)]

    def __array__(self, dtype=None, copy=None):
        plane = np.asarray(self[:, :])
        return plane.astype(dtype) if dtype is not None else plane

    @property
    def oindex(self):
        """Orthogonal reads, `plane.oindex[rows, cols]`, as the streaming
        pyramidizer takes them from a zarr array."""
        return _OIndex(lambda rows, cols: self._array.oindex[self._leading + (rows, cols)])


class _OIndex:
    __slots__ = ("_read",)

    def __init__(self, read):
        self._read = read

    def __getitem__(self, index):
        rows, cols = index
        return self._read(rows, cols)


def _zarr_level_zero(node):
    """The full-resolution 2-D plane of a zarr label image, here or remote.

    Three layouts: a bare array; an NGFF multiscale group (level 0 is the
    first dataset its metadata lists, whatever it is called); and a
    bioformats2raw store, whose series `0` is itself the multiscale group.
    Leading axes are pinned by `_Plane2D`.
    """
    if isinstance(node, zarr.Array):
        array, multiscale = node, None
    else:
        multiscale = ome_zarr._multiscale_of(node)
        if multiscale:
            array = node[ome_zarr._dataset_paths(multiscale)[0]]
        else:
            array = node["0"]
            if not isinstance(array, zarr.Array):
                return _zarr_level_zero(array)
    if array.ndim == 2:
        return array
    axes = ome_zarr._axes_of(multiscale or {}, array.ndim)
    leading = []
    for position, name in enumerate(axes[:-2]):
        depth = int(array.shape[position])
        leading.append(depth // 2 if name == "z" else 0)
    return _Plane2D(array, leading)


class _TiffPlane:
    """A TIFF's label plane at a web address, read by tiles: the full-resolution
    one, or the pyramid's `level`.

    Never through tifffile's zarr view: that reads inside zarr's IO loop, and a
    streamed read waits on the same loop for its bytes (see tiff_region). Each
    read here fetches only the tiles or strips it touches, on the calling
    thread. `shape` is the level's own, so a stack or a colour TIFF is refused
    by the conversion's "one 2-D label plane" check like a local one.
    """

    def __init__(self, tiff, level=0):
        from plexora.server.utils import tiff_region

        series = tiff.series[0]
        level = series.levels[level] if getattr(series, "levels", None) else series
        self._page = level.pages[0]
        self._fetch = tiff_region._fetcher(tiff)
        self._read_region = tiff_region.read_region
        self._region_bytes = tiff_region.region_bytes
        self.shape = tuple(int(size) for size in level.shape)
        self.ndim = len(self.shape)
        self.dtype = np.dtype(level.dtype)

    def _read(self, rows: slice, cols: slice) -> np.ndarray:
        height, width = self.shape[-2], self.shape[-1]
        y0, y1, _ = rows.indices(height)
        x0, x1, _ = cols.indices(width)
        return self._read_region(self._page, y0, y1, x0, x1, fetch=self._fetch)

    def fetch_bytes(self, windows) -> int:
        """What reading every `(rows, cols)` window would fetch, together --
        found from the header, before any of it is fetched."""
        height, width = self.shape[-2], self.shape[-1]
        regions = []
        for rows, cols in windows:
            y0, y1, _ = rows.indices(height)
            x0, x1, _ = cols.indices(width)
            regions.append((y0, y1, x0, x1))
        return self._region_bytes(self._page, regions)

    def __getitem__(self, index):
        rows, cols = index if isinstance(index, tuple) else (index, slice(None))
        return self._read(rows, cols)

    def __array__(self, dtype=None, copy=None):
        plane = self._read(slice(None), slice(None))
        return plane.astype(dtype) if dtype is not None else plane

    @property
    def oindex(self):
        def read(rows, cols):
            rows, cols = np.asarray(rows), np.asarray(cols)
            if not rows.size or not cols.size:
                return np.zeros((rows.size, cols.size), dtype=self.dtype)
            top, left = int(rows.min()), int(cols.min())
            block = self._read(slice(top, int(rows.max()) + 1),
                               slice(left, int(cols.max()) + 1))
            return block[np.ix_(rows - top, cols - left)]

        return _OIndex(read)


def _remote_level_zero(url):
    """(plane, closer) for a label image at a web address, through the cache.

    OME-Zarr labels as a zarr group; a TIFF read tile by tile (`_TiffPlane`);
    a PNG fetched once and decoded whole; an Imaris file's mask channel, its
    middle plane, over the stream (`ims.ImsPlane`, as on disk).
    """
    if is_png(url):
        return read_png_labels(url), lambda: None
    if is_ims(url):
        from plexora.server.volume.sources import ims

        plane = ims.ImsPlane(url)
        return plane, plane.close
    from plexora.server.utils import remote_image

    if remote_image.kind_or_none(url, probe=False) == remote_image.TIFF:
        from plexora.server.utils import tiff_region

        tiff = tiff_region.open_tiff(url)
        try:
            return _TiffPlane(tiff), lambda: tiff_region.close_tiff(tiff)
        except Exception:
            tiff_region.close_tiff(tiff)
            raise
    return _zarr_level_zero(ome_zarr._open_group(url)), lambda: None


class _TiffLabelLevels:
    """A tiled label pyramid in a TIFF at a web address, as the tile route
    reads one: levels keyed "0", "1", ..., what `zarr.open(series.aszarr())`
    gives for the same file on disk (`label_overlay.label_region`). That view
    cannot read a streamed file, so each level is a `_TiffPlane`, fetching only
    the tiles a request touches, through the chunk cache.
    """

    def __init__(self, tiff):
        self._tiff = tiff
        count = len(getattr(tiff.series[0], "levels", None) or [tiff.series[0]])
        self._levels = {str(index): _TiffPlane(tiff, index) for index in range(count)}

    def __contains__(self, key) -> bool:
        return str(key) in self._levels

    def __getitem__(self, key):
        return self._levels[str(key)]

    def __len__(self) -> int:
        return len(self._levels)

    def array_keys(self):
        return iter(self._levels)

    def close(self) -> None:
        from plexora.server.utils import tiff_region

        tiff_region.close_tiff(self._tiff)


def open_remote_labels(url):
    """A label pyramid at a web address, for the tile route to read in place.

    Only for a mask `label_pyramid_gaps` passes: tiled and pyramidal. A TIFF
    as `_TiffLabelLevels`; OME-Zarr labels as their zarr group, read through
    the chunk cache.
    """
    from plexora.server.utils import remote_image, tiff_region

    if remote_image.kind_or_none(url, probe=False) == remote_image.TIFF:
        tiff = tiff_region.open_tiff(url)
        try:
            return _TiffLabelLevels(tiff)
        except Exception:
            tiff_region.close_tiff(tiff)
            raise
    return ome_zarr._open_group(url)


def resolve_derived_mask(segmentation_path, data_directory=None, *,
                         mode: str = MODE_OUTLINES,
                         use_preference: bool = True) -> DerivedMask:
    """Where `segmentation_path`'s derived pyramid is, and where one would go.

    Two locations, searched in this order.

    **Beside the source** is the shared one, and it is shared precisely because
    it is derivable from the mask's path alone: a second project, a second user
    on the same mount, and a data node that has no projects at all each arrive
    at the same filename, so the conversion is paid for once. That is the whole
    reason a node can be handed a raw mask and get on with it, where before it
    needed an operator to convert one by hand and then say where the result
    went.

    **`data_directory`** -- the project's own derived root -- is the fallback,
    and is where every mask generated before this convention still lives. That
    is why it is still searched and not merely written to: reopening an
    existing project has to find its existing pyramid, not spend a minute
    rebuilding one it already has.

    Writing beside the source is not always possible, and this is the case the
    old scheme sidestepped by never trying: pipeline output routinely lands in
    a directory that is read-only to the person opening it. So `target` is the
    first of the two that accepts writes, and `writable` is False only when
    neither does.

    `paths.mask_output_preference()` swaps the order for somebody who would
    rather Plexora kept its output under the project -- a mask in a synced or
    backed-up folder is the case that asks for it. It swaps the order for
    FINDING as well as for writing, because a preference that changed only
    where new files go would answer two different things once both existed.
    Neither setting narrows the search: both places are looked in either way,
    so changing your mind costs nothing and orphans nothing.

    A data node passes its own per-mask folder under the node's data root as
    `data_directory` with `use_preference=False`: the preference is about a
    viewer's own filing, and on a node the shared place beside the mask always
    comes first.
    """
    from plexora import paths

    if _is_remote(segmentation_path):
        # A web address has no folder beside it to write into -- and `Path`
        # of one is `https:/host/...`, which `is_writable` would create under
        # the working directory. The project's own derived root is the only
        # place a remote mask's pyramid can go.
        if data_directory is None:
            raise ValueError("A mask at a web address needs a project to convert into.")
        target = _remote_derived_path(segmentation_path, data_directory, mode)
        existing = target if _is_adoptable(target, segmentation_path, mode) else None
        return DerivedMask(existing, target, paths.is_writable(target.parent))

    source_path = Path(segmentation_path)
    candidates = [derived_output_path(source_path, None, mode=mode)]
    if data_directory is not None:
        in_project = derived_output_path(source_path, data_directory, mode=mode)
        if use_preference and paths.mask_output_preference() == "project":
            candidates.insert(0, in_project)
        else:
            candidates.append(in_project)

    existing = next(
        (c for c in candidates if _is_adoptable(c, source_path, mode)), None)
    for candidate in candidates:
        if paths.is_writable(candidate.parent):
            return DerivedMask(existing, candidate, True)
    return DerivedMask(existing, candidates[0], False)


def source_fingerprint(path) -> Optional[str]:
    """Cheap staleness key for a mask source, used to decide whether an
    already-generated outline file still corresponds to it. Stat-only: never
    reads pixels, so it is safe to call on every datasource load.

    For a web address, the metadata document's ETag or Last-Modified (one
    bounded request, remembered for ten seconds).
    """
    if _is_remote(path):
        from plexora.server.utils import remote_store

        return remote_store.fingerprint_key(path)
    source_path = Path(path)
    try:
        stat_result = source_path.stat()
    except OSError:
        return None
    if not source_path.is_dir():
        return f"{stat_result.st_size}-{stat_result.st_mtime_ns}"
    # A .zarr mask is a directory tree, so aggregate over its entries instead.
    count = 0
    total_size = 0
    newest = 0
    for root, _, files in os.walk(source_path):
        for name in files:
            try:
                entry = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total_size += entry.st_size
            newest = max(newest, entry.st_mtime_ns)
            if count >= _FINGERPRINT_SCAN_LIMIT:
                # Pathologically large store: fall back to the directory's own
                # stat rather than walking forever.
                return f"dir-{stat_result.st_mtime_ns}"
    return f"{count}-{total_size}-{newest}"


def _open_level_zero(path):
    """Return (array, closer) for the highest-resolution plane of `path`.

    `is_ome=False` matches how the rest of plexora opens masks: OME metadata on
    these files is decorative, and trusting it makes tifffile reinterpret the
    page layout of some third-party masks.
    """
    # Whether it reads as zarr, not whether it is NAMED `.zarr`. A SpatialData
    # store's mask arrives as `store.zarr/labels/cells`, which is a zarr group
    # with an ordinary name -- the suffix test sent it to tifffile, which
    # cannot open a directory, so a store's own segmentation never imported.
    if _is_remote(path):
        return _remote_level_zero(path)
    if is_png(path):
        return read_png_labels(path), lambda: None
    if is_ims(path):
        from plexora.server.volume.sources import ims

        plane = ims.ImsPlane(path)
        return plane, plane.close
    if ome_zarr.is_zarr_image_path(path):
        return _zarr_level_zero(zarr.open(str(path), mode="r")), lambda: None
    reader = tf.TiffFile(str(path), is_ome=False)
    store = reader.series[0].aszarr(level=0)
    array = zarr.open(store, mode="r")

    def _close():
        store.close()
        reader.close()

    return array, _close


def _memmap_plane(path, expected_shape=None):
    """A memory map of `path`'s full-resolution plane, or None if it can't be
    mapped.

    Worth reaching for before the Zarr adapter whenever pixels are read in
    windows. Segmentation masks are routinely written uncompressed and
    *untiled*, frequently as a single strip spanning the whole image, and the
    adapter has to decode a whole strip to answer any read inside it. Sampling
    twelve 512x512 windows out of a 12GB single-strip Orion mask costs 59s that
    way and 0.015s through a memory map.

    Returns None for anything compressed, tiled or multi-plane, where memmap
    either raises or hands back something that isn't the plane asked for --
    hence `expected_shape`, which callers pass from the reader whose geometry
    they actually trust.
    """
    if not can_serve_in_place(path) or ome_zarr.is_zarr_image_path(path):
        return None
    try:
        candidate = tf.memmap(str(path))
    except Exception:
        return None
    shape = tuple(getattr(candidate, "shape", ()))
    if len(shape) != 2:
        return None
    if expected_shape is not None and shape != tuple(expected_shape):
        return None
    return candidate


def plane_size(path) -> Optional[tuple]:
    """(width, height) of a mask's full-resolution plane, or None.

    Metadata only -- the reader is asked for the array's shape and no pixel is
    decoded -- so this is cheap to ask about a 12GB mask and is what lets the
    mask's dimensions be compared with the image's at all. Nothing else in
    Plexora records them: a mask is served in the IMAGE's coordinate system
    (Project.all_layers gives the mask layer the image's width and height), so
    the file's own size is never otherwise consulted after conversion.

    None for anything that will not open, is not a single plane, or is not
    here -- all three are "cannot say", which is what a caller comparing two
    sizes needs to be able to tell apart from "they differ".
    """
    if is_png(path):
        # Off the header: decoding a whole PNG to learn its size is the cost
        # this function exists to avoid.
        header = png_header(path)
        return (header.width, header.height) if header is not None else None
    if _is_remote(path):
        candidate = path
    else:
        candidate = Path(path)
        if not candidate.exists():
            return None
    try:
        array, close = _open_level_zero(candidate)
        try:
            shape = tuple(int(size) for size in getattr(array, "shape", ()))
        finally:
            close()
    except Exception:
        return None
    if len(shape) == 3:
        # A 3-D label volume (plexora/server/volume/labels.py): every plane
        # is the image's size, which is the size worth comparing.
        from plexora.server.volume import labels as volume_labels

        if volume_labels.is_label_volume(candidate):
            return shape[2], shape[1]
    if len(shape) != 2:
        return None
    return shape[1], shape[0]


def generated_mask_kind(path) -> Optional[str]:
    """MODE_OUTLINES, MODE_FILLED, or None for a mask we did not write.

    Keyed on the OME marker rather than the filename, so a mask the user has
    since renamed or moved is still recognised as generated output.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        with tf.TiffFile(str(candidate)) as reader:
            recorded = reader.ome_metadata or ""
    except Exception:
        return None
    if OUTLINE_MARKER in recorded:
        return MODE_OUTLINES
    if FILLED_MARKER in recorded:
        return MODE_FILLED
    return None


#: How a mask drawn from polygons records its grid relative to the image's:
#: appended to the marker in the OME Name, e.g. `plexora-label-pyramid scale=4`.
#: `generated_mask_kind` matches the marker by substring, so a stamped file is
#: still recognised by everything that only asks what kind it is.
_SCALE_STAMP = re.compile(r"\bscale=(\d+)\b")


def generated_mask_scale(path) -> Optional[int]:
    """Mask pixels per image pixel, as stamped by the writer, or None.

    None is "not stated": a raster mask converted from pixels, anything we did
    not write, and every polygon mask drawn before the scale was chosen per
    run. The last is why None and 1 differ -- see `_is_adoptable`.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        with tf.TiffFile(str(candidate)) as reader:
            recorded = reader.ome_metadata or ""
    except Exception:
        return None
    if FILLED_MARKER not in recorded and OUTLINE_MARKER not in recorded:
        return None
    found = _SCALE_STAMP.search(recorded)
    return int(found.group(1)) if found else None


def is_generated_outline_mask(path) -> bool:
    """True when `path` is an outline mask this module wrote."""
    return generated_mask_kind(path) == MODE_OUTLINES


def label_pyramid_gaps(path) -> Optional[list]:
    """Requirements `path` fails for being served straight to the tile route as
    a filled label layer. An empty list means it can be served untouched; None
    means it could not be read at all.

    Both requirements matter, and "the mask is already filled labels" satisfies
    neither on its own -- which is the usual surprise. A single-level mask has
    nothing to answer a request for level 3 with (`_zarr_level` hands back full
    resolution for every level, so the viewer draws the wrong scale), and an
    untiled mask makes every tile request decode whole strips -- masks are
    routinely written as one strip spanning the entire image.
    """
    if is_png(path):
        # Never tiled and never pyramidal: a PNG is one compressed stream.
        return ["it has only one resolution level (no pyramid)", "it is not tiled"]
    if is_ims(path):
        # Tiled and pyramidal, but HDF5, which the tile route does not read.
        return ["it is an Imaris file"]
    if _is_remote(path):
        return _remote_label_pyramid_gaps(path)
    candidate = Path(path)
    try:
        if ome_zarr.is_zarr_image_path(path):
            return _zarr_group_gaps(zarr.open(str(path), mode="r"))
        if not candidate.is_file():
            return None
        with tf.TiffFile(str(candidate), is_ome=False) as reader:
            return _tiff_level_gaps(reader)
    except Exception:
        return None


def _remote_label_pyramid_gaps(url) -> Optional[list]:
    """`label_pyramid_gaps` for a web address: the header only, nothing else
    fetched. None when it cannot be read, or is neither a TIFF nor zarr."""
    from plexora.server.utils import remote_image, tiff_region

    try:
        kind = remote_image.kind_or_none(url, probe=False)
        if kind == remote_image.TIFF:
            tiff = tiff_region.open_tiff(url)
            try:
                return _tiff_level_gaps(tiff)
            finally:
                tiff_region.close_tiff(tiff)
        if kind == remote_image.ZARR:
            return _zarr_group_gaps(ome_zarr._open_group(url))
    except Exception:
        return None
    return None


def _tiff_level_gaps(reader) -> list:
    series = reader.series[0]
    page = series.levels[0].pages[0]
    gaps = []
    if len(series.levels) <= 1:
        gaps.append("it has only one resolution level (no pyramid)")
    if not page.is_tiled:
        gaps.append("it is not tiled")
    return gaps


def _zarr_group_gaps(group) -> list:
    if isinstance(group, zarr.Array) or len(group) <= 1:
        return ["it has only one resolution level (no pyramid)"]
    return _zarr_level_gaps(group)


def _zarr_level_gaps(group) -> list:
    """What keeps a multi-level zarr label group from being served as it is.

    The tile route reads a zarr mask as `group[str(level)][rows, cols]`
    (label_overlay.label_region), so it serves a group untouched only when its
    levels are 2-D arrays named 0, 1, 2... -- what SpatialData writes. A
    (t, c, z, y, x) NGFF label image, or one whose datasets are named
    otherwise, used to pass as servable on its level count alone and drew
    nothing; it is converted instead, like any other mask.
    """
    multiscale = ome_zarr._multiscale_of(group)
    names = (ome_zarr._dataset_paths(multiscale) if multiscale
             else sorted((str(k) for k in group.array_keys() if str(k).isdigit()), key=int))
    if [str(n) for n in names[:1]] != ["0"] or not all(
            str(n) == str(i) for i, n in enumerate(names)):
        return ["its levels are not named 0, 1, 2..."]
    first = group[str(names[0])]
    if getattr(first, "ndim", 2) != 2:
        return [f"its levels are {first.ndim}-D arrays, not single planes"]
    return []


def is_servable_label_pyramid(path) -> bool:
    """True when `path` can be served straight to the tile route as a filled
    label layer: tiled, and pyramidal enough for the zoomed-out levels."""
    return label_pyramid_gaps(path) == []


def looks_like_outline_mask(path) -> bool:
    """Heuristic "is this mask already outlines?" test for *user-supplied*
    files, where there is no metadata marker to trust.

    Sampled from contiguous windows on purpose. An earlier version strided the
    whole plane (`level[::step, ::step]`), which for a large mask meant a step
    of ~23px -- far enough apart that neighbouring samples almost never shared
    a label, so every big filled mask scored as "outlines" and outline
    generation was skipped entirely. Interior-ness is a local property and only
    survives contiguous sampling.
    """
    try:
        array, close = _open_level_zero(path)
    except Exception:
        return False
    try:
        if len(array.shape) != 2 or array.size == 0:
            return False
        height, width = (int(size) for size in array.shape)
        # Geometry stays authoritative from the reader opened above (the one
        # that serves tiles); the memory map, when there is one, only makes the
        # windowed reads below cheap enough to run on a datasource load.
        mapped = _memmap_plane(path, (height, width))
        if mapped is not None:
            array = mapped
        window = 512
        nonzero_total = 0
        pixel_total = 0
        interior_total = 0
        boundary_total = 0
        for y_fraction in (0.3, 0.5, 0.7):
            for x_fraction in (0.2, 0.4, 0.6, 0.8):
                y = min(max(0, int(height * y_fraction) - window // 2), max(0, height - window))
                x = min(max(0, int(width * x_fraction) - window // 2), max(0, width - window))
                sample = np.asarray(array[y:y + window, x:x + window])
                if sample.ndim != 2 or sample.size == 0:
                    continue
                nonzero = sample != 0
                nonzero_total += int(np.count_nonzero(nonzero))
                pixel_total += sample.size
                center = nonzero[1:-1, 1:-1]
                if center.size == 0:
                    continue
                center_values = sample[1:-1, 1:-1]
                same_id_interior = (
                    center
                    & (center_values == sample[:-2, 1:-1])
                    & (center_values == sample[2:, 1:-1])
                    & (center_values == sample[1:-1, :-2])
                    & (center_values == sample[1:-1, 2:])
                )
                interior_total += int(np.count_nonzero(same_id_interior))
                boundary_total += int(np.count_nonzero(center))
    finally:
        close()

    if pixel_total == 0 or nonzero_total == 0 or boundary_total == 0:
        return False
    density = nonzero_total / pixel_total
    interior_fraction = interior_total / boundary_total
    return density <= 0.20 and interior_fraction <= 0.05


def pyramid_factors(height: int, width: int, tile_size: int,
                    min_levels: Optional[int] = None) -> list:
    """The downsampling factor of every level, coarsest last.

    One definition, because the level count is a promise: the viewer asks for
    a tile at level N and the file either has that level or serves nothing.

    `min_levels` keeps that promise across two files. A mask is drawn at the
    reference image's size but the image's own pyramid may go coarser than
    the "one tile covers it" rule stops at -- and the mask layer is served at
    the IMAGE's level count (`Project.all_layers` gives it the image's
    `max_level`), so a mask one level short raises a KeyError on the tile the
    whole-slide view asks for first.
    """
    factors = [1]
    while max(
        (height + factors[-1] - 1) // factors[-1],
        (width + factors[-1] - 1) // factors[-1],
    ) > tile_size:
        factors.append(factors[-1] * 2)
    while min_levels and len(factors) < int(min_levels):
        factors.append(factors[-1] * 2)
    return factors


class PyramidError(ValueError):
    """A pyramid could not be written, said as one sentence a person can act on.

    A `ValueError` so the import routes that already turn one into a 400 say
    it as it is, rather than as the "could not be opened" a bare OSError reads
    as by the time it reaches the image-error classifier.
    """


# errno values that mean "the disk is full", whichever way the filesystem says it.
_NO_SPACE_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


def write_tiled_pyramid(
    destination,
    *,
    height: int,
    width: int,
    dtype,
    block: Callable,
    channels: Optional[int] = None,
    metadata: Optional[dict] = None,
    tile_size: int = 1024,
    compression: Optional[str] = "zlib",
    compression_args: Optional[dict] = None,
    max_workers: Optional[int] = None,
    buffer_size: Optional[int] = None,
    min_levels: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stage_callback: Optional[Callable[..., None]] = None,
) -> str:
    """Write a tiled SubIFD pyramid OME-TIFF, tile by tile, from `block`.

    The one place that knows what a Plexora-generated pyramid looks like on
    disk: SubIFD levels, square tiles, `minisblack`, a temporary name beside
    the destination that is renamed over it only once the file is complete.
    A process killed halfway leaves a dot-prefixed `.tmp.ome.tiff`, never a
    file under the destination's name, so partial output is never mistaken
    for a finished pyramid.

    `block(channel, factor, y_start, y_stop, x_start, x_stop, level_height,
    level_width)` returns one tile in LEVEL coordinates, unpadded. Tiles are
    requested channel-major, then by tile row, then left to right -- the order
    tifffile stores them in -- so a producer can read a whole tile row once on
    its first tile and slice the rest out of it.

    `channels=None` writes a 2-D (Y, X) image; an integer writes (C, Y, X).
    `metadata` is the level-0 OME metadata.

    `buffer_size` caps the raw bytes tifffile gathers from `block` before
    compressing them on `max_workers` threads. Its default is 512 MB, which is
    512 MB of decoded tiles held at once whatever the producer's own budget.

    A full disk raises `PyramidError`; every other failure propagates as is.
    Either way nothing is left under the destination's name.
    """
    destination = Path(destination)
    dtype = np.dtype(dtype)
    factors = pyramid_factors(height, width, tile_size, min_levels)
    planes = 1 if channels is None else int(channels)
    total_tiles = planes * sum(
        (((height + factor - 1) // factor + tile_size - 1) // tile_size)
        * (((width + factor - 1) // factor + tile_size - 1) // tile_size)
        for factor in factors
    )
    tiles_written = 0

    if stage_callback is not None:
        stage_callback("building")
    temporary_path = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}-",
            suffix=".tmp.ome.tiff",
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        def tiles_for_level(factor: int):
            nonlocal tiles_written
            level_height = (height + factor - 1) // factor
            level_width = (width + factor - 1) // factor
            for channel in range(planes):
                for y_start in range(0, level_height, tile_size):
                    y_stop = min(y_start + tile_size, level_height)
                    for x_start in range(0, level_width, tile_size):
                        x_stop = min(x_start + tile_size, level_width)
                        tile = block(channel, factor, y_start, y_stop, x_start,
                                     x_stop, level_height, level_width)
                        if tile.shape != (tile_size, tile_size):
                            padded = np.zeros((tile_size, tile_size), dtype=dtype)
                            padded[: tile.shape[0], : tile.shape[1]] = tile
                            tile = padded
                        tiles_written += 1
                        if progress_callback is not None:
                            progress_callback(tiles_written, total_tiles)
                        yield np.ascontiguousarray(tile, dtype=dtype)

        with tf.TiffWriter(str(temporary_path), bigtiff=True, ome=True) as writer:
            for level_index, factor in enumerate(factors):
                level_shape = (
                    (height + factor - 1) // factor,
                    (width + factor - 1) // factor,
                )
                if channels is not None:
                    level_shape = (planes,) + level_shape
                writer.write(
                    tiles_for_level(factor),
                    shape=level_shape,
                    dtype=dtype,
                    tile=(tile_size, tile_size),
                    compression=compression,
                    compressionargs=compression_args,
                    photometric="minisblack",
                    metadata=metadata if level_index == 0 else None,
                    subifds=len(factors) - 1 if level_index == 0 else None,
                    subfiletype=1 if level_index else None,
                    maxworkers=max_workers,
                    buffersize=buffer_size,
                )
        # The tile loop reported 100% as the last tile was YIELDED to the
        # writer; the compression flush and the rename happen after that, and
        # on a large pyramid they are not instant. A bar that reaches 100% and
        # then waits is the same complaint as one that sits at 0%.
        if stage_callback is not None:
            stage_callback("writing")
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError as error:
        if error.errno in _NO_SPACE_ERRNOS:
            raise PyramidError(
                f"The disk holding {destination.parent} ran out of space while "
                f"writing {destination.name}; nothing was kept. Free some space "
                "and try again.") from error
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return str(destination)


def write_label_pyramid(
    destination,
    *,
    height: int,
    width: int,
    dtype,
    block: Callable,
    tile_size: int = 1024,
    compression: Optional[str] = "zlib",
    max_workers: Optional[int] = None,
    marker: str = FILLED_MARKER,
    scale: Optional[int] = None,
    min_levels: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stage_callback: Optional[Callable[..., None]] = None,
) -> str:
    """Write a tiled pyramidal label OME-TIFF, tile by tile, from `block`.

    A label mask is `write_tiled_pyramid` with one plane and the marker in the
    OME Name that `generated_mask_kind` reads back. Two producers write one --
    a raster mask being converted, and a table of boundary polygons being
    drawn (see `boundary_mask`) -- and a second copy of the format would be a
    second answer to "is this file ours", which is the check the whole
    staleness scheme is built on.

    `block(factor, y_start, y_stop, x_start, x_stop, level_height,
    level_width)` returns that tile's contents in LEVEL coordinates, unpadded.

    `scale`, when given, is stamped after the marker (`generated_mask_scale`):
    how many of this file's pixels span one of the reference image's.
    """
    name = marker if scale is None else f"{marker} scale={int(scale)}"
    return write_tiled_pyramid(
        destination,
        height=height,
        width=width,
        dtype=dtype,
        block=lambda _channel, *rest: block(*rest),
        metadata={"axes": "YX", "Channel": {"Name": "cell"}, "Name": name},
        tile_size=tile_size,
        compression=compression,
        max_workers=max_workers,
        min_levels=min_levels,
        progress_callback=progress_callback,
        stage_callback=stage_callback,
    )


def pyramidize_segmentation_mask(
    input_path,
    output_path=None,
    *,
    tile_size: int = 1024,
    compression: Optional[str] = "zlib",
    max_workers: Optional[int] = None,
    outline: bool = True,
    method: str = "exact",
    overwrite: bool = False,
    full_read: Optional[bool] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stage_callback: Optional[Callable[..., None]] = None,
) -> str:
    """Convert a 2-D label TIFF/Zarr/PNG -- here or at a web address -- to a
    tiled, pyramidal OME-TIFF.

    Parameters
    ----------
    input_path
        A single-plane 2-D integer label mask with zero as background. Already
        pyramidal input is fine -- its highest-resolution level is used and a
        fresh outline pyramid is derived from it.
    output_path
        Destination. Defaults to `outline_output_path(input_path)`.
    tile_size
        Square TIFF tile edge, a multiple of 16. Larger tiles cut conversion
        time noticeably on huge masks.
    compression
        Lossless TIFF compression; None writes uncompressed tiles.
    max_workers
        Worker threads tifffile may use for compression.
    outline
        Write only label-boundary pixels, keeping their original cell IDs.
        False writes a conventional filled label pyramid.
    method
        "exact" marks every cell-cell and cell-background boundary
        (8-neighbour). "fast" marks only foreground-background boundaries
        (4-neighbour) -- cheaper, but misses seams where two cells touch.
    overwrite
        Replace an existing destination.
    full_read
        True bulk-reads the whole mask into RAM before tiling, False always
        streams per tile. None (default) decides from the level-0 byte size
        against `PLEXORA_MASK_FULL_READ_MAX_BYTES`.
    progress_callback
        Called as (tiles_written, tiles_total) as the pyramid is written.
    """
    remote = _is_remote(input_path)
    source_path = str(input_path) if remote else Path(input_path).expanduser()
    if not remote and not source_path.exists():
        raise FileNotFoundError(source_path)
    refusal = mask_format_refusal(source_path)
    if refusal is not None:
        raise ValueError(refusal)
    if remote and output_path is None:
        raise ValueError("A mask at a web address needs an output path.")
    if not isinstance(tile_size, int) or tile_size < 16 or tile_size % 16:
        raise ValueError("tile_size must be an integer multiple of 16.")
    outline_method = str(method).strip().lower()
    if outline_method not in {"exact", "fast"}:
        raise ValueError("method must be 'exact' or 'fast'.")

    destination = (
        outline_output_path(source_path)
        if output_path is None
        else Path(output_path).expanduser()
    )
    if not remote and destination.resolve() == source_path.resolve():
        raise ValueError("output_path must be different from input_path.")
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"Destination already exists: {destination}. Pass overwrite=True to replace it."
        )

    def announce(stage):
        """Say which phase this is. Deliberately NOT folded into
        `progress_callback`: that reports `(done, total)` within the tile loop
        and its contract is pinned by tests, while this reports the phases
        BEFORE the tile loop -- which is where all the unexplained waiting was.
        """
        if stage_callback is not None:
            stage_callback(stage)

    labels = None
    close_source = None
    memmap = None
    try:
        # Metadata-only probe first: shape and dtype decide the read strategy,
        # so they must be known before anything is pulled into memory.
        announce("inspecting")
        # A TIFF on this disk keeps its own fast paths (tifffile's sequential
        # whole-plane read, a memory map). Everything else -- a zarr store, a
        # PNG, an Imaris file, anything at a web address -- is opened once through
        # `_open_level_zero` and read from what that returns.
        local_tiff = (not remote and not is_png(source_path) and not is_ims(source_path)
                      and not ome_zarr.is_zarr_image_path(source_path))
        if local_tiff:
            with tf.TiffFile(str(source_path), is_ome=False) as reader:
                series = reader.series[0]
                shape, dtype = series.shape, np.dtype(series.dtype)
        else:
            labels, close_source = _open_level_zero(source_path)
            shape, dtype = labels.shape, np.dtype(labels.dtype)
            if isinstance(labels, np.ndarray):
                # Already in memory (a decoded PNG): nothing to stream.
                memmap = labels
        if len(shape) != 2:
            raise ValueError(
                "input_path must contain one 2-D label plane; "
                f"found shape {tuple(shape)!r}."
            )
        if not np.issubdtype(dtype, np.integer):
            raise TypeError(f"Label mask must have an integer dtype, found {dtype}.")
        height, width = (int(size) for size in shape)
        if height == 0 or width == 0:
            raise ValueError("Label mask must have non-zero height and width.")
        # OME-TIFF has no 64-bit integer type, so a 64-bit mask -- what
        # omero-zarr wrote for IDR's labels, and what numpy defaults to -- is
        # written as uint32. The viewer draws labels as 32-bit values anyway
        # (data_model.read_tile), so nothing that fits is lost; a value that
        # does not fit is refused by name rather than wrapped into another
        # cell's id.
        out_dtype = np.dtype(np.uint32) if dtype.itemsize > 4 else dtype

        use_full_read = (
            bool(full_read)
            if full_read is not None
            else (height * width * dtype.itemsize) <= _full_read_budget()
        )

        # The dominant stall, and the one that used to sit at 0%: a full-plane
        # read of a whole-slide mask is 60 s locally and 179 s streaming.
        # There is no fraction to report inside `tf.imread` -- it is one call --
        # so this stage moves the bar to the foot of its band and names itself,
        # which is the difference between "working" and "hung".
        announce("preparing")
        if memmap is not None:
            pass
        elif use_full_read:
            # One sequential read, then every tile is served from RAM. Routed
            # through `memmap` so the direct-indexing branches below (which
            # already special-case a real ndarray) pick it up unchanged.
            if not local_tiff:
                memmap = np.asarray(labels)
                close_source()
                close_source = None
            else:
                memmap = np.asarray(tf.imread(str(source_path), is_ome=False, series=0, level=0))
            labels = memmap
        elif local_tiff:
            # Streaming per tile: map the source where possible, so each tile is
            # a handful of page faults rather than a strip decode.
            memmap = _memmap_plane(source_path, (height, width))
            if memmap is not None:
                labels = memmap
        if labels is None:
            labels, close_source = _open_level_zero(source_path)

        def outline_tile(halo: np.ndarray) -> np.ndarray:
            center = halo[1:-1, 1:-1]
            if outline_method == "fast":
                foreground = center != 0
                is_boundary = foreground & (
                    (halo[:-2, 1:-1] == 0) | (halo[2:, 1:-1] == 0)
                    | (halo[1:-1, :-2] == 0) | (halo[1:-1, 2:] == 0)
                )
            else:
                # An exact boundary is any labelled pixel adjacent to a
                # *different* label or to background -- not just those next to
                # empty space, which is what makes touching cells separable.
                is_boundary = (center != 0) & (
                    (halo[:-2, 1:-1] != center) | (halo[2:, 1:-1] != center)
                    | (halo[1:-1, :-2] != center) | (halo[1:-1, 2:] != center)
                    | (halo[:-2, :-2] != center) | (halo[:-2, 2:] != center)
                    | (halo[2:, :-2] != center) | (halo[2:, 2:] != center)
                )
            return np.where(is_boundary, center, 0)

        def narrowed(values):
            if values.dtype == out_dtype:
                return values
            if values.size:
                low, high = int(values.min()), int(values.max())
                limit = int(np.iinfo(out_dtype).max)
                if low < 0 or high > limit:
                    raise ValueError(
                        f"This mask's label values run from {low} to {high}; "
                        f"a mask is stored as {out_dtype} (0 to {limit}), so "
                        "its cells would be renumbered into each other. "
                        "Relabel it to consecutive ids first.")
            return values.astype(out_dtype)

        def read_block(y_start, y_stop, x_start, x_stop, factor):
            if factor == 1:
                return narrowed(np.asarray(labels[y_start:y_stop, x_start:x_stop]))
            rows = np.arange(y_start * factor, y_stop * factor, factor)
            columns = np.arange(x_start * factor, x_stop * factor, factor)
            if memmap is not None:
                return narrowed(np.asarray(labels[np.ix_(rows, columns)]))
            return narrowed(np.asarray(labels.oindex[rows, columns]))

        def block(factor, y_start, y_stop, x_start, x_stop,
                  level_height, level_width):
            if not outline:
                return read_block(y_start, y_stop, x_start, x_stop, factor)
            # Read a one-pixel halo *in this level's* coordinates. Comparing
            # against real neighbours rather than a zero-padded edge is what
            # keeps an outline unbroken where two TIFF tiles meet.
            read_y_start = max(0, y_start - 1)
            read_y_stop = min(level_height, y_stop + 1)
            read_x_start = max(0, x_start - 1)
            read_x_stop = min(level_width, x_stop + 1)
            sampled = read_block(
                read_y_start, read_y_stop, read_x_start, read_x_stop, factor
            )
            halo = np.zeros(
                (y_stop - y_start + 2, x_stop - x_start + 2), dtype=out_dtype
            )
            insert_y = read_y_start - (y_start - 1)
            insert_x = read_x_start - (x_start - 1)
            halo[
                insert_y:insert_y + sampled.shape[0],
                insert_x:insert_x + sampled.shape[1],
            ] = sampled
            return outline_tile(halo)

        write_label_pyramid(
            destination,
            height=height,
            width=width,
            dtype=out_dtype,
            block=block,
            tile_size=tile_size,
            compression=compression,
            max_workers=max_workers,
            marker=OUTLINE_MARKER if outline else FILLED_MARKER,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
    finally:
        if memmap is not None:
            underlying = getattr(memmap, "_mmap", None)
            if underlying is not None:
                underlying.close()
        if close_source is not None:
            close_source()

    return str(destination)


__all__ = [
    "FILLED_MARKER",
    "FILLED_SUFFIX",
    "MODE_FILLED",
    "MODE_OUTLINES",
    "OUTLINE_MARKER",
    "OUTLINE_SUFFIX",
    "DerivedMask",
    "derived_output_path",
    "generated_mask_kind",
    "is_generated_outline_mask",
    "is_servable_label_pyramid",
    "label_pyramid_gaps",
    "looks_like_outline_mask",
    "open_remote_labels",
    "outline_output_path",
    "pyramid_factors",
    "pyramidize_segmentation_mask",
    "resolve_derived_mask",
    "source_fingerprint",
    "write_label_pyramid",
]
