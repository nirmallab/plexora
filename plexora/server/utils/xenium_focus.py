"""A Xenium `morphology_focus/` folder, read as one multi-channel image.

From XOA 2.0 a Xenium run writes its in-focus morphology image as a *folder*
rather than a file. A v2 run has one file in it, DAPI. A v3 run has four --
`morphology_focus_0000..0003.ome.tif`, which are DAPI, the boundary stain, the
interior RNA stain and the interior protein stain -- and they are four
separate OME-TIFFs over the identical pixel grid, one channel each, not four
planes of one file.

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

#: What a focus file is called. Anchored and numbered: a stray `.ome.tif`
#: somebody copied into the folder is not a channel of this image.
FOCUS_FILE = re.compile(r"^morphology_focus_\d+\.ome\.tif$", re.IGNORECASE)

#: The folder's name in a run directory. Matched on the NAME rather than on
#: the contents alone so a directory of unrelated OME-TIFFs is not read as a
#: morphology stack.
FOCUS_DIR = "morphology_focus"

#: The tile grid every level is served on, matching the other pyramids.
TILE_SIZE = 1024


def focus_files(path) -> list[Path]:
    """The focus folder's channel files, in channel order, or `[]`.

    Sorted by name, which IS channel order: 10x numbers them `_0000` upward in
    the order its own metadata lists the stains, and a sort that put channel 10
    before channel 2 would silently rename somebody's panel -- so the numbers
    are compared as numbers.
    """
    folder = Path(path)
    if not folder.is_dir():
        return []
    found = [child for child in folder.iterdir()
             if child.is_file() and FOCUS_FILE.match(child.name)]

    def order(child):
        digits = re.findall(r"\d+", child.name)
        return (int(digits[-1]) if digits else 0, child.name)

    return sorted(found, key=order)


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
    if folder.name.lower() != FOCUS_DIR:
        return False
    return len(focus_files(folder)) > 1


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


def channel_names(path) -> list[str]:
    """Each focus file's channel name, out of its own OME-XML.

    `["DAPI", "18S", "ATP1A1/CD45/E-Cadherin", "AlphaSMA/Vimentin"]` for a v3
    run -- which is the panel, and is what the user is looking for in the
    channel list. Falls back to the file's index for any file that does not
    name its channel, rather than to nothing: a list that is half names and
    half blanks is worse than one that is half names and half numbers.
    """
    from plexora.datasource import _channel_names_from_ome_xml

    found = []
    for index, child in enumerate(focus_files(path)):
        names = _channel_names_from_ome_xml(child, 1)
        found.append(str(names[0]) if names else f"Channel {index + 1}")
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
    from skimage.measure import block_reduce

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
        return from_xml(xml).images[0].pixels
    except Exception:
        return {}
