"""KFBio (Konfoong) KFB: brightfield slide scans from the KF-PRO scanners.

A `.kfb` is one file: a header, three small pictures (the slide's macro, its
label, a thumbnail of the scanned area), the JPEG tiles of every level of the
scan's own pyramid one after another, and an index of those tiles. Plexora
shows the scan, as an RGB picture, with the pixel size and the pyramid the file
holds: nothing is converted. This module was written from sample files
(version 1.7, a KFSCAN005 20x scan); every number is little-endian.

Sections are bracketed by marker words `F1 0n EE EE` ... `FF 0n EE EE`
(n = 1 the header, 2 the macro and the thumbnail, 3 the label, 4 a tile record).

The header, at 0 (96 bytes, `F1 01 EE EE` ... `FF 01 EE EE` at 0x5C):

    0x00  u32  0xEEEE01F1
    0x04  4s   "KFB\\0"
    0x0C  f32  format version (1.7)
    0x10  u32  number of tile records in the index
    0x14  u32  height of the finest level, in pixels
    0x18  u32  width of the finest level
    0x1C  u32  the scan's magnification (20): the finest level's `scale`
    0x20  4s   compression, "JPEG" (the only one read)
    0x28  u32  36000, meaning unknown
    0x2C  u64  probably the scan's Unix time
    0x34  u32  offset of the macro section
    0x38  u32  offset of the label section
    0x3C  u64  offset of the thumbnail section
    0x44  u64  offset of the tile index
    0x4C  f32  µm per pixel of the finest level (0.486 at 20x)
    0x58  u32  tile side (256)

After it, at 0x60, a list of properties: u32 count, then per property u32 tag,
u32 byte count and the bytes -- tag 29 the scanner ("KFSCAN00500113025"), 30
the version again, 31 the tile side again, 32..37 numbers whose meaning is not
known (0, 0, 1, 3 and a pair that may be the stage position). Not read here.

A picture section (macro, label, thumbnail): `F1 0n EE EE`, eleven u32 -- 1,
height, width, samples (3), the JPEG's byte count, the section's header size
(52), 0, the magnification (20; 0 for the thumbnail), 0, 0, 0 -- `FF 0n EE EE`,
then the JPEG (JFIF, YCbCr) at the section's offset + its header size. The
macro is the whole slide with the scanned area boxed in red (812 x 2458 in the
sample), the label the slide's label end (812 x 952), the thumbnail the
scanned area (100 x 228). `KfbReader.associated(name)` decodes one.

The tile index: `count` records of 64 bytes, finest level first and each level
in scan order (column strips, not raster order):

    0   u32  0xEEEE04F1
    4   u32  x of the tile's top-left corner, in pixels of its own level
    8   u32  y
    12  u32  width (the JPEG's own width: 256, less at the right edge)
    16  u32  height (256, less at the bottom of a strip)
    20  f32  the level's magnification: 20, 10, 5, ... 20/65536 for the
             finest level of a 20x scan, halving all the way down to 1 x 1
    24  u32  0 in every record seen (the channel or the focal plane of a
             fluorescence `.kfbf` or a z-scan, which this reader leaves alone)
    28  u32  0 likewise
    32  u32  the JPEG's byte count
    36  i64  the JPEG's offset from the start of the index (negative: the tiles
             come before it, back to back)
    44  4u32 20, 20, 20, 20 in every record seen, meaning unknown
    60  u32  0xEEEE04FF

A level is every record sharing a magnification; its size is the finest
level's halved (rounded down) by the ratio of magnifications, which is also
where its tiles end. Tiles sit on a grid of the tile side. The scanner works
in strips eight tiles wide whose bottoms differ by a few pixels, so the
finest level's bottom edge is a staircase of short tiles with nothing below:
an area no record covers is unscanned glass and reads white (255). Levels of
the same size as the one before (two 1 x 1 levels at the bottom) are skipped.
"""
from __future__ import annotations

import io
import os
import struct
from pathlib import Path
from typing import Optional

import numpy as np

from plexora.server.utils.formats.base import FormatUnreadable, Reader, TileCache

_HEADER_OPEN = 0xEEEE01F1
_TILE_OPEN_WORD = 0xEEEE04F1
_TILE_OPEN = struct.pack("<I", _TILE_OPEN_WORD)
_TILE_CLOSE = b"\xff\x04\xee\xee"
_HEADER_SIZE = 0x60
_WHITE = 255

#: The parts of a tile record this reader reads, at their offsets in it.
_RECORD_FIELDS = {"names": ["open", "x", "y", "w", "h", "scale", "plane_a", "plane_b",
                            "size", "offset"],
                  "formats": ["<u4", "<u4", "<u4", "<u4", "<u4", "<f4", "<u4", "<u4",
                              "<u4", "<i8"],
                  "offsets": [0, 4, 8, 12, 16, 20, 24, 28, 32, 36]}

#: The pictures beside the scan: name -> where the header keeps the section's offset.
_ASSOCIATED = {"macro": ("<I", 0x34), "label": ("<I", 0x38), "thumbnail": ("<Q", 0x3C)}


def _decode_jpeg(data: bytes) -> np.ndarray:
    """An RGB uint8 picture of one JPEG: imagecodecs where there is one (its
    browser stand-in included), Pillow otherwise. A damaged one is
    `FormatUnreadable`, not the decoder's own error."""
    try:
        return _decoded_jpeg(data)
    except Exception as error:  # noqa: BLE001 -- whichever decoder said it
        raise FormatUnreadable(f"a tile of this slide is damaged ({error})") from error


def _decoded_jpeg(data: bytes) -> np.ndarray:
    try:
        import imagecodecs
    except ImportError:
        imagecodecs = None
    if imagecodecs is not None:
        picture = np.asarray(imagecodecs.jpeg_decode(data))
    else:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            picture = np.asarray(image.convert("RGB"))
    if picture.ndim == 2:
        picture = np.repeat(picture[..., None], 3, axis=-1)
    elif picture.shape[-1] > 3:
        picture = picture[..., :3]
    return picture.astype(np.uint8, copy=False)


class _Level:
    __slots__ = ("height", "width", "cells")

    def __init__(self, height: int, width: int):
        self.height, self.width = int(height), int(width)
        #: (cell row, cell column) -> numbers of the tiles overlapping that cell.
        self.cells: dict = {}


class KfbReader(Reader):
    format_name = "KFBio KFB"

    def __init__(self, path: str):
        super().__init__()
        self.path = str(path)
        self._handle = open(self.path, "rb")
        try:
            self._open()
        except Exception:
            self.close()
            raise

    # -- opening -------------------------------------------------------------

    def _bytes(self, offset: int, size: int) -> bytes:
        with self._lock:
            self._handle.seek(offset)
            return self._handle.read(size)

    def _open(self) -> None:
        name = Path(self.path).name
        file_size = os.fstat(self._handle.fileno()).st_size
        head = self._bytes(0, _HEADER_SIZE)
        if len(head) < _HEADER_SIZE or struct.unpack_from("<I", head, 0)[0] != _HEADER_OPEN \
                or head[4:7] != b"KFB":
            raise FormatUnreadable(f"{name} is not a KFBio KFB file")
        self.version = round(struct.unpack_from("<f", head, 0x0C)[0], 3)
        count, height, width, self.magnification = struct.unpack_from("<4I", head, 0x10)
        compression = head[0x20:0x24].rstrip(b"\x00").decode("latin-1", "replace")
        if compression != "JPEG":
            raise FormatUnreadable(
                f"{name} stores its tiles as {compression or 'an unnamed compression'}; "
                "Plexora reads KFB files whose tiles are JPEG")
        index_at = struct.unpack_from("<Q", head, 0x44)[0]
        micrometres = struct.unpack_from("<f", head, 0x4C)[0]
        self._tile_side = struct.unpack_from("<I", head, 0x58)[0] or 256
        self._file_size = file_size
        self._associated_at = {key: struct.unpack_from(fmt, head, at)[0]
                               for key, (fmt, at) in _ASSOCIATED.items()}
        if not count or not height or not width:
            raise FormatUnreadable(f"{name} holds no scan")

        records = self._index(name, index_at, count, file_size)
        self._levels = self._build_levels(records, int(height), int(width))
        self.levels = [(level.height, level.width) for level in self._levels]
        self.channels = 3
        self.rgb = True
        self.dtype = np.dtype(np.uint8)
        self.channel_names = ["Red", "Green", "Blue"]
        self.pixel_size = ((float(micrometres), float(micrometres))
                           if 0 < micrometres < 1000 and np.isfinite(micrometres) else None)
        self.images = [Path(self.path).stem]
        self.image_index = 0
        self._cache = TileCache()

    def _index(self, name: str, index_at: int, count: int, file_size: int) -> dict:
        """The tile records as columns, each tile's absolute position in
        `position`."""
        probe = self._bytes(index_at, 256)
        if probe[:4] != _TILE_OPEN:
            raise FormatUnreadable(f"{name}: the tile index is not where the header says")
        close = probe.find(_TILE_CLOSE)
        if close < 44:
            raise FormatUnreadable(f"{name}: the tile index's records are not read")
        record_size = close + 4
        if index_at + count * record_size > file_size:
            raise FormatUnreadable(f"{name} is cut short: its tile index runs past its end")
        raw = self._bytes(index_at, count * record_size)
        dtype = np.dtype(dict(_RECORD_FIELDS, itemsize=record_size))
        records = np.frombuffer(raw, dtype=dtype, count=count)
        if not np.all(records["open"] == _TILE_OPEN_WORD):
            raise FormatUnreadable(f"{name}: its tile index is damaged")
        plane = (records["plane_a"] == 0) & (records["plane_b"] == 0)
        if plane.any() and not plane.all():
            records = records[plane]          # the first channel / focal plane only
        position = records["offset"].astype(np.int64) + np.int64(index_at)
        if not self._starts_jpeg(position, records):
            absolute = records["offset"].astype(np.int64)
            if not self._starts_jpeg(absolute, records):
                raise FormatUnreadable(f"{name}: its tile index does not point at its tiles")
            position = absolute
        keep = ((records["w"] > 0) & (records["h"] > 0) & (records["size"] > 0)
                & (position >= 0) & (position + records["size"] <= file_size))
        return {"x": records["x"][keep].astype(np.int64),
                "y": records["y"][keep].astype(np.int64),
                "w": records["w"][keep].astype(np.int64),
                "h": records["h"][keep].astype(np.int64),
                "scale": records["scale"][keep].astype(np.float64),
                "position": position[keep], "size": records["size"][keep].astype(np.int64)}

    def _starts_jpeg(self, position: np.ndarray, records) -> bool:
        """Whether the first and the last tile `position` names begin with a
        JPEG's start-of-image marker."""
        for index in (0, len(position) - 1):
            at = int(position[index])
            if at < 0 or at + int(records["size"][index]) > self._file_size:
                return False
            if self._bytes(at, 2) != b"\xff\xd8":
                return False
        return True

    def _build_levels(self, records: dict, height: int, width: int) -> list:
        """The levels, finest first, each with its tiles bucketed by the grid
        cells they overlap; `self._tiles[n]` is tile n's (x, y, w, h,
        position, size)."""
        scale_of = records["scale"]
        scales = sorted({float(scale) for scale in np.unique(scale_of) if scale > 0},
                        reverse=True)
        if not scales:
            raise FormatUnreadable(f"{Path(self.path).name} holds no tiles")
        self._tiles = list(zip(records["x"].tolist(), records["y"].tolist(),
                               records["w"].tolist(), records["h"].tolist(),
                               records["position"].tolist(), records["size"].tolist()))
        finest = scales[0]
        side = self._tile_side
        levels: list = []
        for scale in scales:
            members = np.nonzero(scale_of == scale)[0].tolist()
            extent_h = max(self._tiles[n][1] + self._tiles[n][3] for n in members)
            extent_w = max(self._tiles[n][0] + self._tiles[n][2] for n in members)
            ratio = scale / finest
            level_h = max(1, int(height * ratio + 1e-9), extent_h)
            level_w = max(1, int(width * ratio + 1e-9), extent_w)
            if levels and (level_h, level_w) == (levels[-1].height, levels[-1].width):
                continue
            level = _Level(level_h, level_w)
            for number in members:
                x, y, w, h = self._tiles[number][:4]
                for row in range(y // side, (y + h - 1) // side + 1):
                    for column in range(x // side, (x + w - 1) // side + 1):
                        level.cells.setdefault((row, column), []).append(number)
            levels.append(level)
        return levels

    # -- reading -------------------------------------------------------------

    def _tile(self, number: int) -> np.ndarray:
        def make():
            position, size = self._tiles[number][4:]
            return _decode_jpeg(self._bytes(position, size))

        return self._cache.get(number, make)

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        y0, y1, x0, x1 = int(y0), int(y1), int(x0), int(x1)
        out = np.full((max(0, y1 - y0), max(0, x1 - x0), 3), _WHITE, dtype=np.uint8)
        if y1 <= y0 or x1 <= x0:
            return out
        found = self._levels[int(level)]
        side = self._tile_side
        seen = set()
        for row in range(max(0, y0) // side, (y1 - 1) // side + 1):
            for column in range(max(0, x0) // side, (x1 - 1) // side + 1):
                for number in found.cells.get((row, column), ()):
                    if number in seen:
                        continue
                    seen.add(number)
                    tx, ty, tw, th = self._tiles[number][:4]
                    tile = self._tile(number)
                    th, tw = min(th, tile.shape[0]), min(tw, tile.shape[1])
                    sy0, sy1 = max(y0, ty), min(y1, ty + th)
                    sx0, sx1 = max(x0, tx), min(x1, tx + tw)
                    if sy1 <= sy0 or sx1 <= sx0:
                        continue
                    out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = \
                        tile[sy0 - ty:sy1 - ty, sx0 - tx:sx1 - tx]
        return out

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        block = self.read_rgb(level, y0, y1, x0, x1, z, t)
        return np.ascontiguousarray(block[..., min(int(channel), 2)])

    def associated(self, name: str) -> Optional[np.ndarray]:
        """The macro, the label or the thumbnail as RGB uint8, or None when the
        file has none there."""
        offset = self._associated_at.get(name)
        if not offset or offset + 52 > self._file_size:
            return None
        head = self._bytes(offset, 52)
        if head[0:1] != b"\xf1" or head[2:4] != b"\xee\xee":
            return None
        size, header_size = struct.unpack_from("<2I", head, 20)
        if not size or offset + header_size + size > self._file_size:
            return None
        return _decode_jpeg(self._bytes(offset + header_size, size))

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle is not None:
            handle.close()


def reader(path: str, index: int = 0) -> KfbReader:
    if int(index) != 0:
        raise FormatUnreadable(f"{Path(str(path)).name} holds one image; there is no image {index}")
    return KfbReader(path)
