"""Zeiss AxioVision ZVI (`.zvi`): AxioVision's own image files, 1990s to 2021.

A ZVI is a Compound File Binary container (OLE2 structured storage, read by
`cfb.py` here) holding one storage, `Image`, with:

- `Image/Contents`: the image's header;
- `Image/Tags/Contents`: the image's tags (scaling, microscope, the first
  plane's acquisition);
- `Image/Scaling/Contents`: the scaling again, as a record of its own;
- `Image/Item(n)/Contents`, n = 0, 1, ...: one plane each, a header then the
  pixels, uncompressed, to the end of the stream;
- `Image/Item(n)/Tags/Contents`: that plane's tags: its z, channel and time
  index, its channel's name, focus position, exposure, ...;
- and `Layers`, `RootFolder` (the acquisition setup), `DisplayItem`, a
  `Thumbnail`, which a viewer does not need.

Every header and tag list is a sequence of typed values, each a uint16 type
(the OLE VARIANT codes) then its value: 0 empty, 2 int16, 3 int32, 4 float32,
5 float64, 7 a date (float64), 8 text (int32 byte length, UTF-16), 9 a 16-byte
reference, 11 a boolean (int16), 65 bytes (int32 length, the bytes), 69 the
name of a stored object (as text), 72 a 16-byte class id.

The headers (`Image/Contents` and each item's): the version (int32), the
type (int32), two names (empty or text), the width, the height, the depth,
the pixel format, the plane count (`Image/Contents`: the number of items)
and the valid bits per pixel (int32s), 16 bytes, 128 bytes, then the names
of the storages below. In an item the 128 bytes are 32 int32s whose third and
fourth are its z and channel index (the fifth is taken as its time index,
which no sample has shown), and after `Tags` and three references come 28
bytes: 0x10002000, the width, the height, the depth, the bytes per pixel, the
pixel format and the valid bits, int32 each; the pixels follow, row by row,
at the end of the stream. Pixel formats: 1 BGR 8 bits each, 2 BGRA, 3 grey
uint8, 4 grey uint16, 5 int32, 6 float32, 7 float64, 8 BGR 16 bits each;
the bytes per pixel are checked against the format and decide when they
disagree.

A tag list: version (int32), count (int32), then `count` triples: the value,
the tag's id (int32), an attribute (int32). The ids read here:

- 769, 772, 775: the size of a pixel in x and y and of a z step, in the unit
  given by 770, 773 and 776 (76 is µm in every sample; 75 and 77 are taken as
  mm and nm, the neighbours of 76; any other unit is not trusted);
- 2819, 2820, 2821: a plane's z, channel and time index; 2822 and 2827 are
  taken as its tile and scene index (no sample varies them);
- 1284: the channel's name ("Hoechst 33342", "Cy3"); 2051 the filter set's
  ("49 DAPI BP"); 0x1000111 the emission wavelength in nm;
- 2072: the focus position in µm.

What Plexora shows: the planes sorted into channels, z and time by their
indices (the tags; the 128-byte coordinates when an item has no tags), the
channel names from the tags, the pixel size and z step from the scaling.
Grey planes are channels; a BGR plane is a colour picture (`rgb`). A file in
which several planes share the same z, channel and time -- a MosaiX tile set
or a multi-position acquisition, which none of the samples was -- is several
images, grouped by their tile and scene index (or, failing those, by the
order the planes come in), one opened at a time and never stitched.

A region reads the rows it covers and nothing else: each plane's chain of
sectors is mapped to file offsets and its rows read in bands of about 256
KB (whole rows), the last 64 kept.

Written from the sample files (and the Microsoft CFB specification for the
container); Zeiss's own ZVI specification is not public.
"""
from __future__ import annotations

import re
import struct
from collections import OrderedDict
from pathlib import Path
from statistics import median
from typing import Optional

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, TileCache,
                                               assemble, names_or_numbered)
from plexora.server.utils.formats.cfb import STREAM, CompoundFile, CompoundFileError

# -- typed values ---------------------------------------------------------------

_EMPTY, _NULL, _I2, _I4, _R4, _R8, _CY, _DATE, _BSTR, _DISPATCH = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9
_ERROR, _BOOL, _I1, _UI1, _UI2, _UI4 = 10, 11, 16, 17, 18, 19
_I8, _UI8, _INT, _UINT = 20, 21, 22, 23
_FILETIME, _BLOB, _STORED_OBJECT, _CLSID = 64, 65, 69, 72

_FIXED = {_I2: "<h", _BOOL: "<h", _UI2: "<H", _I4: "<i", _INT: "<i", _ERROR: "<i",
          _UI4: "<I", _UINT: "<I", _R4: "<f", _R8: "<d", _DATE: "<d", _CY: "<q",
          _I8: "<q", _UI8: "<Q", _FILETIME: "<Q", _I1: "<b", _UI1: "<B"}


def _values(data: bytes, start: int = 0) -> list:
    """`(offset, type, value)` of each typed value from `start`, up to the
    end or the first type this reader does not know."""
    out, at, end = [], start, len(data)
    while at + 2 <= end:
        kind = struct.unpack_from("<H", data, at)[0]
        body = at + 2
        if kind in (_EMPTY, _NULL):
            out.append((at, kind, None))
            at = body
        elif kind in _FIXED:
            form = _FIXED[kind]
            size = struct.calcsize(form)
            if body + size > end:
                break
            out.append((at, kind, struct.unpack_from(form, data, body)[0]))
            at = body + size
        elif kind in (_BSTR, _STORED_OBJECT, _BLOB):
            if body + 4 > end:
                break
            size = struct.unpack_from("<i", data, body)[0]
            if size < 0 or body + 4 + size > end:
                break
            raw = data[body + 4:body + 4 + size]
            value = raw if kind == _BLOB else raw.decode("utf-16-le", "replace").rstrip("\x00")
            out.append((at, kind, value))
            at = body + 4 + size
        elif kind in (_DISPATCH, _CLSID):
            if body + 16 > end:
                break
            out.append((at, kind, data[body:body + 16]))
            at = body + 16
        else:
            break
    return out


def _tags(data: bytes) -> dict:
    """A tag list's values by tag id (the first of a repeated id)."""
    values = _values(data)
    if len(values) < 2 or values[1][1] != _I4:
        return {}
    found: dict = {}
    entries = values[2:]
    for index in range(min(int(values[1][2]), len(entries) // 3)):
        (_a, _k, value), (_b, id_kind, ident), _attribute = entries[3 * index:3 * index + 3]
        if id_kind == _I4:
            found.setdefault(int(ident), value)
    return found


def _int(value) -> Optional[int]:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# -- the tags read ----------------------------------------------------------------

_SCALE_X, _UNIT_X, _SCALE_Y, _UNIT_Y, _SCALE_Z, _UNIT_Z = 769, 770, 772, 773, 775, 776
_INDEX_Z, _INDEX_C, _INDEX_T, _INDEX_TILE, _INDEX_SCENE = 2819, 2820, 2821, 2822, 2827
_CHANNEL_NAME, _FILTER_SET, _EMISSION, _FOCUS = 1284, 2051, 0x1000111, 2072

#: Scaling units as µm per unit: 76 is µm in every sample; 75 and 77, its
#: neighbours, are taken as mm and nm.
_UNITS = {75: 1000.0, 76: 1.0, 77: 0.001}


def _micrometres(value, unit) -> Optional[float]:
    if not isinstance(value, float) or not value > 0:
        return None
    factor = _UNITS.get(_int(unit))
    return float(value) * factor if factor else None


# -- pixels -------------------------------------------------------------------------

#: pixel format -> (sample type, samples per pixel); a colour format's samples
#: are blue, green, red (and alpha).
_FORMATS = {1: (np.uint8, 3), 2: (np.uint8, 4), 3: (np.uint8, 1), 4: (np.uint16, 1),
            5: (np.int32, 1), 6: (np.float32, 1), 7: (np.float64, 1), 8: (np.uint16, 3)}
#: bytes per pixel -> the same, for a format number this reader does not know
_BY_SIZE = {1: (np.uint8, 1), 2: (np.uint16, 1), 3: (np.uint8, 3), 6: (np.uint16, 3)}
_RAW_VERSION = 0x10002000

#: A cached band of rows is about this many bytes: a viewer tile's rows are
#: three or four bands of a camera frame, and its neighbours share them.
_BAND_BYTES = 1 << 18


def _pixel_type(pixel_format, size) -> Optional[tuple]:
    known = _FORMATS.get(pixel_format)
    if known and (not size or np.dtype(known[0]).itemsize * known[1] == size):
        return known
    return _BY_SIZE.get(size)


class _Item:
    """One plane: where its pixels are and what its tags say."""

    __slots__ = ("number", "entry", "offset", "stride", "width", "height", "dtype",
                 "samples", "rows", "tags", "z", "c", "t", "group")

    def __init__(self, number, entry, offset, stride, width, height, dtype, samples, tags,
                 coordinates):
        self.number, self.entry = number, entry
        self.offset, self.stride = offset, stride
        self.width, self.height = width, height
        self.dtype, self.samples = dtype, samples
        self.rows = max(1, min(height, _BAND_BYTES // max(1, stride)))
        self.tags = tags

        def index(tag, position):
            value = _int(tags.get(tag))
            if value is None and len(coordinates) > position:
                value = coordinates[position]
            return max(0, value or 0)

        self.z, self.c, self.t = index(_INDEX_Z, 2), index(_INDEX_C, 3), index(_INDEX_T, 4)
        others = tuple(value for position, value in enumerate(coordinates)
                       if position not in (2, 3, 4))
        self.group = (_int(tags.get(_INDEX_SCENE)) or 0, _int(tags.get(_INDEX_TILE)) or 0,
                      others)


def _header_size(values) -> tuple:
    """`(width, height, pixel format)` from a header's typed values."""
    ints = [value if kind == _I4 else None for _at, kind, value in values[:10]]
    ints += [None] * (10 - len(ints))
    return ints[4], ints[5], ints[7]


def _item(cf: CompoundFile, number: int, storage, fallback) -> Optional[_Item]:
    children = cf.children(storage)
    contents = children.get("CONTENTS")
    if contents is None or contents.kind != STREAM:
        return None
    offset, size = None, 0
    for length in (1024, 8192):            # the header is 324 bytes in every sample
        head = cf.read(contents, 0, min(contents.size, length))
        values = _values(head)
        width, height, pixel_format = _header_size(values)
        width, height = width or fallback[0], height or fallback[1]
        pixel_format = pixel_format or fallback[2]
        coordinates: tuple = ()
        for _at, kind, value in values:
            if kind == _BLOB and len(value) == 128:
                coordinates = struct.unpack("<32i", value)
                break
        if not width or not height or width <= 0 or height <= 0:
            return None
        # The 28-byte record right before the pixels: found by its width and
        # height, which nothing before it has side by side.
        mark = struct.pack("<ii", width, height)
        at = head.find(mark, 4)
        while 4 <= at <= len(head) - 24:
            version, _w, _h, _depth, bytes_each, form, _bits = \
                struct.unpack_from("<7i", head, at - 4)
            if 0 < bytes_each <= 16 and (version == _RAW_VERSION or _pixel_type(form, bytes_each)):
                offset, size, pixel_format = at - 4 + 28, bytes_each, form or pixel_format
                break
            at = head.find(mark, at + 1)
        if offset is not None or length >= contents.size:
            break
    found = _pixel_type(pixel_format, size if offset is not None else 0)
    if found is None:
        raise FormatUnreadable(
            f"ZVI pixel format {pixel_format} ({size} bytes a pixel) is not read")
    dtype, samples = np.dtype(found[0]), found[1]
    row = width * dtype.itemsize * samples
    padded = -(-row // 4) * 4
    if offset is None:
        # No record: the pixels are the stream's last bytes.
        offset = contents.size - height * row
        if offset < 0:
            return None
    stride = padded if contents.size - offset == height * padded else row
    tags_storage = children.get("TAGS")
    tags: dict = {}
    if tags_storage is not None:
        stream = (tags_storage if tags_storage.kind == STREAM
                  else cf.children(tags_storage).get("CONTENTS"))
        if stream is not None and stream.kind == STREAM:
            tags = _tags(cf.read(stream))
    return _Item(number, contents, offset, stride, width, height, dtype, samples, tags,
                 coordinates)


def _scaling(cf: CompoundFile) -> list:
    """`(value, unit)` of x, y and z from `Image/Scaling/Contents`: each a
    float64 followed by an int32."""
    entry = cf.find("Image/Scaling/Contents")
    if entry is None or entry.kind != STREAM:
        return []
    values = _values(cf.read(entry, 0, 4096))
    return [(values[i][2], values[i + 1][2]) for i in range(len(values) - 1)
            if values[i][1] == _R8 and values[i + 1][1] == _I4][:3]


# -- the reader ----------------------------------------------------------------------

_ITEM = re.compile(r"ITEM\((\d+)\)")


class ZviReader(Reader):
    format_name = "Zeiss ZVI"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        self._cf = None
        handle = open(path, "rb")
        try:
            try:
                self._cf = CompoundFile(handle, self._lock)
            except CompoundFileError as exc:
                raise FormatUnreadable(f"{Path(path).name} is not a ZVI file: {exc}") from None
            self._cache = TileCache(capacity=64)
            self._open(Path(path), int(index))
        except BaseException:
            if self._cf is None:
                handle.close()
            self.close()
            raise

    def _open(self, path: Path, index: int):
        cf = self._cf
        image = cf.find("Image")
        if image is None or image.kind == STREAM:
            raise FormatUnreadable(f"{path.name} holds no AxioVision image")
        contents = cf.find("Image/Contents")
        header = _values(cf.read(contents, 0, 4096)) if contents is not None else []
        fallback = _header_size(header)
        tags_entry = cf.find("Image/Tags/Contents")
        image_tags = _tags(cf.read(tags_entry)) if tags_entry is not None else {}
        numbered = sorted((int(match.group(1)), entry)
                          for name, entry in cf.children(image).items()
                          for match in [_ITEM.fullmatch(name)] if match and entry.kind != STREAM)
        items = [item for number, storage in numbered
                 for item in [_item(cf, number, storage, fallback)] if item is not None]
        if not items:
            raise FormatUnreadable(f"{path.name} holds no image planes")
        groups = self._groups(items)
        if len(groups) == 1:
            self.images = [path.name]
        else:
            self.images = [name for name, _members in groups]
        if not 0 <= index < len(groups):
            raise FormatUnreadable(
                f"{path.name} holds {len(groups)} image{'s' if len(groups) > 1 else ''}; "
                f"there is no image {index}")
        self.image_index = index
        self._describe(groups[index][1], image_tags, cf)

    @staticmethod
    def _groups(items) -> list:
        """`(name, items)` of each image: one, unless planes repeat a z,
        channel and time -- then by tile and scene, then by order."""
        coordinates = [(item.c, item.z, item.t) for item in items]
        if len(set(coordinates)) == len(coordinates):
            return [("", items)]
        by_key: OrderedDict = OrderedDict()
        for item in items:
            by_key.setdefault(item.group, []).append(item)
        groups = []
        for (scene, tile, _others), members in by_key.items():
            split: list = []
            seen: dict = {}
            for item in members:
                key = (item.c, item.z, item.t)
                repeat = seen.get(key, 0)
                seen[key] = repeat + 1
                while len(split) <= repeat:
                    split.append([])
                split[repeat].append(item)
            for part in split:
                groups.append(((scene, tile), part))
        # Named by scene and tile where those tell the images apart, by
        # number where they do not.
        keys = [key for key, _members in groups]
        scenes = len({scene for scene, _tile in keys}) > 1
        tiles = len({tile for _scene, tile in keys}) > 1
        distinct = len(set(keys)) == len(keys)
        named = []
        for number, ((scene, tile), members) in enumerate(groups):
            words = (([f"scene {scene + 1}"] if scenes else [])
                     + ([f"tile {tile + 1}"] if tiles else []))
            name = ", ".join(words) if words and distinct else f"image {number + 1}"
            named.append((name[0].upper() + name[1:], members))
        return named

    def _describe(self, items, image_tags: dict, cf: CompoundFile):
        first = items[0]
        cs = sorted({item.c for item in items})
        zs = sorted({item.z for item in items})
        ts = sorted({item.t for item in items})
        self._slots = {(cs.index(item.c), zs.index(item.z), ts.index(item.t)): item
                       for item in reversed(items)}
        self.levels = [(max(item.height for item in items), max(item.width for item in items))]
        self.z_count, self.t_count = len(zs), len(ts)
        self.dtype = np.dtype(first.dtype)
        samples = first.samples
        names = []
        for c in cs:
            item = next(item for item in items if item.c == c)
            name = item.tags.get(_CHANNEL_NAME)
            if not (isinstance(name, str) and name.strip()):
                name = item.tags.get(_FILTER_SET)
            if not (isinstance(name, str) and name.strip()):
                wave = _int(item.tags.get(_EMISSION))
                name = f"{wave} nm" if wave and wave > 0 else ""
            names.append(name.strip())
        if len(cs) == 1 and not names[0]:
            name = image_tags.get(_CHANNEL_NAME)
            names[0] = name.strip() if isinstance(name, str) else ""
        if samples == 1:
            self._channels = [(index, 0) for index in range(len(cs))]
            self.channels = len(cs)
            self.channel_names = names_or_numbered(names, self.channels)
        else:
            # Blue, green, red in the file; red, green, blue here.
            self._channels = [(index, sample) for index in range(len(cs)) for sample in (2, 1, 0)]
            self.channels = len(self._channels)
            self.rgb = len(cs) == 1 and self.dtype == np.uint8
            colours = ("Red", "Green", "Blue")
            if len(cs) == 1:
                self.channel_names = list(colours)
            else:
                self.channel_names = names_or_numbered(
                    [f"{names[index] or f'Channel {index + 1}'} {colour.lower()}"
                     for index in range(len(cs)) for colour in colours], self.channels)
        scaling = _scaling(cf)
        size_x = _micrometres(image_tags.get(_SCALE_X), image_tags.get(_UNIT_X))
        size_y = _micrometres(image_tags.get(_SCALE_Y), image_tags.get(_UNIT_Y))
        if size_x is None and scaling:
            size_x = _micrometres(*scaling[0])
            size_y = _micrometres(*scaling[1]) if len(scaling) > 1 else None
        if size_x is not None:
            self.pixel_size = (size_x, size_y or size_x)
        if self.z_count > 1:
            step = _micrometres(image_tags.get(_SCALE_Z), image_tags.get(_UNIT_Z))
            if step is None and len(scaling) > 2:
                step = _micrometres(*scaling[2])
            if step is None:
                step = self._focus_step(items, cs[0], ts[0])
            self.z_step = step

    @staticmethod
    def _focus_step(items, c, t) -> Optional[float]:
        """The z step from the planes' focus positions (µm)."""
        focus = sorted((item.z, item.tags.get(_FOCUS)) for item in items
                       if item.c == c and item.t == t and isinstance(item.tags.get(_FOCUS), float))
        steps = [abs(b[1] - a[1]) for a, b in zip(focus, focus[1:]) if b[0] != a[0]]
        step = median(steps) if steps else 0.0
        return float(step) if step > 0 else None

    def note(self) -> str:
        if len(self.images) <= 1:
            return ""
        return (f"This Zeiss ZVI file holds {len(self.images)} images (the tiles or "
                f"positions of one acquisition, which Plexora does not stitch); this is "
                f"\"{self.images[self.image_index]}\".")

    # -- pixels ------------------------------------------------------------------------

    def _band(self, item: _Item, band: int) -> np.ndarray:
        """Rows `band * item.rows` on of a plane, `(rows, width[, samples])`."""

        def make():
            first = band * item.rows
            count = min(item.rows, item.height - first)
            data = self._cf.read(item.entry, item.offset + first * item.stride,
                                 count * item.stride)
            data = data.ljust(count * item.stride, b"\x00")
            row = item.width * item.dtype.itemsize * item.samples
            raw = np.frombuffer(data, dtype=np.uint8).reshape(count, item.stride)
            if item.stride != row:
                raw = np.ascontiguousarray(raw[:, :row])
            pixels = raw.view(item.dtype.newbyteorder("<")).reshape(
                (count, item.width) + ((item.samples,) if item.samples > 1 else ()))
            return pixels.astype(self.dtype, copy=False)

        return self._cache.get((item.number, band), make)

    def _tile_reader(self, item: _Item, sample: Optional[int]):
        def tile(ty, tx):
            if tx != 0 or ty * item.rows >= item.height:
                return None
            block = self._band(item, ty)
            if block.ndim == 2:
                return block
            if sample is None:                     # colour: red, green, blue
                return block[..., 2::-1]
            return block[..., min(sample, block.shape[-1] - 1)]
        return tile

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        index, sample = self._channels[int(channel)]
        item = self._slots.get((index, int(z), int(t)))
        if item is None:              # a plane the acquisition did not record
            return np.zeros((int(y1) - int(y0), int(x1) - int(x0)), dtype=self.dtype)
        return assemble(self._tile_reader(item, sample), item.rows, item.width,
                        int(y0), int(y1), int(x0), int(x1), self.dtype)

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        item = self._slots.get((0, int(z), int(t)))
        if item is None:
            return np.zeros((int(y1) - int(y0), int(x1) - int(x0), 3), dtype=np.uint8)
        return assemble(self._tile_reader(item, None), item.rows, item.width,
                        int(y0), int(y1), int(x0), int(x1), np.uint8, samples=3)

    def close(self):
        cf, self._cf = getattr(self, "_cf", None), None
        if cf is not None:
            cf.close()


def reader(path: str, index: int = 0) -> ZviReader:
    return ZviReader(path, index)
