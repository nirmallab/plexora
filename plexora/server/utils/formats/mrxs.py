"""3DHISTECH MIRAX (`.mrxs`): Pannoramic and MIRAX slide scans.

Plexora reads a fluorescence MIRAX slide itself, so its filters open as
separate channels (OpenSlide decodes the tiles as colour JPEGs and flattens the
channels into one false-colour picture). A brightfield slide stays on OpenSlide
(`claims` says no); this reader can still open one, as a colour picture, where
OpenSlide is not there.

A slide is a small `.mrxs` file (unused here) and a folder of the same name
beside it holding:

- `Slidedat.ini`: `[GENERAL]` (`SLIDE_TYPE`, `IMAGENUMBER_X/Y`,
  `CameraImageDivisionsPerSide`, `SLIDE_ID`), `[HIERARCHICAL]` (the layers,
  below), `[DATAFILE]` (`FILE_n` names) and a section per layer value;
- `Index.dat`: where every stored image is;
- `Data####.dat`: the stored images, packed (each file starts with a 5-byte
  version, the slide id, a 3-character file number and padding: 296 bytes).

The layers. `HIER_<i>_NAME` / `HIER_<i>_VAL_<j>` / `..._SECTION` list the
hierarchical layers: "Slide zoom level" (one value per pyramid level, section
`LAYER_0_LEVEL_<k>_SECTION`: `MICROMETER_PER_PIXEL_X/Y`, `DIGITIZER_WIDTH/HEIGHT`
-- the stored image size --, `OVERLAP_X/Y`, `IMAGE_CONCAT_FACTOR`,
`IMAGE_FORMAT`), "Slide filter level" (one value per filter: `FILTER_NAME`,
`STORING_CHANNEL_NUMBER`, `DATA_IN_THIS_FILTER_LEVEL`, display colour, XDIFF /
YDIFF) and "Scan info layer". `NONHIER_<i>_...` list the others: the scan data
layer (thumbnail, label, preview, XML headers), the histogram, the stitching
layer, `VIMSLIDE_POSITION_BUFFER` (and, from version 2.2,
`StitchingIntensityLayer`).

`Index.dat`: a 5-byte version, the slide id, then two int32 (little-endian,
unaligned, like every number in it): the offsets of the hierarchical and the
non-hierarchical root tables. Each table holds one int32 per layer value, in
the order the layers and values are listed in `Slidedat.ini`, pointing to a
chain of pages: int32 item count, int32 offset of the next page (0 ends it),
then the items. The first page has no items. A hierarchical item is four
int32 -- image index (`y * IMAGENUMBER_X + x`), offset, length, data file
number; a non-hierarchical item five -- two zeros, offset, length, file. Only
the zoom-level values hold images; in the samples the filter-level and
scan-info records are empty (0).

The geometry. The camera takes overlapping photos on a grid of
`IMAGENUMBER_X / div` by `IMAGENUMBER_Y / div` positions
(div = `CameraImageDivisionsPerSide`, 4 in the samples) and splits each photo
into div x div non-overlapping images of `DIGITIZER_WIDTH` x `DIGITIZER_HEIGHT`
(347 x 260 of a 1388 x 1040 camera). `IMAGENUMBER_X/Y` count those camera
images. A level-k image is indexed by its top-left camera image, a multiple of
`D_k`, and holds D_k x D_k camera images squeezed into one stored image of the
same pixel size: `D_0 = 2 ** IMAGE_CONCAT_FACTOR` of level 0 (1 for a scan,
16 for a 1/16 "quick save"), and each further level multiplies by
`2 ** IMAGE_CONCAT_FACTOR` (1, so halving). Once D_k exceeds div, a stored
image holds several photos side by side, overlaps and all, each a fractional
number of pixels wide. Absent images (nothing scanned there) are not stored;
coarse levels also store blank filler images.

Where each photo sits: `VIMSLIDE_POSITION_BUFFER.default` holds one 9-byte
entry per camera position, row-major -- a flag byte (1 = scanned, from version
1.9), then x and y as int32, in level-0 pixels, possibly negative. From 2.2
the buffer is DEFLATE-compressed and found under
`StitchingIntensityLayer.StitchingIntensityLevel`. Neighbouring photos overlap
by about `OVERLAP_X/Y` level-0 pixels (96 on the samples' 20x objective) and
the positions are the scanner's own stitching result: on one sample they
register neighbouring photos to 0-2 pixels; on the other they are the nominal
grid and miss by up to ~16 pixels, so its seams show, as they would in the
scanner's software. The `StitchingLayer` record (per-pair offsets with a
match score, layout not fully worked out) is not read. This reader cuts every
stored image into one piece per photo it holds and pastes each at its photo's
position (scaled to the level), later photos (row-major) over earlier ones in
the overlap. A slide without a position buffer is laid on the nominal grid
(photo pitch `div * DIGITIZER_WIDTH / D_0 - OVERLAP_X`). The image is the
bounding box of the scanned photos; elsewhere it reads 0.

Fluorescence channels (found from the two OpenSlide fluorescence samples):
each stored JPEG's three components are three filters' grey images, stored
raw -- the YCbCr-to-RGB conversion a JPEG decoder applies by default must not
be applied (it clips two of them to 0). Component k is the filter whose
`STORING_CHANNEL_NUMBER` is k: the slide's own histogram
(`VIMSLIDE_HISTOGRAM_DATA`, 3 x 256 uint64 over every level-0 pixel) matches
the raw components in that order and does not match the converted ones.
Channel names are the filters' `FILTER_NAME`s, in the filter layer's order;
the pixel size is level 0's `MICROMETER_PER_PIXEL_X/Y`.

This module was written from OpenSlide's published format notes and the
sample files themselves.
"""
from __future__ import annotations

import io
import math
import os
import struct
import zlib
from pathlib import Path
from typing import Optional

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, TileCache,
                                               names_or_numbered)

_SLIDEDAT = "Slidedat.ini"
_ZOOM_LAYER = "Slide zoom level"
_FILTER_LAYER = "Slide filter level"
_FLUORESCENCE = "SLIDE_TYPE_FLUORESCENCE"
_BRIGHTFIELD = "SLIDE_TYPE_BRIGHTFIELD"
#: The position buffer: (layer, value), newest first; the first is compressed.
_POSITIONS = (("StitchingIntensityLayer", "StitchingIntensityLevel"),
              ("VIMSLIDE_POSITION_BUFFER", "default"))
#: Viewer-sized cells the pieces of a level are filed under.
_CELL = 512


def _half_up(value: float) -> int:
    """`value` rounded half up: both edges of a piece move the same way, so a
    piece a whole number of pixels wide keeps its width (`round` rounds halves
    to even and would stretch 1282.5..1629.5 to 348 pixels)."""
    return math.floor(value + 0.5)


# -- Slidedat.ini -------------------------------------------------------------


def _read_ini(path: Path) -> dict:
    """section -> {KEY: value}; the first of a repeated key wins."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    sections: dict = {}
    current = sections.setdefault("", {})
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
        elif "=" in line:
            key, value = line.split("=", 1)
            current.setdefault(key.strip(), value.strip())
    return sections


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _values(ini: dict, prefix: str) -> list:
    """Every value of the HIER or NONHIER layers, in the index's record order:
    `(layer name, value name, its section)`."""
    head = ini.get("HIERARCHICAL", {})
    out = []
    for layer in range(_int(head.get(f"{prefix}_COUNT"))):
        name = head.get(f"{prefix}_{layer}_NAME", "")
        for value in range(_int(head.get(f"{prefix}_{layer}_COUNT"))):
            out.append((name, head.get(f"{prefix}_{layer}_VAL_{value}", ""),
                        ini.get(head.get(f"{prefix}_{layer}_VAL_{value}_SECTION", ""), {})))
    return out


def _filters(ini: dict) -> list:
    """`(name, component)` of each filter whose images are the zoom levels'
    (`DATA_IN_THIS_FILTER_LEVEL` naming the first filter level, or none)."""
    values = [(value, section) for layer, value, section in _values(ini, "HIER")
              if layer == _FILTER_LAYER]
    if not values:
        return []
    first = values[0][0]
    out = []
    for index, (_value, section) in enumerate(values):
        home = section.get("DATA_IN_THIS_FILTER_LEVEL", first) or first
        if home != first:
            continue
        out.append((section.get("FILTER_NAME", ""),
                    _int(section.get("STORING_CHANNEL_NUMBER"), index)))
    return out


def _all_filters_readable(ini: dict) -> bool:
    values = [section for layer, _value, section in _values(ini, "HIER")
              if layer == _FILTER_LAYER]
    return len(_filters(ini)) == len(values)


def _is_fluorescence(ini: dict) -> bool:
    kind = ini.get("GENERAL", {}).get("SLIDE_TYPE", "").strip().upper()
    if kind:
        return kind == _FLUORESCENCE
    # No slide type (older slides): named filters with a display colour. A
    # brightfield slide's filters are "Default" and black.
    for layer, _value, section in _values(ini, "HIER"):
        if layer != _FILTER_LAYER:
            continue
        name = section.get("FILTER_NAME", "").strip()
        colour = sum(_int(section.get(key)) for key in ("COLOR_R", "COLOR_G", "COLOR_B"))
        if name and name.lower() != "default" and colour:
            return True
    return False


def _folder(path) -> Path:
    path = Path(str(path))
    return path.with_suffix("") if path.suffix.lower() == ".mrxs" else path


def _find(folder: Path, name: str) -> Optional[Path]:
    """`folder/name`, matching the name's case loosely (slides written on
    Windows are read on case-sensitive file systems)."""
    exact = folder / name
    if exact.is_file():
        return exact
    try:
        for entry in os.listdir(folder):
            if entry.lower() == name.lower():
                return folder / entry
    except OSError:
        pass
    return None


def claims(path) -> bool:
    """Whether this reader takes `path`: a fluorescence MIRAX slide laid out as
    the module docstring describes (JPEG levels, every filter in the zoom
    levels' images, halving levels). Decided from `Slidedat.ini` alone."""
    try:
        folder = _folder(path)
        slidedat = _find(folder, _SLIDEDAT)
        if slidedat is None:
            return False
        ini = _read_ini(slidedat)
        if not _is_fluorescence(ini):
            return False
        if ini.get("LAYER_0_SECTION", {}).get("SQRT2_RESIZED", "False").lower() == "true":
            return False
        zoom = [section for layer, _value, section in _values(ini, "HIER")
                if layer == _ZOOM_LAYER]
        if not zoom or any(section.get("IMAGE_FORMAT", "JPEG").upper() != "JPEG"
                           for section in zoom):
            return False
        index_name = ini.get("HIERARCHICAL", {}).get("INDEXFILE", "Index.dat")
        return _all_filters_readable(ini) and _find(folder, index_name) is not None
    except Exception:       # anything odd: leave the file to OpenSlide
        return False


# -- Index.dat ----------------------------------------------------------------


def _chain(data: bytes, pointer: int, width: int) -> np.ndarray:
    """The items of a page chain, `(n, width)` int32."""
    parts = []
    seen = set()
    while pointer and pointer not in seen and 0 < pointer <= len(data) - 8:
        seen.add(pointer)
        count, following = struct.unpack_from("<ii", data, pointer)
        if count > 0:
            end = pointer + 8 + 4 * width * count
            if end > len(data):
                raise FormatUnreadable("Index.dat is cut short")
            parts.append(np.frombuffer(data, dtype="<i4", count=width * count,
                                       offset=pointer + 8).reshape(count, width))
        pointer = following
    if not parts:
        return np.zeros((0, width), dtype=np.int32)
    return np.concatenate(parts).astype(np.int64)


class _Index:
    def __init__(self, path: Path, slide_id: str, hier_count: int, nonhier_count: int):
        self.data = data = path.read_bytes()
        if len(data) < 13:
            raise FormatUnreadable(f"{path.name} is too short to be a MIRAX index")
        start = 5 + len(slide_id)
        if slide_id and data[5:start] != slide_id.encode("latin-1", "replace"):
            found = data.find(slide_id.encode("latin-1", "replace"), 0, 256)
            start = found + len(slide_id) if found >= 0 else start
        if start + 8 > len(data):
            raise FormatUnreadable(f"{path.name} is cut short: it ends before its tables")
        hier, nonhier = struct.unpack_from("<ii", data, start)
        self.hier = self._table(hier, hier_count)
        self.nonhier = self._table(nonhier, nonhier_count)

    def _table(self, at: int, count: int) -> list:
        if count <= 0 or at <= 0 or at + 4 * count > len(self.data):
            return [0] * max(count, 0)
        return list(struct.unpack_from(f"<{count}i", self.data, at))

    def items(self, pointer: int, width: int) -> np.ndarray:
        return _chain(self.data, pointer, width) if pointer else np.zeros((0, width), np.int64)


# -- decoding -----------------------------------------------------------------


def _jpeg_components(data: bytes) -> int:
    at = 2
    while at + 4 <= len(data) and data[at] == 0xFF:
        marker = data[at + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                      0xCD, 0xCE, 0xCF):
            return data[at + 9] if at + 9 < len(data) else 0
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        at += 2 + struct.unpack_from(">H", data, at + 2)[0]
    return 0


def _decode(data: bytes, raw: bool) -> np.ndarray:
    """A stored image as `(h, w, components)` uint8. `raw`: a three-component
    JPEG's components as stored (a fluorescence slide's three filters), not
    converted to RGB; a BMP's samples in file (B, G, R) order. A damaged
    one (a data file cut short, bytes overwritten) is `FormatUnreadable`."""
    try:
        return _decoded(data, raw)
    except FormatUnreadable:
        raise
    except Exception as error:  # noqa: BLE001 -- whichever decoder said it
        raise FormatUnreadable(f"a stored image of this slide is damaged ({error})") from error


def _decoded(data: bytes, raw: bool) -> np.ndarray:
    try:
        import imagecodecs
    except ImportError:         # Pillow does it all, a little slower
        imagecodecs = None
    tile = None
    if data[:2] == b"\xff\xd8":
        three = _jpeg_components(data) == 3
        if imagecodecs is not None and hasattr(imagecodecs, "jpeg8_decode"):
            if raw and three:
                tile = imagecodecs.jpeg8_decode(data, colorspace="YCBCR",
                                                outcolorspace="YCBCR")
            else:
                tile = imagecodecs.jpeg8_decode(data)
    elif data[:4] == b"\x89PNG" and imagecodecs is not None:
        tile = imagecodecs.png_decode(data)
    if tile is None:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as picture:
            if (raw and picture.format == "JPEG" and picture.mode == "RGB"
                    and picture.info.get("adobe_transform", 1) != 0):
                picture.draft("YCbCr", None)        # the components as stored
            elif picture.mode not in ("L", "RGB", "YCbCr"):
                picture = picture.convert("RGB")
            tile = np.asarray(picture)
        if raw and data[:2] == b"BM" and tile.ndim == 3:
            tile = tile[..., ::-1]
    tile = np.asarray(tile)
    if tile.dtype != np.uint8:
        tile = (tile >> 8).astype(np.uint8) if tile.dtype == np.uint16 else tile.astype(np.uint8)
    if tile.ndim == 2:
        tile = tile[..., None]
    return tile


# -- the reader ---------------------------------------------------------------


class _Level:
    __slots__ = ("down", "width", "height", "images", "pieces", "cells")

    def __init__(self, down: int, width: int, height: int, images: dict):
        self.down = down                # camera images per stored image side (D_k)
        self.width, self.height = width, height     # stored image size
        self.images = images            # image index -> (file, offset, length)
        self.pieces = None              # built on first read
        self.cells = None


class MrxsReader(Reader):
    format_name = "3DHISTECH MRXS"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        if int(index) != 0:
            raise FormatUnreadable(f"A MIRAX slide holds one image; there is no image {index}.")
        self._files: dict = {}
        try:
            self._open(path)
        except BaseException:
            self.close()            # the data files opened on the way
            raise

    def _open(self, path: str):
        folder = _folder(path)
        slidedat = _find(folder, _SLIDEDAT)
        if slidedat is None:
            raise FormatUnreadable(
                f"The pixels of {Path(str(path)).name} are in the folder {folder.name} beside "
                "it, which is not there or has no Slidedat.ini. Keep the .mrxs file and its "
                "folder together.")
        self._folder = folder
        ini = self._ini = _read_ini(slidedat)
        general = ini.get("GENERAL", {})
        head = ini.get("HIERARCHICAL", {})
        if ini.get("LAYER_0_SECTION", {}).get("SQRT2_RESIZED", "False").lower() == "true":
            raise FormatUnreadable("This MIRAX slide's levels step by the square root of 2, "
                                   "which Plexora does not read.")
        self._grid_w = _int(general.get("IMAGENUMBER_X"))
        self._grid_h = _int(general.get("IMAGENUMBER_Y"))
        self._div = max(1, _int(general.get("CameraImageDivisionsPerSide"), 1))
        if self._grid_w <= 0 or self._grid_h <= 0:
            raise FormatUnreadable("Slidedat.ini does not give the slide's image grid")
        hier = _values(ini, "HIER")
        nonhier = _values(ini, "NONHIER")
        index_path = _find(folder, head.get("INDEXFILE", "Index.dat"))
        if index_path is None:
            raise FormatUnreadable(f"{folder.name} has no {head.get('INDEXFILE', 'Index.dat')}")
        self._index = _Index(index_path, general.get("SLIDE_ID", ""), len(hier), len(nonhier))
        datafile = ini.get("DATAFILE", {})
        self._names = {number: datafile.get(f"FILE_{number}", f"Data{number:04d}.dat")
                       for number in range(_int(datafile.get("FILE_COUNT")))}

        fluorescence = _is_fluorescence(ini)
        self.rgb = not fluorescence
        self._raw = fluorescence
        self._open_levels(hier)
        self._open_positions(nonhier)
        if fluorescence:
            filters = _filters(ini) or [("", 0), ("", 1), ("", 2)]
            self._components = [component for _name, component in filters]
            self.channels = len(filters)
            self.channel_names = names_or_numbered([name for name, _c in filters], self.channels)
            self._fill = 0
        else:
            self._components = [0, 1, 2]
            self.channels = 3
            self.channel_names = ["Red", "Green", "Blue"]
            colour = _int(self._zoom_sections[0].get("IMAGE_FILL_COLOR_BGR"), 0xFFFFFF)
            self._fill = np.array([colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF],
                                  dtype=np.uint8)
        self.dtype = np.dtype(np.uint8)
        level0 = self._zoom_sections[0]
        mpp = (_float(level0.get("MICROMETER_PER_PIXEL_X")),
               _float(level0.get("MICROMETER_PER_PIXEL_Y")))
        self.pixel_size = (mpp[0], mpp[1] or mpp[0]) if mpp[0] > 0 else None
        self.images = [general.get("SLIDE_NAME") or Path(str(path)).stem]
        self.image_index = 0
        self._cache = TileCache(192)

    # -- opening ------------------------------------------------------------

    def _open_levels(self, hier: list):
        zoom = [(record, section) for record, (layer, _value, section) in enumerate(hier)
                if layer == _ZOOM_LAYER]
        if not zoom:
            raise FormatUnreadable("Slidedat.ini lists no zoom levels")
        self._levels_found: list = []
        self._zoom_sections: list = []
        down = 0
        for number, (record, section) in enumerate(zoom):
            concat = _int(section.get("IMAGE_CONCAT_FACTOR"), 0 if number == 0 else 1)
            down = (1 << max(concat, 0)) if number == 0 else down << max(concat, 0)
            pointer = self._index.hier[record] if record < len(self._index.hier) else 0
            images = {}
            for image, offset, length, file in self._index.items(pointer, 4).tolist():
                if length > 0 and 0 <= image < self._grid_w * self._grid_h:
                    images.setdefault(int(image), (int(file), int(offset), int(length)))
            if not images:
                if not self._levels_found:
                    continue        # nothing at the finest: start from the first stored
                break
            width = _int(section.get("DIGITIZER_WIDTH"))
            height = _int(section.get("DIGITIZER_HEIGHT"))
            if width <= 0 or height <= 0:
                width, height = self._measure(images)
            self._levels_found.append(_Level(down, width, height, images))
            self._zoom_sections.append(section)
        if not self._levels_found:
            raise FormatUnreadable("This MIRAX slide's index lists no images")

    def _measure(self, images: dict) -> tuple:
        """A stored image's (width, height), decoded from the first one."""
        number, offset, length = next(iter(images.values()))
        tile = _decode(self._blob(number, offset, length), self._raw)
        return int(tile.shape[1]), int(tile.shape[0])

    def _open_positions(self, nonhier: list):
        """Each scanned photo's level-0 (x, y), and the image's extent."""
        div = self._div
        across, down = -(-self._grid_w // div), -(-self._grid_h // div)
        level0 = self._levels_found[0]
        d0 = level0.down
        unit_w, unit_h = level0.width / d0, level0.height / d0  # a camera image, level-0 px
        # The photos the finest level's images hold.
        covered = set()
        for image in level0.images:
            ix, iy = image % self._grid_w, image // self._grid_w
            for py in range(iy // div, (iy + d0 - 1) // div + 1):
                for px in range(ix // div, (ix + d0 - 1) // div + 1):
                    covered.add((px, py))
        stored = self._position_buffer(nonhier, across * down)
        photos = {}
        if stored is not None:
            flagged = bool(np.any(stored[:, 0] & 1))
            for px, py in covered:
                if px >= across or py >= down:
                    continue
                flag, x, y = stored[py * across + px]
                if (flag & 1) if flagged else (x or y):
                    photos[(px, py)] = (int(x), int(y))
        else:
            section = self._zoom_sections[0]
            pitch_x = div * unit_w - _float(section.get("OVERLAP_X"))
            pitch_y = div * unit_h - _float(section.get("OVERLAP_Y"))
            photos = {(px, py): (px * pitch_x, py * pitch_y) for px, py in covered}
        if not photos:
            raise FormatUnreadable("This MIRAX slide's index lists no scanned field")
        self._photos = photos
        self._unit = (unit_w, unit_h)
        self._origin = (min(x for x, _y in photos.values()), min(y for _x, y in photos.values()))
        span_x = max(x for x, _y in photos.values()) + div * unit_w - self._origin[0]
        span_y = max(y for _x, y in photos.values()) + div * unit_h - self._origin[1]
        self.levels = []
        for level in self._levels_found:
            scale = level.down / d0
            self.levels.append((max(1, math.ceil(span_y / scale - 1e-6)),
                                max(1, math.ceil(span_x / scale - 1e-6))))

    def _position_buffer(self, nonhier: list, count: int) -> Optional[np.ndarray]:
        """`(count, 3)` int64 of (flag, x, y), or None when the slide has none
        that fits its grid."""
        for layer_name, value_name in _POSITIONS:
            for record, (layer, value, _section) in enumerate(nonhier):
                if layer != layer_name or value != value_name:
                    continue
                pointer = self._index.nonhier[record] if record < len(self._index.nonhier) else 0
                items = self._index.items(pointer, 5)
                if not len(items):
                    continue
                _a, _b, offset, length, number = (int(v) for v in items[0])
                blob = self._blob(number, offset, length)
                if len(blob) != 9 * count:
                    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                        try:
                            blob = zlib.decompress(blob, wbits)
                            break
                        except zlib.error:
                            continue
                if len(blob) < 9 * count:
                    continue
                table = np.frombuffer(blob, dtype=np.dtype([("flag", "u1"), ("x", "<i4"),
                                                            ("y", "<i4")]), count=count)
                return np.stack([table["flag"].astype(np.int64), table["x"].astype(np.int64),
                                 table["y"].astype(np.int64)], axis=1)
        return None

    # -- the pieces of a level --------------------------------------------

    def _pieces(self, level: int):
        """The level's pieces -- (y0, x0, y1, x1 in the level, image, sy0, sx0
        in the stored image), in paint order -- and the cells they cross."""
        found = self._levels_found[level]
        if found.pieces is not None:
            return found.pieces, found.cells
        with self._lock:
            if found.pieces is not None:
                return found.pieces, found.cells
            div, d0, d = self._div, self._levels_found[0].down, found.down
            scale = d / d0
            unit_w, unit_h = found.width / d, found.height / d   # a camera image here
            ox, oy = self._origin
            pieces = []
            for image in found.images:
                ix, iy = image % self._grid_w, image // self._grid_w
                for py in range(iy // div, (iy + d - 1) // div + 1):
                    r0, r1 = max(iy, py * div), min(iy + d, (py + 1) * div)
                    for px in range(ix // div, (ix + d - 1) // div + 1):
                        at = self._photos.get((px, py))
                        if at is None:
                            continue
                        c0, c1 = max(ix, px * div), min(ix + d, (px + 1) * div)
                        # Where the part of photo (px, py) inside this image
                        # sits, in the image and in the level.
                        left = (at[0] - ox) / scale + (c0 - px * div) * unit_w
                        top = (at[1] - oy) / scale + (r0 - py * div) * unit_h
                        x0, x1 = _half_up(left), _half_up(left + (c1 - c0) * unit_w)
                        y0, y1 = _half_up(top), _half_up(top + (r1 - r0) * unit_h)
                        if x1 <= x0 or y1 <= y0:
                            continue
                        sx0 = _half_up((c0 - ix) * unit_w)
                        sy0 = _half_up((r0 - iy) * unit_h)
                        pieces.append(((py, px), (y0, x0, y1, x1, image, sy0, sx0)))
            pieces.sort(key=lambda entry: entry[0])
            pieces = [piece for _order, piece in pieces]
            cells: dict = {}
            for number, (y0, x0, y1, x1, _image, _sy, _sx) in enumerate(pieces):
                for cy in range(y0 // _CELL, (y1 - 1) // _CELL + 1):
                    for cx in range(x0 // _CELL, (x1 - 1) // _CELL + 1):
                        cells.setdefault((cy, cx), []).append(number)
            found.pieces, found.cells = pieces, cells
            return pieces, cells

    def _covering(self, level, y0, y1, x0, x1) -> list:
        pieces, cells = self._pieces(level)
        numbers = set()
        for cy in range(y0 // _CELL, (y1 - 1) // _CELL + 1):
            for cx in range(x0 // _CELL, (x1 - 1) // _CELL + 1):
                numbers.update(cells.get((cy, cx), ()))
        return [pieces[number] for number in sorted(numbers)]

    # -- pixels ---------------------------------------------------------------

    def _blob(self, number: int, offset: int, length: int) -> bytes:
        with self._lock:
            handle = self._files.get(number)
            if handle is None:
                name = self._names.get(number, f"Data{number:04d}.dat")
                found = _find(self._folder, name)
                if found is None:
                    raise FormatUnreadable(f"{self._folder.name} has no {name}")
                handle = self._files[number] = open(found, "rb")
            handle.seek(offset)
            return handle.read(length)

    def _image(self, level: int, image: int) -> np.ndarray:
        def make():
            number, offset, length = self._levels_found[level].images[image]
            return _decode(self._blob(number, offset, length), self._raw)

        return self._cache.get((level, image), make)

    def _paint(self, out, level, y0, y1, x0, x1, planes):
        for py0, px0, py1, px1, image, sy0, sx0 in self._covering(level, y0, y1, x0, x1):
            ty0, ty1 = max(y0, py0), min(y1, py1)
            tx0, tx1 = max(x0, px0), min(x1, px1)
            if ty1 <= ty0 or tx1 <= tx0:
                continue
            tile = self._image(level, image)
            ay, ax = sy0 + ty0 - py0, sx0 + tx0 - px0
            piece = tile[ay:ay + ty1 - ty0, ax:ax + tx1 - tx0]
            if not piece.size:
                continue
            piece = piece[..., [min(plane, tile.shape[-1] - 1) for plane in planes]]
            if out.ndim == 2:
                piece = piece[..., 0]
            out[ty0 - y0:ty0 - y0 + piece.shape[0], tx0 - x0:tx0 - x0 + piece.shape[1]] = piece
        return out

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        out = np.empty((y1 - y0, x1 - x0), dtype=np.uint8)
        out[...] = self._fill if not self.rgb else self._fill[int(channel)]
        return self._paint(out, int(level), y0, y1, x0, x1, [self._components[int(channel)]])

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        out = np.empty((y1 - y0, x1 - x0, 3), dtype=np.uint8)
        out[...] = self._fill
        return self._paint(out, int(level), y0, y1, x0, x1, [0, 1, 2])

    def close(self):
        files, self._files = getattr(self, "_files", {}), {}
        for handle in files.values():
            try:
                handle.close()
            except OSError:
                pass


def reader(path: str, index: int = 0) -> MrxsReader:
    return MrxsReader(path, index)
