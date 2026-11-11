"""Hand-written files in four more vendor formats, for the format tests: Zeiss
ZVI (inside an OLE2 compound file), Zeiss LSM, KFBio KFB and 3DHISTECH MRXS.

Built byte by byte from the layouts in the module docstrings of
`plexora/server/utils/formats/{cfb,zvi,lsm,kfb,mrxs}.py`, from Microsoft's
[MS-CFB] for the compound file and from the CZ_LSMINFO structure as tifffile
documents it -- never with the reader under test -- so a test's expected
values are the arrays written here, not what a reader returns. Everything is
small. Where a docstring left a layout open the real sample files decided
(the ZVI item header's reference before `Tags`, the Scaling record, the MRXS
Index.dat page chains, unscanned position entries of (0, 0, 0)).

Compound files: `write_cfb(path, {"A/B/Stream": bytes})` lays out version 3
(512-byte sectors) or version 4 (4096) files, streams under the 4096-byte
cutoff in the mini stream, the others in regular sectors, every chain in
shuffled, run-wise or contiguous order, optionally with free sectors between
the metadata and the data (a file big enough to need DIFAT sectors). It
returns a `CfbLayout` saying where each stream's bytes are in the file.

ZVI: `write_zvi(path, [ZviPlane(...), ...])`: `Image/Contents`, `Image/Tags`,
`Image/Scaling`, an `Image/Item(n)` per plane, plus the streams a viewer
ignores. `zvi_plane` gives each (z, c, t, scene) its own values.

LSM: `write_lsm(path, ...)`: a little-endian classic TIFF, one data page per
(position, t, z) holding each channel as its own strip, each followed by a
thumbnail page, CZ_LSMINFO on the first page with its channel colours, scan
information, time stamps and channel wavelengths. `lsm_plane` gives each
(position, t, z, channel) its own values.

KFB: `write_kfb(path, KfbSlide(...))`: header, properties, macro, label and
thumbnail sections, JPEG tiles of every level back to back, the tile index.
`kfb_rgb` is the colour at any pixel of any level: 8 x 8 blocks of one colour,
so a JPEG keeps it to within a few levels.

MRXS: `write_mrxs(folder, MrxsSlide(...))`: `<stem>.mrxs` and `<stem>/` with
`Slidedat.ini`, `Index.dat`, `Data0000.dat` (level 0) and `Data0001.dat` (the
coarser levels, the position buffer). `mrxs_expected` is the stitched image
of any level: every scanned photo pasted at its position, later photos over
earlier ones, zeros elsewhere.
"""

from __future__ import annotations

import io
import math
import random
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Compound File Binary (MS-CFB)
# ---------------------------------------------------------------------------

CFB_SIGNATURE = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF
MINI_SECTOR = 64
MINI_CUTOFF = 4096
HEADER_DIFAT = 109


@dataclass
class _Node:
    name: str
    kind: int                       # 1 storage, 2 stream, 5 root
    data: bytes = b""
    children: dict = field(default_factory=dict)
    index: int = -1
    left: int = NOSTREAM
    right: int = NOSTREAM
    child: int = NOSTREAM
    start: int = 0
    size: int = 0
    sectors: list = field(default_factory=list)
    mini: bool = False


@dataclass
class CfbLayout:
    """Where everything went. `pieces[path]` is the stream's bytes as
    `(file offset, length)` runs in stream order."""

    path: Path
    version: int
    sector_size: int
    pieces: dict
    sectors: dict
    mini: dict
    fat_sectors: list
    difat_sectors: list
    directory_sectors: list
    file_size: int
    entry_index: dict

    def held_after_cut(self, stream: str, data: bytes, size: int) -> bytes:
        """`data` (the stream as written) with every byte the file would no
        longer hold once cut to `size` bytes read as zero."""
        out = bytearray(data)
        position = 0
        for offset, length in self.pieces[stream]:
            for k in range(length):
                if offset + k >= size:
                    out[position + k] = 0
            position += length
        return bytes(out)


def _cfb_key(name: str):
    """MS-CFB 2.6.4: a shorter name is less; equal lengths compare upper-cased."""
    return (len(name.encode("utf-16-le")), name.upper())


def _order(numbers: list, layout: str, rng: random.Random) -> list:
    numbers = list(numbers)
    if layout == "shuffled":
        rng.shuffle(numbers)
    elif layout == "runs":
        runs = [numbers[i:i + 3] for i in range(0, len(numbers), 3)]
        rng.shuffle(runs)
        numbers = [n for run in runs for n in run]
    elif layout != "contiguous":
        raise ValueError(layout)
    return numbers


def _tree(streams: dict, storages=()) -> _Node:
    root = _Node("Root Entry", 5)
    for path, data in list(streams.items()) + [(s, None) for s in storages]:
        parts = [p for p in path.split("/") if p]
        node = root
        for depth, part in enumerate(parts):
            last = depth == len(parts) - 1
            key = part.upper()
            if key not in node.children:
                if last and data is not None:
                    node.children[key] = _Node(part, 2, data=bytes(data))
                else:
                    node.children[key] = _Node(part, 1)
            node = node.children[key]
    return root


def _walk(node: _Node):
    yield node
    for key in sorted(node.children):
        yield from _walk(node.children[key])


def _sibling_tree(children: list, shape: str) -> int:
    nodes = sorted(children, key=lambda n: _cfb_key(n.name))
    if not nodes:
        return NOSTREAM
    if shape == "chain":
        for a, b in zip(nodes, nodes[1:]):
            a.right = b.index
        return nodes[0].index
    if shape == "left-chain":
        for a, b in zip(nodes[::-1], nodes[::-1][1:]):
            a.left = b.index
        return nodes[-1].index

    def build(seq):
        if not seq:
            return NOSTREAM
        mid = len(seq) // 2
        node = seq[mid]
        node.left = build(seq[:mid])
        node.right = build(seq[mid + 1:])
        return node.index

    return build(nodes)


def _entry_bytes(node: Optional[_Node], version: int, size_high: int) -> bytes:
    out = bytearray(128)
    if node is None:
        struct.pack_into("<III", out, 68, NOSTREAM, NOSTREAM, NOSTREAM)
        return bytes(out)
    name = node.name.encode("utf-16-le") + b"\x00\x00"
    assert len(name) <= 64, node.name
    out[:len(name)] = name
    struct.pack_into("<HBB", out, 64, len(name), node.kind, 1)
    struct.pack_into("<III", out, 68, node.left, node.right, node.child)
    struct.pack_into("<I", out, 116, node.start)
    high = size_high if (version == 3 and node.kind == 2) else 0
    struct.pack_into("<II", out, 120, node.size & 0xFFFFFFFF, high)
    return bytes(out)


def write_cfb(path, streams: dict, *, version: int = 3, layout: str = "shuffled", seed: int = 0,
              gap: int = 0, storages=(), tree: str = "balanced", size_high: int = 0,
              shuffle_entries: bool = True) -> CfbLayout:
    """A compound file holding `streams` ({"Storage/Sub/Stream": bytes}).

    `layout` orders every chain's sectors ("shuffled", "runs" of three, or
    "contiguous"); `gap` puts that many free sectors between the metadata and
    the regular streams; `tree` shapes each storage's sibling tree
    ("balanced", "chain", "left-chain"); `size_high` goes in the high 32 bits
    of every version-3 stream size, which a reader must ignore."""
    path = Path(path)
    sector = 512 if version == 3 else 4096
    per = sector // 4
    rng = random.Random(seed)
    root = _tree(streams, storages)
    nodes = list(_walk(root))
    others = nodes[1:]
    if shuffle_entries and layout != "contiguous":
        rng.shuffle(others)
    root.index = 0
    for number, node in enumerate(others, start=1):
        node.index = number
    for node in nodes:
        if node.kind in (1, 5):
            node.child = _sibling_tree(list(node.children.values()), tree)

    stream_nodes = [n for n in nodes if n.kind == 2]
    mini_nodes = [n for n in stream_nodes if 0 < len(n.data) < MINI_CUTOFF]
    big_nodes = [n for n in stream_nodes if len(n.data) >= MINI_CUTOFF]

    # -- the mini stream
    mini_count = sum(-(-len(n.data) // MINI_SECTOR) for n in mini_nodes)
    mini_numbers = _order(range(mini_count), layout, rng)
    mini_stream = bytearray(mini_count * MINI_SECTOR)
    mini_fat = [FREESECT] * mini_count
    taken = 0
    for node in mini_nodes:
        count = -(-len(node.data) // MINI_SECTOR)
        chain = mini_numbers[taken:taken + count]
        taken += count
        node.sectors, node.mini, node.start, node.size = chain, True, chain[0], len(node.data)
        for k, number in enumerate(chain):
            piece = node.data[k * MINI_SECTOR:(k + 1) * MINI_SECTOR]
            mini_stream[number * MINI_SECTOR:number * MINI_SECTOR + len(piece)] = piece
            mini_fat[number] = chain[k + 1] if k + 1 < len(chain) else ENDOFCHAIN
    for node in stream_nodes:
        if not node.data:
            node.start, node.size = ENDOFCHAIN, 0

    # -- how many sectors of each kind
    per_dir = sector // 128
    n_dir = -(-len(nodes) // per_dir)
    n_minifat = -(-mini_count * 4 // sector) if mini_count else 0
    n_ministream = -(-len(mini_stream) // sector) if mini_count else 0
    n_data = sum(-(-len(n.data) // sector) for n in big_nodes)
    fat_count, difat_count = 0, 0
    while True:
        total = fat_count + difat_count + n_dir + n_minifat + n_ministream + gap + n_data
        want_fat = -(-total // per)
        want_difat = 0 if want_fat <= HEADER_DIFAT else -(-(want_fat - HEADER_DIFAT) // (per - 1))
        if (want_fat, want_difat) == (fat_count, difat_count):
            break
        fat_count, difat_count = want_fat, want_difat
    meta = fat_count + difat_count + n_dir + n_minifat + n_ministream
    meta_numbers = _order(range(meta), layout, rng)
    data_numbers = _order(range(meta + gap, meta + gap + n_data), layout, rng)

    def take(numbers, count):
        chunk = numbers[:count]
        del numbers[:count]
        return chunk

    fat_sectors = take(meta_numbers, fat_count)
    difat_sectors = take(meta_numbers, difat_count)
    dir_sectors = take(meta_numbers, n_dir)
    minifat_sectors = take(meta_numbers, n_minifat)
    ministream_sectors = take(meta_numbers, n_ministream)
    for node in big_nodes:
        node.sectors = take(data_numbers, -(-len(node.data) // sector))
        node.start, node.size = node.sectors[0], len(node.data)
    root.start = ministream_sectors[0] if ministream_sectors else ENDOFCHAIN
    root.size = len(mini_stream)

    fat = [FREESECT] * (fat_count * per)
    contents = {}

    def chain(numbers, data: bytes):
        for k, number in enumerate(numbers):
            fat[number] = numbers[k + 1] if k + 1 < len(numbers) else ENDOFCHAIN
            contents[number] = data[k * sector:(k + 1) * sector]

    entries = b"".join(_entry_bytes(node, version, size_high) for node in sorted(nodes, key=lambda n: n.index))
    entries += b"".join(_entry_bytes(None, version, 0) for _ in range(n_dir * per_dir - len(nodes)))
    chain(dir_sectors, entries)
    if mini_count:
        chain(minifat_sectors, struct.pack(f"<{len(mini_fat)}I", *mini_fat).ljust(n_minifat * sector, b"\xff"))
        chain(ministream_sectors, bytes(mini_stream))
    for node in big_nodes:
        chain(node.sectors, node.data)
    for number in fat_sectors:
        fat[number] = FATSECT
    for number in difat_sectors:
        fat[number] = DIFSECT
    for k, number in enumerate(fat_sectors):
        contents[number] = struct.pack(f"<{per}I", *fat[k * per:(k + 1) * per])
    rest = fat_sectors[HEADER_DIFAT:]
    for k, number in enumerate(difat_sectors):
        listed = rest[k * (per - 1):(k + 1) * (per - 1)]
        listed = listed + [FREESECT] * (per - 1 - len(listed))
        following = difat_sectors[k + 1] if k + 1 < len(difat_sectors) else ENDOFCHAIN
        contents[number] = struct.pack(f"<{per}I", *listed, following)

    header = bytearray(512)
    header[:8] = CFB_SIGNATURE
    struct.pack_into("<HHHHH", header, 24, 0x003E, version, 0xFFFE, 9 if version == 3 else 12, 6)
    struct.pack_into("<IIII", header, 40, n_dir if version == 4 else 0, fat_count, dir_sectors[0], 0)
    struct.pack_into("<I", header, 56, MINI_CUTOFF)
    struct.pack_into("<II", header, 60, minifat_sectors[0] if minifat_sectors else ENDOFCHAIN,
                     n_minifat)
    struct.pack_into("<II", header, 68, difat_sectors[0] if difat_sectors else ENDOFCHAIN,
                     difat_count)
    listed = fat_sectors[:HEADER_DIFAT] + [FREESECT] * (HEADER_DIFAT - min(HEADER_DIFAT, fat_count))
    struct.pack_into(f"<{HEADER_DIFAT}I", header, 76, *listed)
    total = meta + gap + n_data
    body = bytearray(sector * (1 + total))
    body[:512] = header
    for number, data in contents.items():
        at = (number + 1) * sector
        body[at:at + len(data)] = data
    path.write_bytes(bytes(body))

    # -- where each stream's bytes are
    pieces, sectors, minis, entry_index = {}, {}, {}, {}

    def ministream_offset(position):
        return (ministream_sectors[position // sector] + 1) * sector + position % sector

    def full(node, prefix=""):
        for child in node.children.values():
            name = f"{prefix}{child.name}"
            entry_index[name] = child.index
            if child.kind == 2:
                runs, left = [], child.size
                unit = MINI_SECTOR if child.mini else sector
                for number in child.sectors:
                    length = min(unit, left)
                    at = (ministream_offset(number * MINI_SECTOR) if child.mini
                          else (number + 1) * sector)
                    runs.append((at, length))
                    left -= length
                pieces[name], sectors[name], minis[name] = runs, list(child.sectors), child.mini
            else:
                full(child, name + "/")

    full(root)
    return CfbLayout(path=path, version=version, sector_size=sector, pieces=pieces,
                     sectors=sectors, mini=minis, fat_sectors=fat_sectors,
                     difat_sectors=difat_sectors, directory_sectors=dir_sectors,
                     file_size=len(body), entry_index=entry_index)


def stream_bytes(size: int, seed: int) -> bytes:
    """`size` bytes no two positions of which (mod 2**24) repeat a 3-byte
    window by accident: each 4-byte word is its own index plus `seed`."""
    words = (np.arange(-(-size // 4), dtype=np.uint64) * 2654435761 + seed * 40503) & 0xFFFFFFFF
    return words.astype("<u4").tobytes()[:size]


# ---------------------------------------------------------------------------
# Zeiss ZVI
# ---------------------------------------------------------------------------

#: pixel format -> (numpy dtype, samples) as the zvi.py docstring lists them
ZVI_FORMATS = {1: ("u1", 3), 2: ("u1", 4), 3: ("u1", 1), 4: ("<u2", 1), 5: ("<i4", 1),
               6: ("<f4", 1), 7: ("<f8", 1), 8: ("<u2", 3)}


def _vt_i4(value):
    return struct.pack("<Hi", 3, int(value))


def _vt_r8(value):
    return struct.pack("<Hd", 5, float(value))


def _vt_date(value):
    return struct.pack("<Hd", 7, float(value))


def _vt_bool(value):
    return struct.pack("<Hh", 11, -1 if value else 0)


def _vt_empty():
    return struct.pack("<H", 0)


def _vt_text(text, kind=8):
    raw = text.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<Hi", kind, len(raw)) + raw


def _vt_blob(data):
    return struct.pack("<Hi", 65, len(data)) + data


def _vt_ref():
    return struct.pack("<H", 9) + bytes(16)


def _vt_clsid():
    return struct.pack("<H", 72) + bytes(range(16))


def _tag_list(triples) -> bytes:
    out = _vt_i4(0x20001000) + _vt_i4(len(triples))
    for value, tag, attribute in triples:
        out += value + _vt_i4(tag) + _vt_i4(attribute)
    return out


@dataclass
class ZviPlane:
    """One `Image/Item(n)`. `pixels` is (h, w) grey or (h, w, 3) RGB (written
    in the file's B, G, R order)."""

    pixels: np.ndarray
    z: int = 0
    c: int = 0
    t: int = 0
    tile: Optional[int] = None
    scene: Optional[int] = None
    name: Optional[str] = None
    pixel_format: Optional[int] = None
    bytes_per_pixel: Optional[int] = None
    tags: bool = True
    header_coordinates: bool = True
    focus: Optional[float] = None
    filter_set: Optional[str] = None
    emission: Optional[int] = None


def zvi_plane(z, c, t, height, width, dtype="<u2", scene=0) -> np.ndarray:
    """Plane (z, c, t) of scene `scene`: its own base value plus a ramp of
    0..6, so a plane read from the wrong item, row or column cannot match."""
    ramp = (np.arange(height)[:, None] * 3 + np.arange(width)[None, :]) % 7
    if np.dtype(dtype).itemsize == 1:
        base = (40 * c + 10 * z + 3 * t + 120 * scene) % 240
    else:
        base = 5000 * scene + 1000 * c + 100 * t + 10 * z
    return (base + ramp).astype(dtype)


def zvi_rgb(height, width, seed=0) -> np.ndarray:
    ys = np.arange(height)[:, None]
    xs = np.arange(width)[None, :]
    return np.stack([(ys * 7 + xs * 3 + seed) % 251,
                     (ys * 2 + xs * 11 + 60 + seed) % 251,
                     (ys * 13 + xs * 5 + 120 + seed) % 251], -1).astype(np.uint8)


def _zvi_format(plane: ZviPlane):
    pixels = np.asarray(plane.pixels)
    if plane.pixel_format is not None:
        return plane.pixel_format
    if pixels.ndim == 3:
        return 1 if pixels.dtype == np.uint8 else 8
    return {np.dtype(np.uint8): 3, np.dtype(np.uint16): 4, np.dtype(np.int32): 5,
            np.dtype(np.float32): 6, np.dtype(np.float64): 7}[pixels.dtype]


def _zvi_header(width, height, depth, fmt, count, bits, coordinates) -> bytes:
    return (_vt_i4(0x30001003) + _vt_i4(0) + _vt_empty() + _vt_empty()
            + _vt_i4(width) + _vt_i4(height) + _vt_i4(depth) + _vt_i4(fmt) + _vt_i4(count)
            + _vt_i4(bits) + _vt_blob(bytes(16))
            + _vt_blob(struct.pack("<32i", *coordinates)))


def _zvi_item(plane: ZviPlane) -> bytes:
    pixels = np.asarray(plane.pixels)
    height, width = pixels.shape[:2]
    fmt = _zvi_format(plane)
    if pixels.ndim == 3:
        raw = np.ascontiguousarray(pixels[..., ::-1]).astype(pixels.dtype.newbyteorder("<"))
    else:
        raw = pixels.astype(pixels.dtype.newbyteorder("<"))
    data = raw.tobytes()
    bpp = plane.bytes_per_pixel or (pixels.dtype.itemsize * (pixels.shape[2] if pixels.ndim == 3 else 1))
    bits = 8 * pixels.dtype.itemsize
    coordinates = [0] * 32
    if plane.header_coordinates:
        coordinates[2], coordinates[3], coordinates[4] = plane.z, plane.c, plane.t
    return (_zvi_header(width, height, 1, fmt, 0, bits, coordinates)
            + _vt_ref() + _vt_text("Tags", 69) + _vt_ref() + _vt_ref() + _vt_ref()
            + struct.pack("<7i", 0x10002000, width, height, 1, bpp, fmt, bits) + data)


def _zvi_item_tags(plane: ZviPlane) -> bytes:
    pixels = np.asarray(plane.pixels)
    triples = [(_vt_empty(), 1553, 8),
               (_vt_i4(pixels.shape[1]), 515, 136),
               (_vt_i4(pixels.shape[0]), 516, 136),
               (_vt_date(43123.5), 300, 136),
               (_vt_bool(True), 4001, 0),
               (_vt_clsid(), 4002, 0),
               (_vt_r8(plane.focus if plane.focus is not None else 1234.5), 2072, 129)]
    triples += [(_vt_i4(plane.z), 2819, 129), (_vt_i4(plane.c), 2820, 129),
                (_vt_i4(plane.t), 2821, 129)]
    if plane.tile is not None:
        triples.append((_vt_i4(plane.tile), 2822, 129))
    if plane.scene is not None:
        triples.append((_vt_i4(plane.scene), 2827, 129))
    if plane.name is not None:
        triples.append((_vt_text(plane.name), 1284, 0))
    if plane.filter_set is not None:
        triples.append((_vt_text(plane.filter_set), 2051, 0))
    if plane.emission is not None:
        triples.append((_vt_i4(plane.emission), 0x1000111, 0))
    triples.append((_vt_i4(7), 4003, 0))
    return _tag_list(triples)


def zvi_streams(planes, *, pixel_size=(0.0645, 0.0645), z_step=0.25, unit=76,
                scaling_stream=True, image_tag_scaling=True) -> dict:
    """Every stream of a ZVI holding `planes` (Item(n) in the order given)."""
    first = np.asarray(planes[0].pixels)
    height, width = first.shape[:2]
    fmt = _zvi_format(planes[0])
    sx, sy = pixel_size if pixel_size else (0.0, 0.0)
    sz = z_step or 0.0
    names = ["Tags", "Scaling", "Layers", "RootFolder", "DisplayItem"]
    names += [f"Item({n})" for n in range(len(planes))]
    contents = _zvi_header(width, height, 0, fmt, len(planes), 8 * first.dtype.itemsize, [0] * 32)
    contents += b"".join(_vt_text(name, 69) for name in names)
    image_tags = [(_vt_text("Image"), 1546, 137), (_vt_i4(width), 515, 129),
                  (_vt_i4(height), 516, 129)]
    if image_tag_scaling:
        image_tags += [(_vt_i4(unit), 770, 129), (_vt_r8(sx), 769, 129),
                       (_vt_i4(unit), 773, 129), (_vt_r8(sy), 772, 129),
                       (_vt_i4(unit), 776, 129), (_vt_r8(sz), 775, 129)]
    streams = {
        "SummaryInformation": stream_bytes(88, 1),
        "Tags": stream_bytes(400, 2),
        "Thumbnail": stream_bytes(5000, 3),
        "Image/Contents": contents,
        "Image/Tags/Contents": _tag_list(image_tags),
        "Image/Layers/Contents": stream_bytes(60, 4),
        "Image/RootFolder/Contents": stream_bytes(84, 5),
        "Image/DisplayItem/Contents": stream_bytes(12, 6),
    }
    if scaling_stream:
        scaling = _vt_i4(0x20001002) + _vt_text("Scaling124") + _vt_i4(0)
        for value in (sx, sy, sz):
            scaling += _vt_r8(value) + _vt_i4(unit) + _vt_empty() + _vt_r8(0) + _vt_r8(0)
        scaling += _vt_r8(0) * 12 + _vt_text("Tags", 69)
        streams["Image/Scaling/Contents"] = scaling
    for number, plane in enumerate(planes):
        streams[f"Image/Item({number})/Contents"] = _zvi_item(plane)
        if plane.tags:
            streams[f"Image/Item({number})/Tags/Contents"] = _zvi_item_tags(plane)
    return streams


def write_zvi(path, planes, *, version=3, layout="shuffled", seed=0, gap=0, **kwargs) -> CfbLayout:
    return write_cfb(path, zvi_streams(planes, **kwargs), version=version, layout=layout,
                     seed=seed, gap=gap)


def zvi_stack(*, z=3, c=2, t=1, height=24, width=40, dtype="<u2", names=("Hoechst 33342", "Cy3"),
              order_seed=7, scene=None, tags=True, header_coordinates=True) -> list:
    """The planes of a z/c/t stack, in a shuffled item order."""
    planes = [ZviPlane(zvi_plane(zz, cc, tt, height, width, dtype, scene or 0), z=zz, c=cc, t=tt,
                       scene=scene, name=names[cc] if names and cc < len(names) else None,
                       tags=tags, header_coordinates=header_coordinates)
              for tt in range(t) for cc in range(c) for zz in range(z)]
    random.Random(order_seed).shuffle(planes)
    return planes


# ---------------------------------------------------------------------------
# Zeiss LSM
# ---------------------------------------------------------------------------

#: CZ_LSMINFO (the Carl Zeiss LSM header), as tifffile documents it, then 21
#: reserved uint32: 464 bytes, the StructureSize real files state.
LSMINFO = np.dtype([
    ("MagicNumber", "<u4"), ("StructureSize", "<i4"), ("DimensionX", "<i4"),
    ("DimensionY", "<i4"), ("DimensionZ", "<i4"), ("DimensionChannels", "<i4"),
    ("DimensionTime", "<i4"), ("DataType", "<i4"), ("ThumbnailX", "<i4"), ("ThumbnailY", "<i4"),
    ("VoxelSizeX", "<f8"), ("VoxelSizeY", "<f8"), ("VoxelSizeZ", "<f8"), ("OriginX", "<f8"),
    ("OriginY", "<f8"), ("OriginZ", "<f8"), ("ScanType", "<u2"), ("SpectralScan", "<u2"),
    ("TypeOfData", "<u4"), ("OffsetVectorOverlay", "<u4"), ("OffsetInputLut", "<u4"),
    ("OffsetOutputLut", "<u4"), ("OffsetChannelColors", "<u4"), ("TimeIntervall", "<f8"),
    ("OffsetChannelDataTypes", "<u4"), ("OffsetScanInformation", "<u4"), ("OffsetKsData", "<u4"),
    ("OffsetTimeStamps", "<u4"), ("OffsetEventList", "<u4"), ("OffsetRoi", "<u4"),
    ("OffsetBleachRoi", "<u4"), ("OffsetNextRecording", "<u4"), ("DisplayAspectX", "<f8"),
    ("DisplayAspectY", "<f8"), ("DisplayAspectZ", "<f8"), ("DisplayAspectTime", "<f8"),
    ("OffsetMeanOfRoisOverlay", "<u4"), ("OffsetTopoIsolineOverlay", "<u4"),
    ("OffsetTopoProfileOverlay", "<u4"), ("OffsetLinescanOverlay", "<u4"), ("ToolbarFlags", "<u4"),
    ("OffsetChannelWavelength", "<u4"), ("OffsetChannelFactors", "<u4"),
    ("ObjectiveSphereCorrection", "<f8"), ("OffsetUnmixParameters", "<u4"),
    ("OffsetAcquisitionParameters", "<u4"), ("OffsetCharacteristics", "<u4"),
    ("OffsetPalette", "<u4"), ("TimeDifferenceX", "<f8"), ("TimeDifferenceY", "<f8"),
    ("TimeDifferenceZ", "<f8"), ("InternalUse1", "<u4"), ("DimensionP", "<i4"),
    ("DimensionM", "<i4"), ("DimensionsReserved", "<i4", (16,)), ("OffsetTilePositions", "<u4"),
    ("Reserved", "<u4", (9,)), ("OffsetPositions", "<u4"), ("Reserved2", "<u4", (21,)),
])
assert LSMINFO.itemsize == 464


@dataclass
class LsmTrack:
    """One track of the scan information: its data channels' names and the
    dyes of its acquiring detection channels."""

    data: tuple = ("Ch1",)
    dyes: tuple = ("DAPI",)
    acquire: int = 1
    bleach: int = 0
    name: str = "Track"


def lsm_plane(position, t, z, c, height, width, dtype="<u2") -> np.ndarray:
    ramp = (np.arange(height)[:, None] * 3 + np.arange(width)[None, :]) % 7
    if np.dtype(dtype).itemsize == 1:
        base = (40 * c + 10 * z + 3 * t + 120 * position) % 240
    else:
        base = 5000 * position + 1000 * c + 100 * t + 10 * z
    return (base + ramp).astype(dtype)


def _si(entry, kind, payload=b""):
    return struct.pack("<III", entry, kind, len(payload)) + payload


def _si_long(entry, value):
    return _si(entry, 4, struct.pack("<i", value))


def _si_double(entry, value):
    return _si(entry, 5, struct.pack("<d", value))


def _si_text(entry, text):
    return _si(entry, 2, text.encode("utf-8") + b"\x00")


_SI_END = _si(0xFFFFFFFF, 0)


def _scan_information(tracks, spacing) -> bytes:
    out = struct.pack("<III", 0x10000000, 0, 0) + _si_text(0x10000001, "Recording")
    if spacing:
        sample, line, plane = spacing
        out += (_si_double(0x1000001C, sample) + _si_double(0x1000001D, line)
                + _si_double(0x1000001E, plane))
    out += _si(0x20000000, 0)
    for track in tracks or ():
        out += (_si(0x40000000, 0) + _si_long(0x40000006, track.acquire)
                + _si_text(0x4000000C, track.name) + _si_long(0x40000011, track.bleach))
        out += _si(0x60000000, 0)
        for dye in track.dyes:
            out += (_si(0x70000000, 0) + _si_long(0x7000000B, 1) + _si_text(0x70000026, dye)
                    + _SI_END)
        out += _SI_END + _si(0xC0000000, 0)
        for name in track.data:
            out += (_si(0xD0000000, 0) + _si_text(0xD0000001, name) + _si_long(0xD0000017, 1)
                    + _SI_END)
        out += _SI_END + _SI_END
    out += _SI_END + _SI_END
    return out


def _channel_colors(names) -> bytes:
    count = len(names)
    blob = b"".join(struct.pack("<I", len(n.encode()) + 1) + n.encode() + b"\x00" for n in names)
    size = 24 + 4 * count + len(blob)
    return (struct.pack("<6I", size, count, count, 24, 24 + 4 * count, 0)
            + bytes(4 * count) + blob)


def _ifd(tags, at: int):
    """A classic little-endian IFD at `at` and its out-of-line values. `tags`
    is [(code, type, values, outline)] -- values a list of numbers or bytes,
    `outline` forcing even a 4-byte value out of line (the old LSM writers'
    two BitsPerSample shorts) -- or (code, type, count, ('at', offset))."""
    fmts = {1: "B", 3: "H", 4: "I"}
    tags = sorted(tags, key=lambda tag: tag[0])
    head = bytearray(struct.pack("<H", len(tags)))
    extra = bytearray()
    extra_at = at + 2 + 12 * len(tags) + 4
    for code, kind, values, outline in tags:
        if isinstance(values, tuple) and values and values[0] == "at":
            head += struct.pack("<HHII", code, kind, values[1], values[2])
            continue
        payload = values if isinstance(values, bytes) else struct.pack(
            f"<{len(values)}{fmts[kind]}", *values)
        count = len(values)
        if len(payload) <= 4 and not outline:
            head += struct.pack("<HHI", code, kind, count) + payload.ljust(4, b"\x00")
        else:
            head += struct.pack("<HHII", code, kind, count, extra_at + len(extra))
            extra += payload
            if len(extra) % 2:
                extra += b"\x00"
    head += struct.pack("<I", 0)
    return bytes(head), bytes(extra)


def write_lsm(path, *, height=24, width=40, channels=2, z=3, t=1, positions=0, tiles=0,
              dtype="<u2", scan_type=None, voxel=(0.2e-6, 0.25e-6, 1.5e-6), spacing=None,
              color_names=None, tracks=None, spectral=False, wavelengths=None,
              time_stamps=None, time_interval=0.0, pages_written=None, cut_bytes=0,
              compression=1, thumbnail=(8, 6), magic=0x0400494C, planes=None,
              unwritten=()) -> Path:
    """An LSM file. `positions` / `tiles` set DimensionP / DimensionM (0: none);
    `pages_written` stops after that many data pages (an aborted acquisition);
    `unwritten` lists data pages (by number) whose IFD is there but whose
    strips never were: offsets 0, no bytes;
    `cut_bytes` cuts the end off (the strips are last, in page order);
    `compression` 5 writes LZW strips whose StripByteCounts state the
    uncompressed size, as LSM files do. `planes`, when given, maps
    (position, t, z) to a (C, rows, width) array instead of `lsm_plane`."""
    path = Path(path)
    dtype = np.dtype(dtype)
    if scan_type is None:
        scan_type = 6 if (z > 1 and t > 1) else 3 if t > 1 else 0
    outer = max(1, positions) * max(1, tiles)
    rows = height
    dim_y, dim_z, dim_t = height, z, t
    if scan_type in (1, 2):        # an x-z section or a line scan: rows are z or time
        dim_y = 1
        if scan_type == 1:
            dim_z = height
        else:
            dim_t, dim_z = height, 1
    keys = [(p, tt, zz) for p in range(outer) for tt in range(t if scan_type not in (1, 2) else 1)
            for zz in range(z if scan_type not in (1, 2) else 1)]
    if pages_written is not None:
        keys = keys[:pages_written]
    data = {}
    for key in keys:
        if planes is not None:
            data[key] = np.asarray(planes[key]).astype(dtype)
        else:
            data[key] = np.stack([lsm_plane(*key, c, rows, width, dtype) for c in range(channels)])

    info = np.zeros(1, dtype=LSMINFO)
    info["MagicNumber"], info["StructureSize"] = magic, LSMINFO.itemsize
    info["DimensionX"], info["DimensionY"], info["DimensionZ"] = width, dim_y, dim_z
    info["DimensionChannels"], info["DimensionTime"] = channels, dim_t
    info["DataType"] = 1 if dtype.itemsize == 1 else 2
    info["ThumbnailX"], info["ThumbnailY"] = thumbnail
    info["VoxelSizeX"], info["VoxelSizeY"], info["VoxelSizeZ"] = voxel
    info["ScanType"], info["SpectralScan"] = scan_type, 1 if spectral else 0
    info["TimeIntervall"] = time_interval
    info["DimensionP"], info["DimensionM"] = positions, tiles

    blocks = []        # (field, bytes)
    if color_names is not None:
        blocks.append(("OffsetChannelColors", _channel_colors(color_names)))
    if tracks is not None or spacing is not None:
        blocks.append(("OffsetScanInformation", _scan_information(tracks, spacing)))
    if time_stamps is not None:
        stamps = np.asarray(time_stamps, dtype="<f8")
        blocks.append(("OffsetTimeStamps",
                       struct.pack("<ii", 8 + 8 * len(stamps), len(stamps)) + stamps.tobytes()))
    if wavelengths is not None:
        ranges = np.asarray(wavelengths, dtype="<f8").reshape(-1, 2)
        blocks.append(("OffsetChannelWavelength",
                       struct.pack("<i", len(ranges)) + ranges.tobytes()))

    def strips_of(array):
        out = []
        for plane in array:
            raw = np.ascontiguousarray(plane).astype(dtype.newbyteorder("<")).tobytes()
            if compression == 5:
                import imagecodecs

                out.append((imagecodecs.lzw_encode(raw), len(raw)))
            else:
                out.append((raw, len(raw)))
        return out

    tw, th = thumbnail
    thumb = np.full((3, th, tw), 0xEE, dtype=np.uint8)
    page_strips = []
    for key in keys:
        page_strips.append(strips_of(data[key]))
        page_strips.append([(plane.tobytes(), plane.size) for plane in thumb])

    def page_tags(number, offsets, counts, info_at):
        if number % 2 == 0:
            bits = [8 * dtype.itemsize] * channels
            tags = [(254, 4, [0], False), (256, 4, [width], False), (257, 4, [rows], False),
                    (258, 3, bits, channels == 2), (259, 3, [compression], False),
                    (262, 3, [2 if channels == 3 else 1], False), (273, 4, offsets, False),
                    (277, 3, [channels], False), (279, 4, counts, False), (284, 3, [2], False)]
            if number == 0:
                tags.append((34412, 1, ("at", LSMINFO.itemsize, info_at), False))
        else:
            tags = [(254, 4, [1], False), (256, 4, [tw], False), (257, 4, [th], False),
                    (258, 3, [8, 8, 8], False), (259, 3, [1], False), (262, 3, [2], False),
                    (273, 4, offsets, False), (277, 3, [3], False), (279, 4, counts, False),
                    (284, 3, [2], False)]
        return tags

    def assemble(strip_offsets, info_at):
        out = bytearray(b"II*\x00" + struct.pack("<I", 8))
        ifd_at = []
        for number, strips in enumerate(page_strips):
            at = len(out)
            ifd_at.append(at)
            counts = [stated for _raw, stated in strips]
            head, extra = _ifd(page_tags(number, strip_offsets[number], counts, info_at), at)
            out += head + extra
        for here, following in zip(ifd_at, ifd_at[1:] + [0]):
            count = struct.unpack_from("<H", out, here)[0]
            struct.pack_into("<I", out, here + 2 + 12 * count, following)
        return out

    dummy = [[0] * len(strips) for strips in page_strips]
    region = assemble(dummy, 0)
    info_at = len(region)
    cursor = info_at + LSMINFO.itemsize
    for name, blob in blocks:
        info[name] = cursor
        cursor += len(blob) + (len(blob) % 2)
    strip_offsets = []
    for number, strips in enumerate(page_strips):
        offsets = []
        for raw, _stated in strips:
            if number % 2 == 0 and number // 2 in unwritten:
                offsets.append(0)
                continue
            offsets.append(cursor)
            cursor += len(raw)
        strip_offsets.append(offsets)
    out = assemble(strip_offsets, info_at)
    assert len(out) == info_at
    out += info.tobytes()
    for _name, blob in blocks:
        out += blob + (b"\x00" if len(blob) % 2 else b"")
    for number, strips in enumerate(page_strips):
        if number % 2 == 0 and number // 2 in unwritten:
            continue
        for raw, _stated in strips:
            out += raw
    if cut_bytes:
        out = out[:-cut_bytes]
    path.write_bytes(bytes(out))
    return path


def lsm_strip_offset(path, page: int, channel: int) -> int:
    """Where data page `page`'s channel strip starts (read back with tifffile,
    which is how the fixture's own layout is checked, not the reader under test)."""
    import tifffile

    with tifffile.TiffFile(str(path)) as tif:
        return int(tif.pages[2 * page].dataoffsets[channel])


# ---------------------------------------------------------------------------
# KFBio KFB
# ---------------------------------------------------------------------------


def kfb_rgb(level, ys, xs) -> np.ndarray:
    """The colour the KFB fixture stores at rows `ys`, columns `xs` of level
    `level`: one colour per 8 x 8 block, every block of every level its own."""
    by = (np.asarray(ys)[:, None] // 8).astype(np.int64)
    bx = (np.asarray(xs)[None, :] // 8).astype(np.int64)
    r = (by * 13 + bx * 7 + level * 61) % 200 + 30
    g = (by * 5 + bx * 17 + level * 29 + 85) % 200 + 30
    b = (by * 11 + bx * 3 + level * 97 + 170) % 200 + 30
    return np.stack([r, g, b], -1).astype(np.uint8)


def jpeg_rgb(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(array), "RGB").save(buffer, "JPEG", quality=100,
                                                             subsampling=0)
    return buffer.getvalue()


def jpeg_components(array: np.ndarray) -> bytes:
    """A three-component JPEG whose components are `array[..., k]` as they
    are: written as YCbCr samples, no colour conversion."""
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(array), "YCbCr").save(buffer, "JPEG", quality=100,
                                                               subsampling=0)
    return buffer.getvalue()


@dataclass
class KfbSlide:
    height: int = 300
    width: int = 1300
    tile: int = 128
    magnification: int = 20
    mpp: float = 0.486
    strip_tiles: int = 8
    #: per strip of level 0, how many rows its bottom falls short of `height`
    drops: tuple = (0, 7)
    #: number of levels written (None: down to the last whose sides are both >= 1)
    levels: Optional[int] = None
    #: one more level of the same size as the last (two 1 x 1 levels at the bottom)
    duplicate_bottom: bool = False
    pictures: bool = True
    compression: bytes = b"JPEG"
    version: float = 1.7
    record_order: str = "strips"      # "strips" (scan order), "raster", "shuffled"


def kfb_level_shapes(slide: KfbSlide) -> list:
    shapes = []
    level = 0
    while True:
        h, w = slide.height >> level, slide.width >> level
        if h < 1 or w < 1 or (slide.levels is not None and level >= slide.levels):
            break
        shapes.append((h, w))
        level += 1
    return shapes


def kfb_expected(slide: KfbSlide, level: int) -> np.ndarray:
    h, w = kfb_level_shapes(slide)[level]
    out = kfb_rgb(level, np.arange(h), np.arange(w))
    if level == 0:
        span = slide.strip_tiles * slide.tile
        for strip, drop in enumerate(slide.drops):
            if drop:
                out[h - drop:, strip * span:(strip + 1) * span] = 255
    return out


KFB_PICTURES = {"macro": ((40, 100), (200, 30, 30)), "label": ((40, 50), (30, 200, 30)),
                "thumbnail": ((20, 46), (30, 30, 200))}


def _kfb_section(kind: int, array: np.ndarray, magnification: int) -> bytes:
    jpeg = jpeg_rgb(array)
    h, w = array.shape[:2]
    return (bytes([0xF1, kind, 0xEE, 0xEE])
            + struct.pack("<11I", 1, h, w, 3, len(jpeg), 52, 0, magnification, 0, 0, 0)
            + bytes([0xFF, kind, 0xEE, 0xEE]) + jpeg)


def write_kfb(path, slide: KfbSlide = None, *, cut_bytes=0, seed=3) -> Path:
    path = Path(path)
    slide = slide or KfbSlide()
    shapes = kfb_level_shapes(slide)
    tile = slide.tile
    records = []          # (x, y, w, h, scale, jpeg)
    rng = random.Random(seed)
    level_list = list(enumerate(shapes))
    if slide.duplicate_bottom:
        level_list.append((len(shapes), shapes[-1]))
    for level, (h, w) in level_list:
        scale = slide.magnification / (1 << level)
        cols, rows = -(-w // tile), -(-h // tile)
        span = slide.strip_tiles
        cells = []
        if slide.record_order == "raster":
            cells = [(r, c) for r in range(rows) for c in range(cols)]
        else:
            for strip in range(-(-cols // span)):
                for r in range(rows):
                    for c in range(strip * span, min(cols, (strip + 1) * span)):
                        cells.append((r, c))
            if slide.record_order == "shuffled":
                rng.shuffle(cells)
        content_level = min(level, len(shapes) - 1)
        for r, c in cells:
            x, y = c * tile, r * tile
            bottom = h
            if level == 0:
                strip = c // span
                bottom = h - (slide.drops[strip] if strip < len(slide.drops) else 0)
            tw, th = min(tile, w - x), min(tile, bottom - y)
            if tw <= 0 or th <= 0:
                continue
            pixels = kfb_rgb(content_level, np.arange(y, y + th), np.arange(x, x + tw))
            records.append((x, y, tw, th, scale, jpeg_rgb(pixels)))

    out = bytearray(96)
    properties = [(29, b"KFSCAN00500113025"), (30, struct.pack("<f", slide.version)),
                  (31, struct.pack("<I", tile)), (32, struct.pack("<I", 0))]
    out += struct.pack("<I", len(properties))
    for tag, value in properties:
        out += struct.pack("<II", tag, len(value)) + value
    offsets = {}
    if slide.pictures:
        for name, kind, magnification in (("macro", 2, 20), ("label", 3, 20),
                                          ("thumbnail", 2, 0)):
            (ph, pw), colour = KFB_PICTURES[name]
            offsets[name] = len(out)
            out += _kfb_section(kind, np.full((ph, pw, 3), colour, dtype=np.uint8), magnification)
    tile_at = []
    for record in records:
        tile_at.append(len(out))
        out += record[5]
    index_at = len(out)
    for (x, y, tw, th, scale, jpeg), at in zip(records, tile_at):
        out += struct.pack("<IIIIIfIIIq4II", 0xEEEE04F1, x, y, tw, th, scale, 0, 0, len(jpeg),
                           at - index_at, 20, 20, 20, 20, 0xEEEE04FF)
    struct.pack_into("<I4s", out, 0, 0xEEEE01F1, b"KFB\x00")
    struct.pack_into("<fIIII4s", out, 0x0C, slide.version, len(records), slide.height,
                     slide.width, slide.magnification, slide.compression)
    struct.pack_into("<IQII", out, 0x28, 36000, 1_600_000_000, offsets.get("macro", 0),
                     offsets.get("label", 0))
    struct.pack_into("<QQf", out, 0x3C, offsets.get("thumbnail", 0), index_at, slide.mpp)
    struct.pack_into("<I", out, 0x58, tile)
    out[0x5C:0x60] = bytes([0xFF, 0x01, 0xEE, 0xEE])
    data = bytes(out)
    if cut_bytes:
        data = data[:-cut_bytes]
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# 3DHISTECH MRXS
# ---------------------------------------------------------------------------


@dataclass
class MrxsSlide:
    photos_x: int = 3
    photos_y: int = 2
    div: int = 2                     # CameraImageDivisionsPerSide
    dw: int = 64                     # DIGITIZER_WIDTH
    dh: int = 48                     # DIGITIZER_HEIGHT
    levels: int = 4
    overlap: tuple = (16, 16)
    #: (px, py) -> level-0 (x, y); None: the default jittered layout
    positions: Optional[dict] = None
    #: "plain" (VIMSLIDE_POSITION_BUFFER), "deflate" (2.2's
    #: StitchingIntensityLayer), "none" (no buffer: the nominal grid)
    position_buffer: str = "plain"
    unscanned: frozenset = frozenset()
    #: (FILTER_NAME, STORING_CHANNEL_NUMBER) in the filter layer's order
    filters: tuple = (("Rhodamine", 2), ("FITC", 0), ("UV", 1))
    slide_type: str = "SLIDE_TYPE_FLUORESCENCE"
    mpp: tuple = (0.3225, 0.3226)
    image_format: str = "JPEG"
    concat: Optional[tuple] = None
    data_in_filter: Optional[tuple] = None
    slide_id: str = "7C691E3F2DC6412781AECA3E3B3213C1"
    items_per_page: int = 5
    version: str = "1.9"

    @property
    def photo_shape(self):
        return self.div * self.dh, self.div * self.dw

    @property
    def images_x(self):
        return self.photos_x * self.div

    @property
    def images_y(self):
        return self.photos_y * self.div

    def concat_factors(self):
        return self.concat or (0,) + (1,) * (self.levels - 1)

    def placed(self) -> dict:
        """Level-0 (x, y) of every photo."""
        if self.positions is not None:
            return dict(self.positions)
        ph, pw = self.photo_shape
        pitch_x, pitch_y = pw - self.overlap[0], ph - self.overlap[1]
        jitter = {(1, 0): (8, 0), (2, 0): (0, 8), (0, 1): (-8, 0), (1, 1): (0, -8),
                  (2, 1): (8, 8), (0, 2): (8, -8), (1, 2): (-8, 8)}
        out = {}
        for py in range(self.photos_y):
            for px in range(self.photos_x):
                jx, jy = jitter.get((px, py), (0, 0))
                out[(px, py)] = (-24 + px * pitch_x + jx, 1000 + py * pitch_y + jy)
        return out

    def nominal(self) -> dict:
        ph, pw = self.photo_shape
        d0 = 1 << self.concat_factors()[0]
        pitch_x = self.div * self.dw / d0 - self.overlap[0]
        pitch_y = self.div * self.dh / d0 - self.overlap[1]
        return {(px, py): (int(px * pitch_x), int(py * pitch_y))
                for py in range(self.photos_y) for px in range(self.photos_x)}


def mrxs_photo(slide: MrxsSlide, filter_number: int, px: int, py: int) -> np.ndarray:
    """Filter `filter_number`'s grey image of photo (px, py): 16 x 16 blocks."""
    ph, pw = slide.photo_shape
    by = np.arange(ph)[:, None] // 16
    bx = np.arange(pw)[None, :] // 16
    p = py * slide.photos_x + px
    return ((filter_number * 80 + p * 23 + by * 11 + bx * 5) % 230 + 15).astype(np.uint8)


def _down(array: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return array
    h, w = array.shape[-2:]
    shaped = array.reshape(array.shape[:-2] + (h // factor, factor, w // factor, factor))
    return np.rint(shaped.astype(np.float64).mean(axis=(-3, -1))).astype(np.uint8)


def _sources(slide: MrxsSlide) -> list:
    """(JPEG component, content number) pairs: a fluorescence slide's filter
    n is stored in component STORING_CHANNEL_NUMBER; a brightfield slide's
    three components are a colour picture (content 0, 1, 2 = R, G, B)."""
    if slide.slide_type == "SLIDE_TYPE_FLUORESCENCE":
        return [(storing, number) for number, (_name, storing) in enumerate(slide.filters)]
    return [(k, k) for k in range(3)]


def mrxs_expected(slide: MrxsSlide, level: int, positions: Optional[dict] = None) -> np.ndarray:
    """(filters, h, w) of `level`, filters in the filter layer's order (R, G,
    B for a brightfield slide): every scanned photo at its position scaled to
    the level, row-major photos later over earlier, zeros elsewhere, the image
    the scanned photos' bounding box."""
    positions = positions or (slide.placed() if slide.position_buffer != "none" else slide.nominal())
    scanned = [(px, py) for py in range(slide.photos_y) for px in range(slide.photos_x)
               if (px, py) not in slide.unscanned]
    ph, pw = slide.photo_shape
    x0 = min(positions[p][0] for p in scanned)
    y0 = min(positions[p][1] for p in scanned)
    x1 = max(positions[p][0] for p in scanned) + pw
    y1 = max(positions[p][1] for p in scanned) + ph
    factor = 1 << sum(slide.concat_factors()[1:level + 1])     # D_level / D_0
    assert (x1 - x0) % factor == 0 and (y1 - y0) % factor == 0
    numbers = [number for _component, number in _sources(slide)]
    out = np.zeros((len(numbers), (y1 - y0) // factor, (x1 - x0) // factor), np.uint8)
    for px, py in scanned:
        x, y = positions[(px, py)]
        assert (x - x0) % factor == 0 and (y - y0) % factor == 0
        ty, tx = (y - y0) // factor, (x - x0) // factor
        for number in numbers:
            piece = _down(mrxs_photo(slide, number, px, py), factor)
            out[number, ty:ty + piece.shape[0], tx:tx + piece.shape[1]] = piece
    return out


def _ini(sections: dict) -> str:
    lines = []
    for name, entries in sections.items():
        lines.append(f"[{name}]")
        lines += [f"{key} = {value}" for key, value in entries]
    return "\r\n".join(lines) + "\r\n"


def _stored_images(slide: MrxsSlide, level: int) -> dict:
    """{image index: (dh, dw, components) uint8} of a zoom level: each the
    D x D camera images from its top-left one, squeezed to one stored image."""
    d = 1
    for factor in slide.concat_factors()[:level + 1]:
        d <<= factor
    ph, pw = slide.photo_shape
    mosaic = np.zeros((3, slide.images_y * slide.dh, slide.images_x * slide.dw), np.uint8)
    for py in range(slide.photos_y):
        for px in range(slide.photos_x):
            if (px, py) in slide.unscanned:
                continue
            for component, number in _sources(slide):
                mosaic[component, py * ph:(py + 1) * ph, px * pw:(px + 1) * pw] = \
                    mrxs_photo(slide, number, px, py)
    out = {}
    for cy in range(0, slide.images_y, d):
        for cx in range(0, slide.images_x, d):
            photos = {((cx + i) // slide.div, (cy + j) // slide.div)
                      for i in range(d) for j in range(d)
                      if cx + i < slide.images_x and cy + j < slide.images_y}
            if all(p in slide.unscanned for p in photos):
                continue
            block = np.zeros((3, d * slide.dh, d * slide.dw), np.uint8)
            part = mosaic[:, cy * slide.dh:(cy + d) * slide.dh, cx * slide.dw:(cx + d) * slide.dw]
            block[:, :part.shape[1], :part.shape[2]] = part
            out[cy * slide.images_x + cx] = np.moveaxis(_down(block, d), 0, -1)
    return out


def _position_bytes(slide: MrxsSlide) -> bytes:
    placed = slide.placed()
    out = bytearray()
    for py in range(slide.photos_y):
        for px in range(slide.photos_x):
            if (px, py) in slide.unscanned:
                out += struct.pack("<Bii", 0, 0, 0)
            else:
                out += struct.pack("<Bii", 1, *placed[(px, py)])
    return bytes(out)


def write_mrxs(folder, slide: MrxsSlide = None, stem="slide") -> Path:
    folder = Path(folder)
    slide = slide or MrxsSlide()
    data_dir = folder / stem
    data_dir.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.mrxs").write_bytes(b"3DHISTECH MIRAX slide (unused)\x00" * 4)
    sid = slide.slide_id.encode("ascii")
    files = [bytearray(), bytearray()]
    for number, blob in enumerate(files):
        blob += (b"01.01" + sid + f"{number:03d}".encode()).ljust(296, b"0")

    def store(number, payload: bytes):
        at = len(files[number])
        files[number] += payload
        return at, len(payload), number

    rgb = slide.slide_type != "SLIDE_TYPE_FLUORESCENCE"
    hier_items = []
    for level in range(slide.levels):
        items = []
        for index, image in sorted(_stored_images(slide, level).items()):
            if slide.image_format == "JPEG":
                payload = jpeg_rgb(image) if rgb else jpeg_components(image)
            else:
                buffer = io.BytesIO()
                Image.fromarray(image, "RGB").save(buffer, "PNG")
                payload = buffer.getvalue()
            at, length, number = store(0 if level == 0 else 1, payload)
            items.append((index, at, length, number))
        hier_items.append(items)

    nonhier = [("Scan data layer", "ScanDataLayer_SlideThumbnail",
                store(1, jpeg_rgb(np.full((16, 24, 3), 90, np.uint8)))),
               ("VIMSLIDE_HISTOGRAM_DATA", "default", store(1, bytes(3 * 256 * 8)))]
    if slide.position_buffer == "plain":
        nonhier.append(("VIMSLIDE_POSITION_BUFFER", "default", store(1, _position_bytes(slide))))
    elif slide.position_buffer == "deflate":
        nonhier.append(("StitchingIntensityLayer", "StitchingIntensityLevel",
                        store(1, zlib.compress(_position_bytes(slide)))))
    for number, blob in enumerate(files):
        (data_dir / f"Data{number:04d}.dat").write_bytes(bytes(blob))

    # -- Index.dat
    index = bytearray(b"01.02" + sid + bytes(8))
    hier_count = slide.levels + len(slide.filters) + 1
    hier_table_at = len(index)
    index += bytes(4 * hier_count)
    nonhier_table_at = len(index)
    index += bytes(4 * len(nonhier))
    struct.pack_into("<ii", index, 37, hier_table_at, nonhier_table_at)

    def chain(rows, width):
        first = len(index)
        index.extend(struct.pack("<ii", 0, 0))
        previous = first
        per = slide.items_per_page
        for start in range(0, len(rows), per):
            page = rows[start:start + per]
            at = len(index)
            struct.pack_into("<i", index, previous + 4, at)
            index.extend(struct.pack("<ii", len(page), 0))
            for row in page:
                index.extend(struct.pack(f"<{width}i", *row))
            previous = at
        return first

    for level, items in enumerate(hier_items):
        struct.pack_into("<i", index, hier_table_at + 4 * level, chain(items, 4))
    for k, (_layer, _value, (at, length, number)) in enumerate(nonhier):
        struct.pack_into("<i", index, nonhier_table_at + 4 * k,
                         chain([(0, 0, at, length, number)], 5))
    (data_dir / "Index.dat").write_bytes(bytes(index))

    # -- Slidedat.ini
    general = [("SLIDE_VERSION", "01.03"), ("SLIDE_NAME", stem), ("SLIDE_ID", slide.slide_id),
               ("IMAGENUMBER_X", slide.images_x), ("IMAGENUMBER_Y", slide.images_y),
               ("CURRENT_SLIDE_VERSION", slide.version), ("SLIDE_TYPE", slide.slide_type),
               ("CameraImageDivisionsPerSide", slide.div)]
    hier = [("INDEXFILE", "Index.dat"), ("HIER_COUNT", 3), ("NONHIER_COUNT", len(nonhier)),
            ("HIER_0_NAME", "Slide zoom level"), ("HIER_0_COUNT", slide.levels),
            ("HIER_0_SECTION", "LAYER_0_SECTION")]
    for level in range(slide.levels):
        hier += [(f"HIER_0_VAL_{level}", f"ZoomLevel_{level}"),
                 (f"HIER_0_VAL_{level}_SECTION", f"LAYER_0_LEVEL_{level}_SECTION")]
    hier += [("HIER_1_NAME", "Slide filter level"), ("HIER_1_COUNT", len(slide.filters)),
             ("HIER_1_SECTION", "LAYER_1_SECTION")]
    for number in range(len(slide.filters)):
        hier += [(f"HIER_1_VAL_{number}", f"FilterLevel_{number}"),
                 (f"HIER_1_VAL_{number}_SECTION", f"LAYER_1_LEVEL_{number}_SECTION")]
    hier += [("HIER_2_NAME", "Scan info layer"), ("HIER_2_COUNT", 1),
             ("HIER_2_SECTION", "LAYER_2_SECTION"),
             ("HIER_2_VAL_0", "ScanInfoLayer_DefaultLevel"),
             ("HIER_2_VAL_0_SECTION", "LAYER_2_LEVEL_0_SECTION")]
    for k, (layer, value, _where) in enumerate(nonhier):
        hier += [(f"NONHIER_{k}_NAME", layer), (f"NONHIER_{k}_COUNT", 1),
                 (f"NONHIER_{k}_SECTION", f"NONHIERLAYER_{k}_SECTION"),
                 (f"NONHIER_{k}_VAL_0", value),
                 (f"NONHIER_{k}_VAL_0_SECTION", f"NONHIERLAYER_{k}_LEVEL_0_SECTION")]
    sections = {"GENERAL": general, "HIERARCHICAL": hier,
                "DATAFILE": [("FILE_COUNT", 2), ("FILE_0", "Data0000.dat"),
                             ("FILE_1", "Data0001.dat")]}
    for level, factor in enumerate(slide.concat_factors()):
        scale = 1 << level
        sections[f"LAYER_0_LEVEL_{level}_SECTION"] = [
            ("IMAGE_FILL_COLOR_BGR", 0), ("MICROMETER_PER_PIXEL_X", slide.mpp[0] * scale),
            ("MICROMETER_PER_PIXEL_Y", slide.mpp[1] * scale), ("DIGITIZER_WIDTH", slide.dw),
            ("DIGITIZER_HEIGHT", slide.dh), ("OVERLAP_X", slide.overlap[0] / scale),
            ("OVERLAP_Y", slide.overlap[1] / scale), ("IMAGE_CONCAT_FACTOR", factor),
            ("IMAGE_FORMAT", slide.image_format), ("IMAGE_COMPRESSION_FACTOR", 90)]
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for number, (name, storing) in enumerate(slide.filters):
        r, g, b = colours[number % 3]
        data_in = (slide.data_in_filter[number] if slide.data_in_filter else "FilterLevel_0")
        sections[f"LAYER_1_LEVEL_{number}_SECTION"] = [
            ("FILTER_NAME", name), ("XDIFF", 0), ("YDIFF", 0), ("COLOR_R", r), ("COLOR_G", g),
            ("COLOR_B", b), ("STORING_CHANNEL_NUMBER", storing),
            ("DATA_IN_THIS_FILTER_LEVEL", data_in)]
    (data_dir / "Slidedat.ini").write_bytes(_ini(sections).encode("utf-8"))
    return folder / f"{stem}.mrxs"
