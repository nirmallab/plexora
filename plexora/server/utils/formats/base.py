"""What every reader in this package hands over, and the pyramid made of it.

A reader opens one file and says what the image in it is -- its native
resolution levels (finest first), its channels and their names, whether its
samples are one colour picture, its pixel size, its z and t -- and reads a
rectangle of one channel of one level of one plane. Nothing else: everything a
viewer needs on top of that is built here, once, from the same pieces the TIFF
and DICOM readers are built from:

- the halving chain the viewer addresses (`brightfield._dyadic_shapes`), each
  level read from the nearest native level and resampled in flight
  (`dicom_wsi._MonoLevel` for channels, `dicom_wsi._ColorLevel` for a colour
  picture), stopping where a tile would cost more than it should
  (`brightfield._affordable`);
- the coarse levels a file without a pyramid lacks, derived once into the
  project folder by `ome_zarr.build_extension` and appended here;
- the overview, the geometry and the scale bar's numbers.

A reader's file is read through one handle, under a lock, by every level and
every plane of the pyramids opened on it: moving through z opens nothing.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np


class FormatSupportMissing(RuntimeError):
    """The package that reads this format is not installed.

    `INSTALL` is the line the import dialog shows (`import_proposal._install_hint`
    reads it by name, as it does for the slide and DICOM readers).
    """

    def __init__(self, format_name: str, package: str):
        self.INSTALL = f"pip install '{package}'"
        super().__init__(
            f"Reading {format_name} files needs the '{package}' package, which "
            f"is not installed here. Install it with:\n\n    {self.INSTALL}\n")


class FormatUnreadable(ValueError):
    """A file of a known format whose content this reader cannot serve."""


class Reader:
    """One opened file. Subclasses fill the fields in `__init__` and implement
    `read` (and `read_rgb` when `rgb`).

    `levels` are `(height, width)` of the file's own resolution levels, finest
    first; a file without a pyramid has one. `read` returns `(h, w)` of
    `dtype`; `read_rgb` `(h, w, 3)` uint8. Both may be asked for any rectangle
    inside the level and must return exactly its size.
    """

    format_name: str = ""
    levels: list
    channels: int = 1
    dtype: np.dtype = np.dtype(np.uint16)
    rgb: bool = False
    channel_names: list
    #: `(x, y)` in µm, or None when the file states none.
    pixel_size: Optional[tuple] = None
    z_count: int = 1
    t_count: int = 1
    #: µm between planes, or None.
    z_step: Optional[float] = None
    #: Every image the file holds, by name, and which of them this reader shows.
    #: Only images of the tissue (not a slide scan's overview or label), in a
    #: fixed order: `image_index` is a position in this list, and it is what a
    #: project records (`ImageSpec.image_index`) and the import offers.
    images: list
    image_index: int = 0

    def __init__(self):
        self.levels = []
        self.channel_names = []
        self.images = []
        self._lock = threading.RLock()

    def read(self, level: int, channel: int, y0: int, y1: int, x0: int, x1: int,
             z: int = 0, t: int = 0) -> np.ndarray:
        raise NotImplementedError

    def read_rgb(self, level: int, y0: int, y1: int, x0: int, x1: int,
                 z: int = 0, t: int = 0) -> np.ndarray:
        planes = [self.read(level, channel, y0, y1, x0, x1, z, t)
                  for channel in range(min(3, self.channels))]
        while len(planes) < 3:
            planes.append(planes[-1])
        block = np.stack(planes, axis=-1)
        if block.dtype != np.uint8:
            from plexora.server.utils.brightfield import colour_bytes

            block = colour_bytes(block)
        return np.ascontiguousarray(block)

    def close(self) -> None:
        pass

    # -- conveniences for subclasses -------------------------------------

    def note(self) -> str:
        """One sentence on what was opened, for a file holding more than one
        image; '' when it holds one. Said where only one of them is taken --
        a file added as a layer of a sample that already has its image; an
        import of the file offers each image as a sample of its own."""
        if len(self.images) <= 1:
            return ""
        shown = self.images[self.image_index] if self.image_index < len(self.images) else ""
        others = len(self.images) - 1
        return (f"This {self.format_name} file holds {len(self.images)} images; "
                f"Plexora opens \"{shown}\" and not the other "
                f"{others if others > 1 else 'one'}.")


def chosen(images, index, path) -> int:
    """`index` as a position in `images` (what a reader's file holds), or
    `FormatUnreadable` naming how many images there are."""
    try:
        index = int(index or 0)
    except (TypeError, ValueError):
        raise FormatUnreadable(f"{index!r} is not an image number") from None
    count = len(images)
    if not 0 <= index < count:
        raise FormatUnreadable(
            f"{Path(str(path)).name} holds {count} image{'s' if count != 1 else ''}; "
            f"there is no image {index + 1}.")
    return index


class TileCache:
    """The last decoded tiles, shared by every level of a reader: a viewer tile
    is a quarter of a 512-pixel file tile or less, and its neighbours ask for
    the same one."""

    def __init__(self, capacity: int = 96):
        self._capacity = capacity
        self._tiles: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, make):
        with self._lock:
            tile = self._tiles.get(key)
            if tile is not None:
                self._tiles.move_to_end(key)
                return tile
        tile = make()
        with self._lock:
            self._tiles[key] = tile
            while len(self._tiles) > self._capacity:
                self._tiles.popitem(last=False)
        return tile


def assemble(read_tile, tile_h: int, tile_w: int, y0: int, y1: int, x0: int, x1: int,
             dtype, samples: int = 0, fill=0) -> np.ndarray:
    """A rectangle of a tiled plane: `read_tile(ty, tx)` gives a whole tile
    (or None where the file has none, which reads as `fill`)."""
    shape = (y1 - y0, x1 - x0) + ((samples,) if samples else ())
    out = np.empty(shape, dtype=dtype)
    out[...] = fill
    for ty in range(y0 // tile_h, (y1 - 1) // tile_h + 1):
        for tx in range(x0 // tile_w, (x1 - 1) // tile_w + 1):
            tile = read_tile(ty, tx)
            if tile is None:
                continue
            ty0, tx0 = ty * tile_h, tx * tile_w
            sy0, sy1 = max(y0, ty0), min(y1, ty0 + tile_h)
            sx0, sx1 = max(x0, tx0), min(x1, tx0 + tile_w)
            piece = tile[sy0 - ty0:sy1 - ty0, sx0 - tx0:sx1 - tx0]
            out[sy0 - y0:sy0 - y0 + piece.shape[0], sx0 - x0:sx0 - x0 + piece.shape[1]] = piece
    return out


# -- the pyramid --------------------------------------------------------------


class _Native:
    """A native level as `brightfield._pick_source` and `_affordable` see it."""

    __slots__ = ("index", "height", "width")

    def __init__(self, index: int, height: int, width: int):
        self.index, self.height, self.width = int(index), int(height), int(width)


class _ChannelSource:
    __slots__ = ("_reader", "_level", "_channel", "_z", "_t", "height", "width")

    def __init__(self, reader: Reader, native: _Native, channel: int, z: int, t: int):
        self._reader, self._level, self._channel = reader, native.index, int(channel)
        self._z, self._t = int(z), int(t)
        self.height, self.width = native.height, native.width

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        return self._reader.read(self._level, self._channel, y0, y1, x0, x1, self._z, self._t)


class _ColourSource:
    __slots__ = ("_reader", "_level", "_z", "_t", "height", "width")

    def __init__(self, reader: Reader, native: _Native, z: int, t: int):
        self._reader, self._level = reader, native.index
        self._z, self._t = int(z), int(t)
        self.height, self.width = native.height, native.width

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        return self._reader.read_rgb(self._level, y0, y1, x0, x1, self._z, self._t)


class FormatPyramid:
    """One plane of a file's image, shaped like every other pyramid here.

    No `.shape`, `pyramid[str(level)]`, levels `[channel, rows, cols]` (and
    `.rgb` on a colour image's levels): the contract `DicomPyramid` keeps, for
    the reasons given there.
    """

    def __init__(self, levels, *, reader: Reader, path, extension=None,
                 base_levels: Optional[int] = None, z: int = 0, t: int = 0,
                 owner: bool = True):
        self._levels = list(levels)
        self._reader = reader
        self._owner = owner
        self.path = str(path)
        self.extension = str(extension) if extension is not None else None
        self.base_levels = len(self._levels) if base_levels is None else base_levels
        self.z, self.t = int(z), int(t)
        self.is_color = bool(reader.rgb)
        self.format_name = reader.format_name
        self.channel_names = list(reader.channel_names)
        self.mpp = tuple(reader.pixel_size) if reader.pixel_size else None
        self.z_count, self.t_count = int(reader.z_count), int(reader.t_count)
        self.z_step = reader.z_step
        self.image_index = int(reader.image_index)
        self.note = reader.note()

    def __len__(self) -> int:
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key) -> bool:
        try:
            return 0 <= int(key) < len(self._levels)
        except (TypeError, ValueError):
            return False

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    @property
    def level_shapes(self) -> list:
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]

    @property
    def reader(self) -> Reader:
        return self._reader

    def plane(self, z: int = 0, t: int = 0) -> "FormatPyramid":
        """The same levels at another plane, on the same open file. The
        coarse levels derived at import belong to the default plane only; at
        another plane those the file cannot serve are resampled from the
        plane's own level above as they are read (`build(derive=True)`), so
        every plane has the default plane's levels."""
        if (int(z), int(t)) == (self.z, self.t):
            return self
        return build(self._reader, self.path, extension=None, z=z, t=t, owner=False,
                     derive=len(self) > self.base_levels)

    def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and self._owner:
            reader.close()


def default_plane(reader: Reader) -> tuple[int, int]:
    """`(z, t)` the 2-D path shows: the middle plane of the first frame, the
    plane the volume layer calls its default (`Volume.default_view`)."""
    return int(reader.z_count) // 2, 0


def build(reader: Reader, path, extension=None, z: Optional[int] = None,
          t: Optional[int] = None, owner: bool = True,
          derive: bool = False) -> FormatPyramid:
    """The pyramid of plane (z, t) (the default plane when not given): the
    levels the file can serve, then the derived ones in `extension`. With
    `derive`, a level the file cannot serve is instead resampled from this
    plane's level above it as it is read -- a plane of a large stack without
    a pyramid of its own, whose default plane's levels were derived at
    import (`FormatPyramid.plane`); a tile there reads the plane's full
    resolution once, through the levels between."""
    from plexora.server.utils import brightfield, dicom_wsi

    if z is None or t is None:
        dz, dt = default_plane(reader)
        z = dz if z is None else z
        t = dt if t is None else t
    natives = [_Native(index, height, width)
               for index, (height, width) in enumerate(reader.levels)]
    natives.sort(key=lambda native: -native.width)
    finest = natives[0]
    dtype = np.dtype(np.uint8) if reader.rgb else np.dtype(reader.dtype)
    levels: list = []
    served = None
    for height, width in brightfield._dyadic_shapes(finest.height, finest.width):
        native = brightfield._pick_source(natives, height, width)
        if served is None and levels and not brightfield._affordable(native, height, width):
            if not derive:
                break
            served = len(levels)
        if served is not None:
            above = levels[-1]
            if reader.rgb:
                levels.append(dicom_wsi._ColorLevel(dicom_wsi._LevelRgbSource(above),
                                                    height, width))
            else:
                levels.append(dicom_wsi._MonoLevel(
                    [dicom_wsi._LevelSource(above, channel) for channel in range(reader.channels)],
                    height, width, dtype))
            continue
        if reader.rgb:
            levels.append(dicom_wsi._ColorLevel(_ColourSource(reader, native, z, t),
                                                height, width))
        else:
            levels.append(dicom_wsi._MonoLevel(
                [_ChannelSource(reader, native, channel, z, t)
                 for channel in range(reader.channels)], height, width, dtype))
    base_levels = len(levels) if served is None else served
    if served is None and extension and Path(extension).exists():
        import zarr

        derived = zarr.open_group(str(extension), mode="r")
        index = base_levels
        while str(index) in derived:
            array = derived[str(index)]
            height, width = int(array.shape[-2]), int(array.shape[-1])
            if reader.rgb:
                levels.append(dicom_wsi._ColorLevel(brightfield._PlanarSource(array),
                                                    height, width))
            else:
                levels.append(dicom_wsi._MonoLevel(
                    [dicom_wsi._ZarrSource(array, channel)
                     for channel in range(int(array.shape[0]))], height, width, dtype))
            index += 1
    return FormatPyramid(levels, reader=reader, path=path, extension=extension,
                         base_levels=base_levels, z=z, t=t, owner=owner)


@dataclass
class Description:
    """What a file is, without a pyramid or an open handle: what the import
    dialog, the channel names, the scale bar and the type detection ask."""

    format_name: str
    height: int
    width: int
    channels: int
    dtype: str
    rgb: bool
    channel_names: list = field(default_factory=list)
    pixel_size: Optional[tuple] = None
    z_count: int = 1
    t_count: int = 1
    z_step: Optional[float] = None
    level_shapes: list = field(default_factory=list)
    images: list = field(default_factory=list)
    image_index: int = 0
    note: str = ""

    @classmethod
    def of(cls, reader: Reader) -> "Description":
        height, width = reader.levels[0]
        return cls(format_name=reader.format_name, height=int(height), width=int(width),
                   channels=int(reader.channels),
                   dtype="uint8" if reader.rgb else str(np.dtype(reader.dtype)),
                   rgb=bool(reader.rgb), channel_names=list(reader.channel_names),
                   pixel_size=tuple(reader.pixel_size) if reader.pixel_size else None,
                   z_count=int(reader.z_count), t_count=int(reader.t_count),
                   z_step=reader.z_step,
                   level_shapes=[[int(h), int(w)] for h, w in reader.levels],
                   images=list(reader.images), image_index=int(reader.image_index),
                   note=reader.note())


def names_or_numbered(names: Sequence, count: int) -> list:
    """`count` channel names: the file's where it gives one, "Channel n" where
    it gives none or repeats one (a viewer cannot tell two "Cy5" apart)."""
    out = []
    seen = set()
    for index in range(count):
        name = str(names[index]).strip() if index < len(names) and names[index] else ""
        if not name or name in seen:
            name = f"Channel {index + 1}" if not name else f"{name} ({index + 1})"
        seen.add(name)
        out.append(name)
    return out


class WholeArrayReader(Reader):
    """A reader whose package reads a whole image at once (Olympus OIR and
    OIB, a Hyperion acquisition): loaded the first time a pixel is asked for,
    kept as `(T, Z, C, Y, X)`, and every read is a slice of it. These are
    camera-frame and ablation-sized images; one over `MEMORY_BYTES` is loaded
    into a temporary memory-mapped file rather than into memory."""

    MEMORY_BYTES = 1 << 30

    def __init__(self):
        super().__init__()
        self._array = None

    def _load(self, out=None) -> np.ndarray:
        """The whole image, `(T, Z, C, Y, X)`."""
        raise NotImplementedError

    def _nbytes(self) -> int:
        height, width = self.levels[0]
        return (int(height) * int(width) * int(self.channels) * int(self.z_count)
                * int(self.t_count) * np.dtype(self.dtype).itemsize)

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._array is None:
                out = "memmap" if self._nbytes() > self.MEMORY_BYTES else None
                self._array = self._load(out)
            return self._array

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        return np.ascontiguousarray(self._whole()[int(t), int(z), int(channel), y0:y1, x0:x1])

    def close(self):
        self._array = None


def as_tzcyx(array: np.ndarray, dims) -> np.ndarray:
    """`array` with axes named by `dims` as `(T, Z, C, Y, X)`: missing axes
    added, any other axis read at its first index."""
    dims = list(dims)
    for name in list(dims):
        if name not in "TZCYX":
            array = np.take(array, 0, axis=dims.index(name))
            dims.remove(name)
    for name in "TZC":
        if name not in dims:
            array = array[np.newaxis]
            dims.insert(0, name)
    return np.transpose(array, [dims.index(name) for name in "TZCYX"])
