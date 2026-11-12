"""Olympus / Evident cellSens VSI: slide scans from the VS110, VS120 and VS200,
and cellSens acquisitions.

A `.vsi` is two things in one file and a folder beside it:

- a TIFF holding small pictures (the slide's overview, its label);
- a tree of tagged properties describing every "stack" the scan made -- the
  overview, the label, each scanned region at each objective, focus maps --
  with its name, size, pixel size, dimensions and channel names;
- the pixels of each stack in `_<name>_/stack<id>/frame_t*.ets`, one ETS file
  per stack: tiles (JPEG, JPEG 2000, raw, ...) addressed by tile column, tile
  row, any further dimensions (z, channel, time) and the pyramid level.

Each scanned region is an image of its own (`reader(path, index)`; the
overview, the label, masks and focus maps are not images of the tissue and are
not counted), with its channel names and pixel size from the tree and its own
pyramid from the ETS: nothing is converted. The import offers each region as
a sample. An ETS opened on its own, without the .vsi, is ets.py's.

The property tree. Every node is a "volume": a 24-byte header (int16 header
size, int16 0x5349 "IS", int32 version, int64 offset of the first field from
the header, int32 flags whose low 28 bits count the fields, 4 bytes) and its
fields. A field is int32 type, int32 tag, uint32 offset of the next field from
the volume header (0 for the last), int32 data size, then -- when bit 27 of the
type is set -- one more int32 (a second tag, an index), then the data. Bit 28
marks a field whose data is a nested volume, starting right after it; bit 30
marks a value stored in the size field itself. The low 24 bits are the value's
type (int32 = 5, double = 10, a pair of doubles = 260, four int32 = 259, an
int16 array = 0x2003, UTF-16 text = 0x2000 and up). This module reads only the
tags it needs; it was written from the sample files themselves.
"""
from __future__ import annotations

import glob
import os
import re
import struct
from pathlib import Path
from typing import Optional

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, TileCache,
                                               assemble, chosen, names_or_numbered)

_MAGIC = 0x5349           # "IS"
_EXTENDED = 0x10000000
_EXTRA_TAG = 0x08000000
_INLINE = 0x40000000

# The tags this reader reads (the numbers as the files have them).
_STACK = 2001             # one stack; its index is the `stack<id>` folder
_DIMENSION = 2007         # one extra dimension of a stack; its index is its position
_DIMENSION_ENTRY = 2008   # one value of a dimension (a channel, ...); its index
_DIMENSION_SIZES = 2003   # int16 per extra dimension, in the ETS's order
_DIMENSION_KIND = 2023    # what a dimension is: 1 z, 2 t, 3 lambda, 4 channel
_STACK_NAME = 2030
_CHANNEL_NAME = 2419
_CHANNEL_NAME_OLD = 2021
_BOUNDARY = 2053          # x, y, width, height of the stack's full resolution
_PIXEL_SIZE = 2019        # x, y in µm
_Z_INCREMENT = 2013       # a value volume: its double is the z step in µm

_KIND_Z, _KIND_T, _KIND_LAMBDA, _KIND_C = 1, 2, 3, 4

#: Stacks that are not the tissue.
_NOT_THE_SCAN = re.compile(r"overview|label|macro|mask|focus|thumbnail|preview", re.I)


# -- the property tree --------------------------------------------------------


class _Field:
    __slots__ = ("tag", "index", "kind", "value", "children")

    def __init__(self, tag, index, kind, value=None, children=None):
        self.tag, self.index, self.kind = tag, index, kind
        self.value, self.children = value, children


def _value(kind: int, data: bytes):
    if kind & 0x2000 and kind != 0x2003:
        try:
            return data.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return None
    if kind == 0x2003:
        return struct.unpack(f"<{len(data) // 2}H", data[: len(data) // 2 * 2])
    if kind == 13:
        return data.decode("latin-1").rstrip("\x00")
    if kind == 5 and len(data) == 4:
        return struct.unpack("<i", data)[0]
    if kind == 10 and len(data) == 8:
        return struct.unpack("<d", data)[0]
    if kind in (258, 259) and len(data) == 16:
        return struct.unpack("<4i", data)
    if kind == 260 and len(data) == 16:
        return struct.unpack("<2d", data)
    if kind == 268 and len(data) == 16:
        return struct.unpack("<2d", data)
    return None


def _volume(data: bytes, start: int, depth: int = 0) -> list:
    """The fields of the volume whose header is at `start`."""
    if depth > 64 or start + 24 > len(data):
        return []
    _size, magic, _version, first, flags = struct.unpack_from("<hHiqi", data, start)
    if magic != _MAGIC:
        return []
    fields = []
    at = start + first
    for _ in range(flags & 0x0FFFFFFF):
        if at < start or at + 16 > len(data):
            break
        kind, tag, following, size = struct.unpack_from("<iiIi", data, at)
        body = at + 16
        index = None
        if kind & _EXTRA_TAG:
            index = struct.unpack_from("<i", data, body)[0]
            body += 4
        real = kind & 0xFFFFFF
        if kind & _EXTENDED:
            fields.append(_Field(tag, index, real, children=_volume(data, body, depth + 1)))
        elif kind & _INLINE:
            fields.append(_Field(tag, index, real, value=size))
        elif 0 <= size <= len(data) - body:
            fields.append(_Field(tag, index, real, value=_value(real, data[body:body + size])))
        if not following:
            break
        at = start + following
    return fields


def _walk(fields, skip=()):
    for item in fields:
        yield item
        if item.children and item.tag not in skip:
            yield from _walk(item.children, skip)


def _first(fields, tag, test=lambda value: value is not None, skip=(_STACK,)):
    for item in _walk(fields, skip):
        if item.tag == tag and item.children is None and test(item.value):
            return item.value
    return None


class _Stack:
    """What the tree says about one stack."""

    def __init__(self, ident: int, fields: list):
        self.ident = ident
        self.name = _first(fields, _STACK_NAME, lambda v: isinstance(v, str)) or ""
        boundary = _first(fields, _BOUNDARY, lambda v: isinstance(v, tuple) and len(v) == 4)
        self.size = (int(boundary[3]), int(boundary[2])) if boundary else None   # (h, w)
        scale = _first(fields, _PIXEL_SIZE, lambda v: isinstance(v, tuple) and len(v) == 2)
        self.pixel_size = (float(scale[0]), float(scale[1])) if scale and scale[0] > 0 else None
        sizes = next((item.value for item in fields
                      if item.tag == _DIMENSION_SIZES and isinstance(item.value, tuple)), ())
        self.dimension_sizes = tuple(int(size) for size in sizes)
        self.dimension_kinds: dict = {}
        self.channel_names: list = []
        self.z_step: Optional[float] = None
        for item in fields:
            if item.tag != _DIMENSION or item.children is None:
                continue
            entries = [child for child in item.children
                       if child.tag == _DIMENSION_ENTRY and child.children is not None]
            kind = next((child.value for entry in entries[:1] for child in entry.children
                         if child.tag == _DIMENSION_KIND and child.children is None), None)
            position = int(item.index or 0)
            self.dimension_kinds[position] = kind
            if kind == _KIND_C:
                self.channel_names = [
                    _first(entry.children, _CHANNEL_NAME, lambda v: isinstance(v, str))
                    or _first(entry.children, _CHANNEL_NAME_OLD, lambda v: isinstance(v, str))
                    or "" for entry in entries]
            elif kind == _KIND_Z and entries:
                for node in _walk(entries[0].children):
                    if node.tag == _Z_INCREMENT and node.children is not None:
                        step = _measure(node.children)
                        if step:
                            self.z_step = abs(step)
                        break


def _measure(fields) -> Optional[float]:
    """A value volume's number in µm: a double beside a unit text such as
    "10^-6m^1" (metres times a power of ten)."""
    number = next((item.value for item in fields
                   if item.children is None and isinstance(item.value, float)), None)
    if number is None:
        return None
    unit = next((item.value for item in fields
                 if item.children is None and isinstance(item.value, str)), "")
    found = re.match(r"10\^(-?\d+)m", unit.replace("\x00", ""))
    return float(number) * 10.0 ** (int(found.group(1)) + 6) if found else float(number)


def _stacks(path: str) -> dict:
    with open(path, "rb") as handle:
        data = handle.read()
    found = {}
    for item in _walk(_volume(data, 8), skip=()):
        if item.tag == _STACK and item.children is not None and item.index is not None:
            found.setdefault(int(item.index), _Stack(int(item.index), item.children))
    return found


# -- the ETS pixel files ----------------------------------------------------------

_PIXEL_TYPES = {1: np.int8, 2: np.uint8, 3: np.int16, 4: np.uint16, 5: np.int32,
                6: np.uint32, 9: np.float32, 10: np.float64}
_RAW, _JPEG, _JPEG2000, _JPEG_LOSSLESS, _PNG, _BMP = 0, 2, 3, 5, 8, 9


class _Ets:
    def __init__(self, path: str):
        self.path = path
        self._handle = open(path, "rb")
        head = self._handle.read(64)
        if head[:4] != b"SIS\x00":
            raise FormatUnreadable(f"{Path(path).name} is not an ETS pixel file")
        _magic, _hsize, _version, ndim, extra_at, extra_size, _pad, index_at, count = \
            struct.unpack_from("<4siiiqiiqi", head, 0)
        self._handle.seek(extra_at)
        extra = self._handle.read(max(extra_size, 160))
        if extra[:4] != b"ETS\x00":
            raise FormatUnreadable(f"{Path(path).name} has no ETS header")
        (_v, pixel_type, self.samples, _colourspace, self.compression, _quality,
         self.tile_w, self.tile_h, _tile_z) = struct.unpack_from("<9i", extra, 4)
        if pixel_type not in _PIXEL_TYPES:
            raise FormatUnreadable(f"{Path(path).name}: pixel type {pixel_type} is not read")
        self.dtype = np.dtype(_PIXEL_TYPES[pixel_type]).newbyteorder("<")
        background = struct.unpack_from("<10i", extra, 108)
        self.component_order, self.pyramid = struct.unpack_from("<ii", extra, 148)
        self.background = background
        self.ndim = ndim
        self._handle.seek(index_at)
        entry = 20 + 4 * ndim
        table = self._handle.read(entry * count)
        self.tiles: dict = {}
        for number in range(count):
            at = number * entry
            coordinate = struct.unpack_from(f"<{ndim}i", table, at + 4)
            offset, size = struct.unpack_from("<qi", table, at + 4 + 4 * ndim)
            self.tiles.setdefault(coordinate, (offset, size))
        if not self.tiles:
            raise FormatUnreadable(f"{Path(path).name} holds no tiles")
        top = [max(coordinate[axis] for coordinate in self.tiles) for axis in range(ndim)]
        self.extent = top
        self.levels = top[-1] + 1 if self.pyramid else 1

    def fill(self, dtype, samples):
        if samples == 3:
            colour = self.background[0]
            return np.array([colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF],
                            dtype=dtype)
        return np.array(self.background[0] if self.background[0] < 1 << 16 else 0).astype(dtype)

    def raw(self, offset, size) -> bytes:
        self._handle.seek(offset)
        return self._handle.read(size)

    def decode(self, data: bytes) -> np.ndarray:
        import imagecodecs

        if self.compression == _RAW:
            tile = np.frombuffer(data, dtype=self.dtype,
                                 count=self.tile_h * self.tile_w * self.samples)
            tile = tile.reshape(self.tile_h, self.tile_w, self.samples)
            if self.samples == 3 and self.component_order == 1:
                tile = tile[..., ::-1]
        elif self.compression == _JPEG:
            tile = imagecodecs.jpeg_decode(data)
        elif self.compression == _JPEG2000:
            tile = imagecodecs.jpeg2k_decode(data)
        elif self.compression == _JPEG_LOSSLESS:
            tile = imagecodecs.ljpeg_decode(data)
        elif self.compression == _PNG:
            tile = imagecodecs.png_decode(data)
        elif self.compression == _BMP:
            tile = _bmp(data)
        else:
            raise FormatUnreadable(f"ETS compression {self.compression} is not read")
        tile = np.asarray(tile)
        if tile.ndim == 2:
            tile = tile[..., None]
        return tile

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def _bmp(data: bytes) -> np.ndarray:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as picture:
        return np.asarray(picture)


# -- the reader ---------------------------------------------------------------


def _ets_files(vsi: Path) -> dict:
    """stack id -> its ETS file, from `_<name>_/stack<id>/`."""
    folder = vsi.parent / f"_{vsi.stem}_"
    found = {}
    for stack in glob.glob(str(folder / "stack*")):
        match = re.fullmatch(r"stack(\d+)", os.path.basename(stack))
        if not match:
            continue
        files = sorted(name for name in glob.glob(os.path.join(stack, "frame_t*.ets")))
        if files:
            found[int(match.group(1))] = files[0]
    return found


class VsiReader(Reader):
    format_name = "Olympus VSI"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        vsi = Path(path)
        files = _ets_files(vsi)
        if not files:
            raise FormatUnreadable(
                f"The pixels of {vsi.name} are in the folder _{vsi.stem}_ beside it, "
                "which is not there. Keep the .vsi file and its folder together.")
        try:
            stacks = _stacks(path)
        except Exception:      # the pixels are still readable without names
            stacks = {}
        candidates = []
        for ident, ets_path in sorted(files.items()):
            stack = stacks.get(ident)
            name = stack.name if stack else ""
            if name and _NOT_THE_SCAN.search(name):
                continue
            candidates.append((ident, ets_path, stack))
        if not candidates:
            # Nothing named as a scan: the stack with the most tiles at level 0.
            candidates = [(ident, ets_path, stacks.get(ident))
                          for ident, ets_path in sorted(files.items())]
            candidates.sort(key=lambda entry: -os.path.getsize(entry[1]))
        self.images = [(stack.name if stack and stack.name else f"stack {ident}")
                       for ident, _ets, stack in candidates]
        self.image_index = chosen(self.images, index, path)
        ident, ets_path, stack = candidates[self.image_index]
        self._ets = _Ets(ets_path)
        self._cache = TileCache()
        self._open(stack)

    def _open(self, stack: Optional[_Stack]):
        ets = self._ets
        extra = ets.ndim - 2 - (1 if ets.pyramid else 0)
        sizes = [ets.extent[2 + axis] + 1 for axis in range(extra)]
        kinds = dict(stack.dimension_kinds) if stack else {}
        if extra and not kinds and extra == 1:
            kinds = {0: _KIND_C}
        self._axis = {"z": None, "c": None, "t": None}
        for position in range(extra):
            kind = kinds.get(position)
            name = {_KIND_Z: "z", _KIND_C: "c", _KIND_LAMBDA: "c", _KIND_T: "t"}.get(kind)
            if name and self._axis[name] is None:
                self._axis[name] = position
        if self._axis["c"] is not None and ets.samples > 1 and sizes[self._axis["c"]] == 1:
            # One plane of colour samples (an H&E scan, an overview): its
            # channels are the samples, not that dimension of one.
            self._axis["c"] = None
        self._extra = extra
        self.rgb = ets.samples == 3 and ets.dtype == np.uint8
        if self._axis["c"] is not None:
            self.channels = sizes[self._axis["c"]]
        else:
            self.channels = 3 if ets.samples == 3 else 1
        self.z_count = sizes[self._axis["z"]] if self._axis["z"] is not None else 1
        self.t_count = sizes[self._axis["t"]] if self._axis["t"] is not None else 1
        self.dtype = np.dtype(ets.dtype.newbyteorder("="))
        grid_h = (ets.extent[1] + 1) * ets.tile_h
        grid_w = (ets.extent[0] + 1) * ets.tile_w
        height, width = (stack.size if stack and stack.size else (grid_h, grid_w))
        height, width = min(height, grid_h), min(width, grid_w)
        self.levels = []
        for level in range(ets.levels):
            self.levels.append((max(1, -(-height // (1 << level))),
                                max(1, -(-width // (1 << level)))))
        self.pixel_size = stack.pixel_size if stack else None
        self.z_step = stack.z_step if stack else None
        names = stack.channel_names if stack else []
        if self.rgb:
            self.channel_names = ["Red", "Green", "Blue"]
        else:
            self.channel_names = names_or_numbered(names, self.channels)
        self._fill = ets.fill(self.dtype, 3 if self.rgb else 0)

    def _coordinate(self, level, tx, ty, channel, z, t) -> tuple:
        values = [tx, ty] + [0] * self._extra
        for axis, value in (("c", channel), ("z", z), ("t", t)):
            if self._axis[axis] is not None:
                values[2 + self._axis[axis]] = int(value)
        if self._ets.pyramid:
            values.append(int(level))
        return tuple(values)

    def _tile(self, level, channel, z, t, ty, tx):
        key = self._coordinate(level, tx, ty, channel, z, t)
        found = self._ets.tiles.get(key)
        if found is None:
            return None

        def make():
            with self._lock:
                data = self._ets.raw(*found)
            return self._ets.decode(data)

        return self._cache.get(key, make)

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        ets = self._ets
        sample = 0
        if self._axis["c"] is None and ets.samples > 1:
            sample, channel = int(channel), 0

        def tile(ty, tx):
            found = self._tile(level, channel, z, t, ty, tx)
            return None if found is None else found[..., min(sample, found.shape[-1] - 1)]

        fill = self._fill[sample] if np.ndim(self._fill) else self._fill
        return assemble(tile, ets.tile_h, ets.tile_w, y0, y1, x0, x1, self.dtype, fill=fill)

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        ets = self._ets
        return assemble(lambda ty, tx: self._tile(level, 0, z, t, ty, tx),
                        ets.tile_h, ets.tile_w, y0, y1, x0, x1, np.uint8, samples=3,
                        fill=self._fill)

    def close(self):
        ets, self._ets = getattr(self, "_ets", None), None
        if ets is not None:
            ets.close()


def reader(path: str, index: int = 0) -> VsiReader:
    return VsiReader(path, index)
