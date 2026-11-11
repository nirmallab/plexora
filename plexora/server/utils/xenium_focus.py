"""A Xenium `morphology_focus/` folder, read as one multi-channel image.

From XOA 2.0 a Xenium run writes its in-focus morphology image as a *folder*
rather than a file. A v2 run has one file in it, DAPI. A v3 run has four --
`morphology_focus_0000..0003.ome.tif`, which are DAPI, the boundary stain, the
interior RNA stain and the interior protein stain -- and they are four
separate OME-TIFFs over the identical pixel grid, one channel each, not four
planes of one file. XOA 4.0 renamed them after the stain they hold --
`ch0000_dapi.ome.tif`, `ch0001_atp1a1_cd45_e-cadherin.ome.tif`, ... -- and
both namings are read (`FOCUS_FILE`), ordered by the channel number either way.

That is the whole problem this module exists for. Every reader in Plexora
opens ONE path and reads `shape[0]` as the channel count, so four files means
four one-channel images, four cards, four sets of settings, and no way to
composite them; the folder is one image with four channels and nothing else in
the tree can say so.

`FocusPyramid` is shaped exactly like the zarr *group* tifffile produces for a
pyramidal TIFF -- `pyramid[str(level)]`, `len(pyramid)`, each level indexed
`[channel, rows, cols]` -- which is the same contract `brightfield.RgbPyramid`
and `ome_zarr.NgffPyramid` meet, and for the same reason: `_zarr_level`,
`read_tile`'s isinstance branches and `node/api.py`'s `hasattr(pyramid,
"shape")` test all take their existing paths untouched. The channel axis is
the FILE axis; everything below it is each file's own pyramid, read through
`tiff_series.channel_series` so a focus file that is itself a small z-stack
collapses to its middle plane before it becomes a channel here.

A folder holding a single file never reaches here. `spatial_scene` resolves it
to that file, because one file is an ordinary single-channel OME-TIFF and the
ordinary reader is the one with the mileage on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

#: What a focus file is called: `morphology_focus_0000.ome.tif` (XOA 2.0-3.x)
#: or `ch0000_<stain>.ome.tif` (XOA 4.0 on). Anchored and numbered: a stray
#: `.ome.tif` somebody copied into the folder is not a channel of this image.
FOCUS_FILE = re.compile(
    r"^(?:morphology_focus_(?P<index>\d+)|ch(?P<channel>\d+)_(?P<stain>.+?))"
    r"\.ome\.tif$", re.IGNORECASE)

#: The folder's name in a run directory. Matched on the NAME rather than on
#: the contents alone so a directory of unrelated OME-TIFFs is not read as a
#: morphology stack.
FOCUS_DIR = "morphology_focus"

#: The tile grid every level is served on, matching the other pyramids.
TILE_SIZE = 1024


def _channel_number(name):
    """(channel number, naming) of a focus file's name, or None."""
    match = FOCUS_FILE.match(name)
    if match is None:
        return None
    if match.group("index") is not None:
        return int(match.group("index")), "numbered"
    return int(match.group("channel")), "stain"


def focus_files(path) -> list[Path]:
    """The focus folder's channel files, in channel order, or `[]`.

    Ordered by the channel number in the name, which IS channel order: 10x
    numbers them from 0 upward in the order its own metadata lists the stains,
    and a sort that put channel 10 before channel 2 would silently rename
    somebody's panel -- so the numbers are compared as numbers.

    `[]` -- not a guess -- for a folder whose recognised files cannot be one
    image: both namings at once (two runs' files copied together), or two
    files claiming one channel number. Assembling either would put a stain in
    another's slot; `spatial_scene._xenium_path` then opens a single file.
    """
    folder = Path(path)
    if folder.is_file():
        return [Path(member) for member in ome_set(folder)]
    if not folder.is_dir():
        return []
    found = []
    for child in folder.iterdir():
        parsed = _channel_number(child.name)
        if parsed is not None and child.is_file():
            found.append((parsed[0], parsed[1], child))
    if len({naming for _, naming, _ in found}) > 1:
        return []
    numbers = [number for number, _, _ in found]
    if len(set(numbers)) != len(numbers):
        return []
    return [child for _, _, child in sorted(found, key=lambda item: item[0])]


def is_focus_dir(path) -> bool:
    """Whether `path` is a `morphology_focus/` folder of SEVERAL channels.

    False for a folder holding one file. That is not a judgement about the
    folder -- it is how the one-file case is routed to the plain TIFF reader,
    which produces the identical image through code every other import already
    exercises. Both answers agree on what the picture is; they differ only in
    how much new machinery stands between the user and it.
    """
    from plexora.server.providers.base import is_remote_locator

    if not path or is_remote_locator(path):
        return False
    folder = Path(path)
    if folder.is_file():
        # The first file of a multi-file OME-TIFF, one channel a file: the
        # same thing by another route (`ome_set`).
        return len(ome_set(folder)) > 1
    if folder.name.lower() != FOCUS_DIR:
        return False
    return len(focus_files(folder)) > 1


#: What a member of a multi-file OME-TIFF is called.
_OME_SET_SUFFIXES = (".tif", ".tiff", ".tf2", ".tf8", ".btf")


def _set_document(path):
    """The OME document that describes the set `path` belongs to: its own,
    or -- for a `BinaryOnly` file -- the `MetadataFile` it names beside it (a
    `.companion.ome`, or another OME-TIFF)."""
    from plexora.server.utils import ome_xml, tiff_region

    def embedded(file):
        with tiff_region.opened(str(file)) as handle:
            description = handle.pages[0].tags["ImageDescription"].value
        return description if isinstance(description, str) else None

    description = embedded(path)
    if not description or not ome_xml.is_ome_xml(description) or (
            "FileName" not in description and "BinaryOnly" not in description):
        return None
    document = ome_xml.read(description)
    if document is not None and document.metadata_file and not document.images:
        other = Path(path).parent / Path(document.metadata_file).name
        if not other.is_file():
            return None
        if other.name.lower().endswith(_OME_SET_SUFFIXES):
            description = embedded(other)
        else:
            description = other.read_text(encoding="utf-8", errors="replace")
        document = ome_xml.read(description) if description else None
    return document


def _ome_set(path) -> tuple:
    try:
        if not str(path).lower().endswith(_OME_SET_SUFFIXES):
            return ()
        document = _set_document(path)
        if document is None or not document.images:
            return ()
        image = document.images[0]
        if image.size_z != 1 or image.size_t != 1 or image.size_c < 2 \
                or any(count != 1 for count in image.samples):
            return ()
        by_channel = {}
        for position, (first_c, _z, _t, ifd, count, file_name, _uuid) in enumerate(image.tiff_data):
            channel = position if first_c is None else first_c
            if not file_name or ifd not in (None, 0) or count not in (None, 1) \
                    or channel in by_channel:
                return ()
            by_channel[channel] = Path(file_name).name
        names = [by_channel.get(channel) for channel in range(image.size_c)]
        if None in names or len(set(names)) != len(names) or len(by_channel) != image.size_c:
            return ()
        folder = Path(path).parent
        if names[0] != Path(path).name:
            return ()               # a member, not the file that stands for the set
        members = tuple(str(folder / name) for name in names)
        return members if all(Path(member).is_file() for member in members) else ()
    except Exception:  # noqa: BLE001 -- not a set this reader assembles: the file opens by itself
        return ()


_ome_set_cached = None


def ome_set(path) -> tuple:
    """The files of the multi-file OME-TIFF that `path` is the FIRST file of,
    in channel order; `()` for any other file.

    One file per channel is how Bio-Formats, Micro-Manager and several
    instruments' exports have written large panels since the 2011-06 schema
    (and how a Xenium focus folder is written): each file holds one plane and
    the whole document, whose `TiffData` elements name the file of every
    channel. Plexora opens a TIFF without tifffile's OME reader and does not
    follow those names, so such an image opened as its first channel alone.

    Assembled only when it is the simple, common shape -- one plane per
    channel (`SizeZ = SizeT = 1`), every channel's file named and present
    beside this one -- and only for the file that holds channel 0, so the
    other members still open by themselves and the set has one address. A
    `BinaryOnly` file is described by the `MetadataFile` it names. Anything
    else (planes of a z-stack or time series spread over files) reads as
    before. Remembered per file.
    """
    global _ome_set_cached
    if _ome_set_cached is None:
        from plexora.server.utils import metacache

        _ome_set_cached = metacache.per_file(_ome_set)
    return _ome_set_cached(path)


class _Level:
    """One resolution, across every channel file.

    Indexed `[channel, rows, cols]`, and also `[channel, rows]` -- the
    quantization-window scan slabs a plane two subscripts at a time, and a
    level that only accepted three would 500 the first time a window was
    computed.
    """

    __slots__ = ("_arrays", "shape", "dtype", "ndim")

    def __init__(self, arrays):
        self._arrays = list(arrays)
        first = self._arrays[0]
        self.shape = (len(self._arrays), int(first.shape[-2]), int(first.shape[-1]))
        self.dtype = first.dtype
        self.ndim = 3

    def __getitem__(self, key):
        if not isinstance(key, tuple):
            key = (key,)
        index, rest = key[0], key[1:]
        if isinstance(index, slice):
            # A whole-level read, which `overview_plane` and `block_reduce`
            # want as a real (c, y, x) array.
            chosen = self._arrays[index]
            return np.stack([np.asarray(array[(0,) + rest]) for array in chosen])
        return np.asarray(self._arrays[int(index)][(0,) + rest])

    def __array__(self, dtype=None):
        stacked = np.stack([np.asarray(array[0]) for array in self._arrays])
        return stacked.astype(dtype) if dtype is not None else stacked


class FocusPyramid:
    """A focus folder's resolution levels, shaped like a pyramidal TIFF's
    zarr group.

    Deliberately not a `zarr.Array` and deliberately without `.shape`, for the
    same reason `RgbPyramid` is not: both are how existing code tells a pyramid
    from a single plane.
    """

    def __init__(self, levels, *, path=None, handles=(), names=()):
        self._levels = list(levels)
        self.path = str(path) if path is not None else None
        #: The open file handles, held so they outlive this object's use --
        #: every level above reads lazily from them.
        self._handles = list(handles)
        #: Each channel's name out of its own OME-XML, in channel order.
        self.channel_names = list(names)

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
        return [[int(level.shape[-2]), int(level.shape[-1])]
                for level in self._levels]


def _file_levels(path):
    """`(levels, handle)` for one focus file, finest first.

    Through `channel_series`, so a file that is a z-stack of one channel is
    already reduced to its focal plane and a file that is a plain 2-D plane
    keeps its SubIFD pyramid. Either way what comes back is `(1, Y, X)` per
    level, and the leading 1 is dropped as the arrays are stacked.
    """
    import tifffile as tf
    import zarr

    from plexora.server.utils import tiff_series

    handle = tf.TiffFile(str(path), is_ome=False)
    store = zarr.open(tiff_series.channel_series(handle).aszarr(), mode="r")
    if hasattr(store, "shape"):
        arrays = [store]
    else:
        arrays = [store[key] for key in sorted(store.array_keys(), key=int)]
    return arrays, handle


def open_focus(path, extension=None) -> FocusPyramid:
    """The folder at `path` as a `FocusPyramid`.

    The level chain stops where the SHALLOWEST file's does, and every level is
    checked to agree on its dimensions. Both are refusals rather than repairs:
    the files are supposed to be the same grid written by the same instrument
    in one pass, and a folder where they are not is a folder where compositing
    the channels would put stains in the wrong place -- which looks plausible
    and is wrong, the one failure mode worth a hard stop.

    `extension` is accepted and ignored. A focus file is written pyramidal by
    the instrument, so the derived-levels path every other reader needs has
    nothing to do here; taking the argument keeps the provider's call sites
    identical across formats.
    """
    files = focus_files(path)
    if not files:
        raise FileNotFoundError(f"No morphology_focus files in {path}")

    per_file, handles = [], []
    for child in files:
        arrays, handle = _file_levels(child)
        per_file.append(arrays)
        handles.append(handle)

    depth = min(len(arrays) for arrays in per_file)
    levels = []
    for index in range(depth):
        planes = [arrays[index] for arrays in per_file]
        sizes = {tuple(int(size) for size in plane.shape[-2:]) for plane in planes}
        if len(sizes) != 1:
            raise ValueError(
                f"{Path(path).name} level {index} disagrees between its "
                f"channel files: {sorted(sizes)}. These have to be the same "
                "pixel grid to be one image.")
        levels.append(_Level(planes))

    return FocusPyramid(levels, path=path, handles=handles,
                        names=channel_names(path))


def stain_from_name(name) -> str | None:
    """The stain an XOA 4 focus file's name gives, readable, or None.

    `ch0001_atp1a1_cd45_e-cadherin.ome.tif` -> `ATP1A1/CD45/E-CADHERIN`: the
    name joins a channel's stains with underscores, and the OME-XML (which
    is preferred when present) joins them with slashes.
    """
    match = FOCUS_FILE.match(name)
    stain = match.group("stain") if match is not None else None
    if not stain:
        return None
    return "/".join(part.upper() for part in stain.split("_") if part)


def channel_names(path) -> list[str]:
    """Each focus file's channel name, out of its own OME-XML.

    `["DAPI", "18S", "ATP1A1/CD45/E-Cadherin", "AlphaSMA/Vimentin"]` for a v3
    run -- which is the panel, and is what the user is looking for in the
    channel list. An XOA 4 file that does not name its channel is named by
    its file (`stain_from_name`); anything else falls back to its index rather
    than to nothing: a list that is half names and half blanks is worse than
    one that is half names and half numbers.
    """
    from plexora.datasource import _channel_names_from_ome_xml

    files = focus_files(path)
    if Path(path).is_file():
        # A multi-file OME-TIFF: every file carries the whole document, so the
        # first one names every channel.
        names = _channel_names_from_ome_xml(files[0], len(files)) if files else None
        return list(names) if names else [f"Channel {index + 1}" for index in range(len(files))]
    found = []
    for index, child in enumerate(files):
        names = _channel_names_from_ome_xml(child, 1)
        found.append(str(names[0]) if names
                     else stain_from_name(child.name) or f"Channel {index + 1}")
    return found


def geometry(pyramid) -> dict:
    """Shape facts, in `local.image_geometry`'s vocabulary."""
    finest = pyramid[0]
    return {
        "levels": len(pyramid),
        "num_channels": int(finest.shape[0]),
        "height": int(finest.shape[-2]),
        "width": int(finest.shape[-1]),
        "tile_height": TILE_SIZE,
        "tile_width": TILE_SIZE,
        "level_shapes": pyramid.level_shapes,
    }


def overview_plane(pyramid, minimum: int = 200, maximum: int = 400) -> np.ndarray:
    """A materialized coarse level as (c, y, x), for the mini-map and stats.

    Same heuristic as the TIFF, NGFF and brightfield paths -- the smallest
    level with both dimensions >= `minimum`, pooled down when it is still well
    above it -- which is what makes it bounded whatever the folder's full
    resolution is.
    """
    from plexora.server.utils.blocks import block_reduce

    candidates = [index for index in range(len(pyramid))
                  if all(size >= minimum for size in pyramid[index].shape[-2:])]
    index = candidates[-1] if candidates else 0
    array = np.asarray(pyramid[index])
    if array.shape[-2] > maximum or array.shape[-1] > maximum:
        factor = max(1, int(min(array.shape[-2] // minimum,
                                array.shape[-1] // minimum)))
        array = block_reduce(array, (1, factor, factor), np.mean)
    return array


def physical_metadata(path):
    """The first file's OME `Pixels`, the way the plain TIFF reader returns it.

    The pixel size is a property of the grid, and every file in the folder is
    on the same grid -- which `open_focus` has already refused to proceed
    without -- so the first file speaks for all of them.
    """
    import tifffile as tf
    from ome_types import from_xml

    files = focus_files(path)
    if not files:
        return {}
    try:
        with tf.TiffFile(str(files[0]), is_ome=False) as handle:
            xml = handle.pages[0].tags["ImageDescription"].value
    except Exception:
        return {}
    try:
        return from_xml(xml).images[0].pixels
    except Exception:
        # A schema before 2016-06, which ome_types reads only with lxml.
        from plexora.server.utils import ome_xml

        return ome_xml.pixels(xml)
