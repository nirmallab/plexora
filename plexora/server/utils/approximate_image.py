"""Images made smaller for a `.plexora` file, within their own noise.

With "Smaller images" on, a bundle carries each 16-bit fluorescence image it
includes approximately rather than byte for byte: every pixel v is kept as
q = round(sqrt(v)), and comes back as q*q -- within sqrt(v) + 0.25 counts of
v, by construction (|sqrt(v) - q| <= 0.5). Photon noise grows as sqrt(v) too,
so the error is the same share of the noise at every brightness, and there is
no background to estimate (a wrong one would lose dim staining). Measured on
two 19-channel CyCIF slides (2026-11-12): bright pixels within about 2%, dim
staining within 7.4%, the error under the pixel noise everywhere; about 85%
smaller than the uncompressed image with its pyramid. Masks, tables, colour
images and anything not a 16-bit grey TIFF go as they are (`reason`). The
pyramid travels with the image: rebuilding one on the other computer is slow.

q is then coded without loss, with nothing but numpy and zstd -- what the
website's Python has too, so it reads these images as the desktop does
(server/utils/formats/approx.py; Plexora opens the container itself, nothing
is written back). Each 1024 x 1024 tile is predicted coarse to fine, as SZ3
interpolates: every 64th pixel first, then at each stride every pixel between
two decoded ones from their mean, rows then columns. The residuals are
zigzagged, split into bit planes (bitshuffle) and zstd'd. That is about 10%
larger than SZ3 at the same bound (which the website has no build of), and
decodes in a few slices per stride.

The container, `<name>.approx`:

    MAGIC
    each tile's bytes: level by level, channel by channel, row by row
    the index, JSON: {format, method, tile, axes, dtype,
        levels: [{shape, tiles: [[offset, length], ...]}],
        description, resolution, resolutionunit, source: {name, size}}
    the index's length (8 bytes, little-endian), END

The index keeps the image's first description (the OME-XML, so channel names
and the pixel size survive). `decode` writes a container out as a tiled TIFF,
for whoever wants one; its Software tag (MARK) says what it is, so exporting
it again copies it rather than making it smaller twice.

The same container is a large flat image's compressed pyramid copy
(server/utils/image_pyramid.py, `<name>.pyramid.approx`): `write_tiles` takes
the tiles as the pyramid builder makes them, each level from the exact level
above, so the error never compounds down the pyramid.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import numpy as np

MAGIC = b"PXAPPROX1\n"
END = b"PXAPPROX"
#: The container's name: the image's own name, and this.
SUFFIX = ".approx"
FORMAT = 1
METHOD = "sqrt-grid"
TILE = 1024
#: The coarsest pixels a tile keeps as they are, every ANCHOR-th.
ANCHOR = 64
#: Bit planes of a zigzagged residual: q is 0..256, so a residual is within
#: +-256 and zigzags to at most 512.
PLANES = 10
#: zstd's level. Over bit planes a middling level does what level 19 does
#: over bytes (0.286 against 0.289 bytes a pixel at level 3, measured), many
#: times faster; encoding is mostly numpy's time.
LEVEL = 9
#: The Software tag of a TIFF `decode` wrote.
MARK = "Plexora approximate image (sqrt-grid)"
#: Smaller than this, an image goes as it is: it saves little.
MIN_BYTES = 1 << 20
#: About what an image comes to, as a share of its file: the export
#: dialog's estimate. About 0.16 of an uncompressed slide with its pyramid; a
#: file compressed already (LZW, zstd) is smaller to begin with, so its share
#: is larger (about 0.22 and 0.25).
SHARE = 0.16
SHARE_COMPRESSED = 0.25
_SUFFIXES = (".tif", ".tiff", ".btf", ".tf8", ".tf2")
_MARK_PREFIX = "Plexora approximate image"


def _website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def can_read() -> bool:
    """Whether this Python reads approximate images: it needs zstd, which the
    desktop's imagecodecs and the website's shim of it both have."""
    try:
        import imagecodecs

        return callable(getattr(imagecodecs, "zstd_decode", None))
    except ImportError:
        return False


def can_make() -> bool:
    """Whether this Python makes them: not the website, whose Python cannot
    write a file of that size (its files live in the tab's memory)."""
    return not _website() and can_read()


def likely(path) -> bool:
    """`reason` from the name alone: a TIFF of some size. The export itself
    asks `reason`."""
    try:
        target = Path(path)
        return target.name.lower().endswith(_SUFFIXES) and target.stat().st_size >= MIN_BYTES
    except (OSError, TypeError, ValueError):
        return False


def estimate(path) -> Optional[int]:
    """About what the image at `path` comes to as an approximate image, for
    the export dialog -- or None when it would go as it is. Reads the file's
    first page only (no pixels, none of its other pages): whether it is a
    16-bit grey plane, compressed already, or written by `decode`."""
    if not likely(path):
        return None
    try:
        import tifffile

        with tifffile.TiffFile(path) as tif:
            first = tif.pages.first
            if str(first.software or "").startswith(_MARK_PREFIX):
                return None
            if first.dtype != np.uint16 or first.samplesperpixel != 1:
                return None
            compressed = int(first.compression) != 1
        size = Path(path).stat().st_size
    except Exception:  # noqa: BLE001 -- an estimate that cannot read the file offers nothing
        return None
    return int(size * (SHARE_COMPRESSED if compressed else SHARE))


def reason(path) -> Optional[str]:
    """None when `path` can go as an approximate image, else why not: it
    must be a TIFF holding one 16-bit grey image (YX, or CYX with one page
    per channel), its pyramid if any as SubIFDs, not written by `decode`."""
    target = Path(path)
    if not target.name.lower().endswith(_SUFFIXES):
        return "not a TIFF"
    try:
        if target.stat().st_size < MIN_BYTES:
            return "small"
        import tifffile

        with tifffile.TiffFile(target) as tif:
            if len(tif.series) != 1:
                return "more than one image in the file"
            series = tif.series[0]
            first = tif.pages[0]
            if str(first.software or "").startswith(_MARK_PREFIX):
                return "approximate already"
            if series.dtype != np.uint16:
                return "not 16-bit"
            if series.axes not in ("YX", "CYX") or first.samplesperpixel != 1:
                return "not one grey plane per channel"
            levels = series.levels
            if len(levels) > 1:
                if not first.subifds:
                    return "a pyramid without SubIFDs"
                if any(len(level.shape) != len(series.shape) or level.shape[:-2] != series.shape[:-2]
                       for level in levels):
                    return "an uneven pyramid"
    except Exception:  # noqa: BLE001 -- a file that does not read goes as it is
        return "not readable"
    return None


# -- one tile -------------------------------------------------------------------


def _rows(q, s):
    """(the pixels between two decoded rows at stride `s`, their prediction):
    rows s, 3s, ... at columns 0, 2s, ..., from the mean of the rows either
    side -- the one above alone at the bottom edge -- or None."""
    target = q[s::2 * s, ::2 * s]
    count = target.shape[0]
    if not target.size:
        return None
    above = q[::2 * s, ::2 * s][:count]
    below = q[2 * s::2 * s, ::2 * s][:count]
    if below.shape[0] < count:
        below = np.concatenate([below, above[below.shape[0]:]], axis=0)
    return target, (above + below) >> 1


def _columns(q, s):
    """`_rows` across: every row at stride `s`, columns s, 3s, ..."""
    target = q[::s, s::2 * s]
    count = target.shape[1]
    if not target.size:
        return None
    left = q[::s, ::2 * s][:, :count]
    right = q[::s, 2 * s::2 * s][:, :count]
    if right.shape[1] < count:
        right = np.concatenate([right, left[:, right.shape[1]:]], axis=1)
    return target, (left + right) >> 1


def _steps():
    stride = ANCHOR // 2
    while stride:
        yield _rows, stride
        yield _columns, stride
        stride //= 2


_M1, _M2, _M3 = np.uint64(0x00AA00AA00AA00AA), np.uint64(0x0000CCCC0000CCCC), np.uint64(0x00000000F0F0F0F0)
_S7, _S14, _S28 = np.uint64(7), np.uint64(14), np.uint64(28)
#: q*q for every q, clipped to 16 bits (256*256 is one past).
_SQUARES = np.minimum(np.arange(257, dtype=np.uint32) ** 2, 65535).astype(np.uint16)


def _transpose8(words):
    """Each word's bits as an 8 x 8 matrix, transposed: bit 8r + c <-> bit
    8c + r (Hacker's Delight 7-3). Its own inverse. Eight pixels' bytes in, the
    same 8 bits of each pixel's value out -- one byte a bit plane -- and back."""
    t = (words ^ (words >> _S7)) & _M1
    words = words ^ t ^ (t << _S7)
    t = (words ^ (words >> _S14)) & _M2
    words = words ^ t ^ (t << _S14)
    t = (words ^ (words >> _S28)) & _M3
    return words ^ t ^ (t << _S28)


def encode_tile(tile) -> bytes:
    """One tile of uint16 as the container keeps it."""
    import imagecodecs

    q = np.rint(np.sqrt(np.asarray(tile), dtype=np.float32)).astype(np.int16)
    parts = [q[::ANCHOR, ::ANCHOR]]
    for step, stride in _steps():
        found = step(q, stride)
        if found is not None:
            parts.append(found[0] - found[1])
    flat = np.concatenate([part.ravel() for part in parts])
    zigzag = (flat << 1) ^ (flat >> 15)              # 0..512
    # Bit planes (bitshuffle; plane b holds bit b of each value, eight values
    # a byte, the first in the lowest bit): zstd packs the busy low ones close
    # to their entropy. Only as many as the tile's largest residual needs (its
    # first byte says how many): a noisy tile rarely needs more than 8 of 10.
    used = max(1, int(zigzag.max()).bit_length())
    count = zigzag.size
    per_plane = -(-count // 8)
    low = np.zeros(per_plane * 8, dtype=np.uint8)
    low[:count] = zigzag & 0xFF
    planes = np.ascontiguousarray(_transpose8(low.view(np.uint64)).view(np.uint8).reshape(-1, 8).T)
    planes = [planes[:min(used, 8)]]
    for bit in range(8, used):
        planes.append(np.packbits(((zigzag >> bit) & 1).astype(np.uint8), bitorder="little")[None, :])
    return bytes([used]) + imagecodecs.zstd_encode(np.concatenate(planes).tobytes(), level=LEVEL)


def decode_tile(data, shape) -> np.ndarray:
    """`encode_tile`'s bytes back to uint16 of `shape`: q*q."""
    import imagecodecs

    height, width = int(shape[0]), int(shape[1])
    count = height * width
    per_plane = -(-count // 8)
    data = bytes(data)
    used = data[0] if data else 0
    raw = np.frombuffer(imagecodecs.zstd_decode(data[1:]), dtype=np.uint8) if used else None
    if not 1 <= used <= PLANES or raw.size != used * per_plane:
        raise ValueError("This approximate image is damaged.")
    raw = raw.reshape(used, per_plane)
    eight = np.zeros((8, per_plane), dtype=np.uint8)
    eight[:min(used, 8)] = raw[:8]
    words = np.ascontiguousarray(eight.T).view(np.uint64).ravel()
    flat = _transpose8(words).view(np.uint8)[:count].astype(np.int16)
    for bit in range(8, used):
        flat |= np.unpackbits(raw[bit], count=count, bitorder="little").astype(np.int16) << bit
    flat = (flat >> 1) ^ -(flat & 1)
    q = np.empty((height, width), dtype=np.int16)
    at = 0

    def take(block_shape):
        nonlocal at
        size = block_shape[0] * block_shape[1]
        block = flat[at:at + size].reshape(block_shape)
        at += size
        return block

    anchors = q[::ANCHOR, ::ANCHOR]
    q[::ANCHOR, ::ANCHOR] = take(anchors.shape)
    for step, stride in _steps():
        found = step(q, stride)
        if found is not None:
            target, predicted = found
            target[...] = take(target.shape) + predicted
    return _SQUARES[q]


# -- the container ----------------------------------------------------------------


def _workers(workers: Optional[int]) -> int:
    return max(1, int(workers or min(16, os.cpu_count() or 4)))


def _raw_bytes(shapes) -> int:
    return sum(int(np.prod(shape)) * 2 for shape in shapes)


def encode(path, out, *, advance: Optional[Callable[[int], None]] = None,
           check: Optional[Callable[[], None]] = None, workers: Optional[int] = None) -> dict:
    """Write the image at `path` approximately to the binary stream `out`
    (only its `write` is used). `advance(n)` is told the image's bytes done,
    in the file's own bytes; `check()` is called between rows of tiles and
    may raise to stop. Returns {size, tiles}."""
    import tifffile
    import zarr

    why = reason(path)
    if why is not None:
        raise ValueError(f"{Path(path).name} cannot go as an approximate image ({why}).")
    advance = advance or (lambda count: None)
    check = check or (lambda: None)
    written = 0

    def put(data) -> None:
        nonlocal written
        out.write(data)
        written += len(data)

    put(MAGIC)
    file_size = Path(path).stat().st_size
    levels_out, tiles_done = [], 0
    with tifffile.TiffFile(path) as tif, ThreadPoolExecutor(_workers(workers)) as pool:
        series = tif.series[0]
        first = tif.pages[0]
        levels = series.levels
        # Progress in the file's bytes, whatever its own compression.
        scale = file_size / max(_raw_bytes(level.shape for level in levels), 1)
        for number, level in enumerate(levels):
            shape = tuple(int(size) for size in level.shape)
            planes = shape[0] if len(shape) == 3 else 1
            height, width = shape[-2:]
            store = series.aszarr(level=number) if len(levels) > 1 else series.aszarr()
            tiles = []
            try:
                array = zarr.open(store, mode="r")
                for plane in range(planes):
                    for y in range(0, height, TILE):
                        check()
                        # A row of tiles at a time: a stripped file is read once.
                        band = np.asarray(array[plane, y:y + TILE, :] if len(shape) == 3
                                          else array[y:y + TILE, :])
                        pieces = [band[:, x:x + TILE] for x in range(0, width, TILE)]
                        for data in pool.map(encode_tile, pieces):
                            tiles.append([written, len(data)])
                            put(data)
                        tiles_done += len(pieces)
                        advance(int(band.nbytes * scale))
            finally:
                store.close()
            levels_out.append({"shape": list(shape), "tiles": tiles})
        resolution = unit = None
        if "XResolution" in first.tags and "YResolution" in first.tags:
            resolution = [float(value) for value in first.resolution]
            unit = int(first.resolutionunit)
        index = {"format": FORMAT, "method": METHOD, "tile": TILE, "axes": series.axes, "dtype": "uint16",
                 "levels": levels_out, "description": first.description or None,
                 "resolution": resolution, "resolutionunit": unit,
                 "source": {"name": Path(path).name, "size": file_size}}
    text = json.dumps(index, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    put(text)
    put(len(text).to_bytes(8, "little"))
    put(END)
    return {"size": written, "tiles": tiles_done}


def write_tiles(path, shapes, tiles, *, description: Optional[str] = None, source: Optional[dict] = None,
                workers: Optional[int] = None) -> dict:
    """Write an approximate image to `path` from `tiles`: every tile of every
    level of `shapes` ((C, Y, X) each), uint16, unpadded at the edges, in the
    container's order -- level, plane, tile row, tile column. Tiles are
    encoded on `workers` threads while the next are made; the file appears
    under `path` only once complete (written through a dot-prefixed `.tmp`
    beside it, removed on any failure). Returns {size, tiles}."""
    from collections import deque

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{os.getpid()}-{threading.get_ident()}.tmp")
    count = _workers(workers)
    levels_out = [{"shape": [int(side) for side in shape], "tiles": []} for shape in shapes]
    grids = [_tile_grid(tuple(shape)) for shape in shapes]
    wanted = [planes * rows * columns for planes, rows, columns in grids]
    written = done = 0
    try:
        with open(temporary, "wb") as out, ThreadPoolExecutor(count) as pool:
            def put(data) -> None:
                nonlocal written
                out.write(data)
                written += len(data)

            put(MAGIC)
            level = 0
            pending = deque()

            def land(future) -> None:
                nonlocal level, done
                data = future.result()
                while level < len(wanted) and len(levels_out[level]["tiles"]) >= wanted[level]:
                    level += 1
                if level == len(wanted):
                    raise ValueError(f"{path.name}: more tiles were made than its levels hold.")
                levels_out[level]["tiles"].append([written, len(data)])
                put(data)
                done += 1

            for tile in tiles:
                pending.append(pool.submit(encode_tile, tile))
                if len(pending) >= 4 * count:
                    land(pending.popleft())
            while pending:
                land(pending.popleft())
            if done != sum(wanted):
                raise ValueError(f"{path.name}: {done} tiles were made of the {sum(wanted)} its levels hold.")
            index = {"format": FORMAT, "method": METHOD, "tile": TILE, "axes": "CYX", "dtype": "uint16",
                     "levels": levels_out, "description": description or None,
                     "resolution": None, "resolutionunit": None, "source": source}
            text = json.dumps(index, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            put(text)
            put(len(text).to_bytes(8, "little"))
            put(END)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {"size": written, "tiles": done}


def _tile_grid(shape) -> tuple:
    planes = shape[0] if len(shape) == 3 else 1
    height, width = shape[-2:]
    return planes, -(-height // TILE), -(-width // TILE)


class Container:
    """An approximate image opened for reading: its index, and any tile of it
    decoded (`tile`), through one handle under a lock."""

    def __init__(self, path):
        self.path = str(path)
        self._handle = open(self.path, "rb")
        self._lock = threading.Lock()
        try:
            self.index = self._read_index()
        except BaseException:
            self._handle.close()
            raise
        self.shapes = [tuple(level["shape"]) for level in self.index["levels"]]

    def _read_index(self) -> dict:
        handle = self._handle
        if handle.read(len(MAGIC)) != MAGIC:
            raise ValueError("This is not an approximate image.")
        handle.seek(-(8 + len(END)), os.SEEK_END)
        length = int.from_bytes(handle.read(8), "little")
        if handle.read(len(END)) != END:
            raise ValueError("This approximate image is damaged.")
        handle.seek(-(8 + len(END) + length), os.SEEK_END)
        index = json.loads(handle.read(length).decode("utf-8"))
        if index.get("format") != FORMAT or index.get("method") != METHOD:
            raise ValueError("This approximate image was made by a newer Plexora.")
        for level in index["levels"]:
            planes, rows, columns = _tile_grid(tuple(level["shape"]))
            if len(level["tiles"]) != planes * rows * columns:
                raise ValueError("This approximate image is damaged.")
        return index

    def tile_shape(self, level: int, ty: int, tx: int) -> tuple:
        height, width = self.shapes[level][-2:]
        return min(TILE, height - ty * TILE), min(TILE, width - tx * TILE)

    def raw(self, level: int, plane: int, ty: int, tx: int) -> bytes:
        _planes, rows, columns = _tile_grid(self.shapes[level])
        offset, length = self.index["levels"][level]["tiles"][(plane * rows + ty) * columns + tx]
        with self._lock:
            self._handle.seek(offset)
            return self._handle.read(length)

    def tile(self, level: int, plane: int, ty: int, tx: int) -> np.ndarray:
        return decode_tile(self.raw(level, plane, ty, tx), self.tile_shape(level, ty, tx))

    def close(self) -> None:
        self._handle.close()


def read_index(path) -> dict:
    """The index of the approximate image at `path`."""
    container = Container(path)
    try:
        return container.index
    finally:
        container.close()


def decode(source, dest, *, advance: Optional[Callable[[int], None]] = None,
           check: Optional[Callable[[], None]] = None, workers: Optional[int] = None) -> Path:
    """Write the approximate image `source` out as a tiled TIFF at `dest`
    (through `<dest>.part`): the same levels (the pyramid as SubIFDs), its own
    description, zstd. `advance(n)` is told the image's bytes done, in the
    original file's bytes; `check()` may raise to stop."""
    import tifffile

    advance = advance or (lambda count: None)
    check = check or (lambda: None)
    container = Container(source)
    index = container.index
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    count = _workers(workers)
    scale = int((index.get("source") or {}).get("size") or 0) / max(_raw_bytes(container.shapes), 1)
    options = {"tile": (TILE, TILE), "compression": "zstd", "predictor": True,
               "photometric": "minisblack", "metadata": None, "maxworkers": count}

    def tiles(level: int):
        planes, rows, columns = _tile_grid(container.shapes[level])
        keys = [(plane, ty, tx) for plane in range(planes) for ty in range(rows) for tx in range(columns)]
        window = count * 4
        for start in range(0, len(keys), window):
            check()
            jobs = [(container.raw(level, *key), container.tile_shape(level, key[1], key[2]))
                    for key in keys[start:start + window]]
            for tile in pool.map(lambda job: decode_tile(*job), jobs):
                advance(int(tile.nbytes * scale))
                yield tile

    try:
        with ThreadPoolExecutor(count) as pool, tifffile.TiffWriter(part, bigtiff=True, ome=False) as writer:
            for number, shape in enumerate(container.shapes):
                extra = {"subfiletype": 1} if number else {"software": MARK}
                if number == 0 and len(container.shapes) > 1:
                    extra["subifds"] = len(container.shapes) - 1
                if number == 0 and index.get("description"):
                    # As bytes: OME-XML is UTF-8 ("µm"), and tifffile takes
                    # only ASCII text.
                    extra["description"] = index["description"].encode("utf-8")
                if number == 0 and index.get("resolution"):
                    extra["resolution"] = tuple(index["resolution"])
                    extra["resolutionunit"] = index.get("resolutionunit")
                writer.write(tiles(number), shape=shape, dtype=np.uint16, **options, **extra)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        container.close()
    return dest
