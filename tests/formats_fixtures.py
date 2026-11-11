"""Hand-written files in the three legacy microscope formats, for the format tests.

Built from the byte layouts in the module docstrings of
`plexora/server/utils/formats/{dv,vsi,ndpis}.py` and never with the reader
under test, so a test's expected values come from the arrays written here and
not from what the reader returns. Everything is tiny.

DeltaVision: `write_dv`. Plane (z, w, t) holds `dv_value(z, w, t)` at its first
pixel and a small ramp over the rest, so every plane is identifiable and a
region read can be checked against a slice of what was written.

Olympus VSI: `write_vsi`. A `.vsi` whose TIFF part is degenerate (the property
tree starts at byte 8) plus `_<stem>_/stack<id>/frame_t.ets` per stack, raw
tiles, any dimensions and an optional pyramid.

NDPIS: `write_ndpis`. A CRLF text file naming pyramidal `.ndpi` TIFFs.
"""

from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# DeltaVision
# ---------------------------------------------------------------------------

#: header mode -> numpy dtype (as the file's docstring lists them)
DV_MODES = {0: "u1", 1: "i2", 2: "f4", 6: "u2"}
#: image sequence number -> the order the planes are stored in, fastest first
DV_ORDERS = {0: ("z", "t", "w"), 1: ("w", "z", "t"), 2: ("z", "w", "t")}


def dv_value(z: int, w: int, t: int) -> int:
    return 1000 * w + 100 * t + z


def dv_plane(z, w, t, height, width, dtype) -> np.ndarray:
    """Plane (z, w, t): `10 * id` plus a ramp of 0..6 over the pixels."""
    ramp = (np.arange(height * width).reshape(height, width) % 7)
    return (dv_value(z, w, t) * 10 + ramp).astype(dtype)


def write_dv(path, *, height=24, width=32, depth=3, waves=2, times=2, sequence=0, mode=6,
             endian="<", pixel_size=(0.0645, 0.0645, 0.2), wavelengths=(523, 608, 0, 0, 0),
             extended=128, cut_bytes=0, sections=None) -> Path:
    """A DeltaVision file. `cut_bytes` removes that many bytes from the end
    (an interrupted acquisition); `sections` overrides the plane count the
    header states."""
    path = Path(path)
    dtype = np.dtype(DV_MODES[mode]).newbyteorder(endian)
    count = depth * waves * times if sections is None else sections
    header = bytearray(1024)
    struct.pack_into(endian + "4i", header, 0, width, height, count, mode)
    struct.pack_into(endian + "3f", header, 40, *pixel_size)
    struct.pack_into(endian + "i", header, 92, extended)
    struct.pack_into(endian + "H", header, 96, 0xC0A0)
    struct.pack_into(endian + "2h", header, 180, times, sequence)
    struct.pack_into(endian + "h", header, 196, waves)
    struct.pack_into(endian + "5h", header, 198, *wavelengths)
    order = DV_ORDERS[sequence]
    sizes = {"z": depth, "w": waves, "t": times}
    body = bytearray()
    # the first axis in `order` varies fastest
    for combo in itertools.product(*[range(sizes[axis]) for axis in reversed(order)]):
        index = dict(zip(reversed(order), combo))
        plane = dv_plane(index["z"], index["w"], index["t"], height, width, dtype)
        body += plane.tobytes()
    data = bytes(header) + b"\xab" * extended + bytes(body)
    if cut_bytes:
        data = data[:-cut_bytes]
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Olympus VSI + ETS
# ---------------------------------------------------------------------------

_EXTENDED = 0x10000000
_EXTRA_TAG = 0x08000000
_INLINE = 0x40000000


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


@dataclass
class Fld:
    """One field of a property volume."""

    tag: int
    kind: int = 0
    data: bytes = b""
    index: Optional[int] = None
    children: Optional[list] = None       # a nested volume
    inline: Optional[int] = None          # a value stored in the size field


def f_text(tag, text, index=None):
    return Fld(tag, 0x2000, _utf16(text), index)


def f_int(tag, value):
    return Fld(tag, 5 | _INLINE, inline=value)


def f_double(tag, value):
    return Fld(tag, 10, struct.pack("<d", value))


def f_box(tag, x, y, width, height):
    return Fld(tag, 259, struct.pack("<4i", x, y, width, height))


def f_pair(tag, a, b):
    return Fld(tag, 260, struct.pack("<2d", a, b))


def f_sizes(tag, sizes):
    return Fld(tag, 0x2003, struct.pack(f"<{len(sizes)}H", *sizes))


def f_volume(tag, children, index=None):
    return Fld(tag, 0, children=children, index=index)


def volume_bytes(fields: list) -> bytes:
    """A volume: its 24-byte header (int16 size, uint16 0x5349, int32 version,
    int64 first-field offset, int32 field count, 4 spare) and its fields."""
    blobs = []
    for item in fields:
        kind = item.kind
        index = item.index
        if index is not None:
            kind |= _EXTRA_TAG
        nested = b""
        data = item.data
        size = len(data)
        if item.children is not None:
            kind |= _EXTENDED
            nested = volume_bytes(item.children)
            size = len(nested)
        if item.inline is not None:
            size = item.inline
        extra = struct.pack("<i", index) if index is not None else b""
        blobs.append((kind, item.tag, size, extra + data + nested))
    out = bytearray(struct.pack("<hHiqi", 24, 0x5349, 2, 24, len(fields)) + b"\x00" * 4)
    for number, (kind, tag, size, payload) in enumerate(blobs):
        following = len(out) + 16 + len(payload) if number < len(blobs) - 1 else 0
        out += struct.pack("<iiIi", kind, tag, following, size) + payload
    return bytes(out)


def vsi_value(level, c, z, y, x, sample=None):
    """The pixel the ETS fixture stores at (y, x) of channel c, plane z, level
    `level`: a function of every coordinate, so a read from the wrong tile,
    level, channel or plane cannot match by accident. `sample` is a colour
    component (RGB stacks)."""
    y = np.asarray(y)[:, None]
    x = np.asarray(x)[None, :]
    base = (y * 31 + x * 17 + 5000 * c + 600 * z + 2000 * level + 1) % 60000
    if sample is None:
        return base
    return (base + 40 * sample) % 251


@dataclass
class VsiStack:
    ident: int
    name: str = "Scan"
    height: int = 601
    width: int = 1001
    tile: int = 256
    pyramid: bool = True
    #: the extra dimensions in the ETS's coordinate order: (kind, size) with
    #: kind 1 = z, 4 = channel
    dims: list = field(default_factory=lambda: [(4, 2)])
    channel_names: tuple = ("DAPI", "Cy5")
    pixel_size: Optional[tuple] = (0.5, 0.5)
    z_step: Optional[float] = None
    pixel_type: int = 4                      # 2 uint8, 4 uint16
    samples: int = 1
    background: int = 77
    missing: set = field(default_factory=set)     # {(level, ty, tx)}
    levels: int = 2


def _ets_bytes(stack: VsiStack) -> bytes:
    itemsize = 1 if stack.pixel_type == 2 else 2
    dtype = np.uint8 if stack.pixel_type == 2 else np.uint16
    tile, samples = stack.tile, stack.samples
    ndim = 2 + len(stack.dims) + (1 if stack.pyramid else 0)
    head = bytearray(64)
    ets = bytearray(160)
    ets[0:4] = b"ETS\x00"
    struct.pack_into("<9i", ets, 4, 0x30001, stack.pixel_type, samples,
                     1 if samples == 3 else 0, 0, 90, tile, tile, 1)
    struct.pack_into("<10i", ets, 108, stack.background, *([0] * 9))
    struct.pack_into("<ii", ets, 148, 0, 1 if stack.pyramid else 0)
    body = bytearray()
    entries = []
    base = 64 + 160
    levels = range(stack.levels if stack.pyramid else 1)
    extra_ranges = [range(size) for _kind, size in stack.dims]
    for level in levels:
        height = -(-stack.height // (1 << level))
        width = -(-stack.width // (1 << level))
        for extras in itertools.product(*extra_ranges):
            kinds = {kind: value for (kind, _size), value in zip(stack.dims, extras)}
            c, z = kinds.get(4, 0), kinds.get(1, 0)
            ys, xs = np.arange(height), np.arange(width)
            if samples == 3:
                plane = np.stack([vsi_value(level, c, z, ys, xs, s) for s in range(3)], -1)
            else:
                plane = vsi_value(level, c, z, ys, xs)
            plane = plane.astype(dtype)
            rows, cols = -(-height // tile), -(-width // tile)
            for ty in range(rows):
                for tx in range(cols):
                    if (level, ty, tx) in stack.missing:
                        continue
                    padded = np.full((tile, tile) + ((3,) if samples == 3 else ()), 9999 % 251,
                                     dtype=dtype)
                    piece = plane[ty * tile:(ty + 1) * tile, tx * tile:(tx + 1) * tile]
                    padded[:piece.shape[0], :piece.shape[1]] = piece
                    raw = padded.tobytes()
                    coords = [tx, ty, *extras] + ([level] if stack.pyramid else [])
                    entries.append((coords, base + len(body), len(raw)))
                    body += raw
    index_at = base + len(body)
    table = bytearray()
    for coords, offset, size in entries:
        table += struct.pack("<i", 0) + struct.pack(f"<{ndim}i", *coords)
        table += struct.pack("<qi", offset, size) + struct.pack("<i", 0)
    struct.pack_into("<4siiiqiiqi", head, 0, b"SIS\x00", 64, 4, ndim, 64, 160, 0,
                     index_at, len(entries))
    return bytes(head) + bytes(ets) + bytes(body) + bytes(table)


def _stack_fields(stack: VsiStack) -> list:
    kids = [f_text(2030, stack.name),
            f_box(2053, 0, 0, stack.width, stack.height)]
    if stack.pixel_size:
        kids.append(f_pair(2019, *stack.pixel_size))
    kids.append(f_sizes(2003, [size for _kind, size in stack.dims]))
    for position, (kind, size) in enumerate(stack.dims):
        entries = []
        for entry in range(size):
            inner = []
            if entry == 0:
                inner.append(f_int(2023, kind))
            if kind == 4:
                inner.append(f_text(2419, stack.channel_names[entry]))
            if kind == 1 and entry == 0 and stack.z_step:
                inner.append(f_volume(2013, [f_double(2014, stack.z_step),
                                             f_text(2015, "10^-6m^1")]))
            entries.append(f_volume(2008, inner, index=entry))
        kids.append(f_volume(2007, entries, index=position))
    return kids


def write_vsi(folder, stem="slide", stacks=None, with_pixels=True) -> Path:
    """`<folder>/<stem>.vsi` and, unless `with_pixels` is False, its
    `_<stem>_/stack<id>/frame_t.ets` files."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stacks = [VsiStack(2)] if stacks is None else stacks
    root = volume_bytes([f_volume(2001, _stack_fields(stack), index=stack.ident)
                         for stack in stacks])
    path = folder / f"{stem}.vsi"
    # a TIFF header whose first IFD is far past the end, then the tree at byte 8
    path.write_bytes(b"II*\x00" + struct.pack("<I", 0x7FFFFFF0) + root)
    if with_pixels:
        for stack in stacks:
            where = folder / f"_{stem}_" / f"stack{stack.ident}"
            where.mkdir(parents=True, exist_ok=True)
            (where / "frame_t.ets").write_bytes(_ets_bytes(stack))
    return path


def vsi_expected(stack: VsiStack, level, c, z, y0, y1, x0, x1):
    return vsi_value(level, c, z, np.arange(y0, y1), np.arange(x0, x1)).astype(
        np.uint8 if stack.pixel_type == 2 else np.uint16)


# ---------------------------------------------------------------------------
# NDPIS
# ---------------------------------------------------------------------------


def in_colour(plane, sample):
    """`plane` as an RGB picture with its signal only in colour sample `sample`
    (the way the scanner stores a fluorescence channel)."""
    rgb = np.zeros(plane.shape + (3,), dtype=plane.dtype)
    rgb[..., sample] = plane
    return rgb


def write_ndpi(path, data, name=None, *, levels=2, resolution=None, tile=64):
    """A pyramidal NDPI: a classic TIFF with 64-bit offsets, the way
    Hamamatsu writes it (tifffile reads a `.ndpi` file name as one and cannot
    write one, so the bytes are laid out here).

    Header `II*\0` and an 8-byte first-IFD offset. Each IFD: the tag count,
    12-byte tags (value or low 32 bits of the offset), the next IFD's 8-byte
    offset, then the high 32 bits of every tag's value. Pages are the
    levels, finest first, uncompressed tiles. `data` is (H, W) or (H, W, 3);
    `name` goes in tag 65434; `resolution` is the pixel size in um.
    """
    data = np.asarray(data)
    color = data.ndim == 3
    bits = data.dtype.itemsize * 8
    pages = []
    for level in range(levels):
        plane = data[::1 << level, ::1 << level]
        pages.append(plane)
    out = bytearray(b"II*\x00" + struct.pack("<Q", 12))
    first_ifd_at = []

    def blob(payload: bytes) -> int:
        at = len(out)
        out.extend(payload)
        if len(out) % 2:
            out.append(0)
        return at

    # tiles first, so the IFDs can name them
    tile_tables = []
    for plane in pages:
        height, width = plane.shape[:2]
        offsets, counts = [], []
        for ty in range(-(-height // tile)):
            for tx in range(-(-width // tile)):
                padded = np.zeros((tile, tile) + ((3,) if color else ()), dtype=plane.dtype)
                piece = plane[ty * tile:(ty + 1) * tile, tx * tile:(tx + 1) * tile]
                padded[:piece.shape[0], :piece.shape[1]] = piece
                raw = padded.tobytes()
                offsets.append(blob(raw))
                counts.append(len(raw))
        tile_tables.append((offsets, counts))

    ifd_positions = []
    for number, plane in enumerate(pages):
        height, width = plane.shape[:2]
        offsets, counts = tile_tables[number]
        tags = []          # (code, type, count, payload bytes)

        def add(code, kind, values):
            fmt = {3: "H", 4: "I", 5: "II"}[kind]
            if kind == 5:
                payload = b"".join(struct.pack("<II", *pair) for pair in values)
            else:
                payload = struct.pack(f"<{len(values)}{fmt}", *values)
            tags.append((code, kind, len(values), payload))

        add(254, 4, [0 if number == 0 else 1])
        add(256, 4, [width])
        add(257, 4, [height])
        add(258, 3, [bits] * (3 if color else 1))
        add(259, 3, [1])
        add(262, 3, [2 if color else 1])
        add(277, 3, [3 if color else 1])
        add(284, 3, [1])
        add(322, 4, [tile])
        add(323, 4, [tile])
        add(324, 4, offsets)
        add(325, 4, counts)
        if resolution:
            per_cm = int(round(1e4 / resolution * 1000))
            add(282, 5, [(per_cm, 1000)])
            add(283, 5, [(per_cm, 1000)])
            add(296, 3, [3])
        if number == 0 and name is not None:
            text = name.encode("latin-1") + b"\x00"
            tags.append((65434, 2, len(text), text))
        tags.sort(key=lambda tag: tag[0])
        sizes = {2: 1, 3: 2, 4: 4, 5: 8}
        # out-of-line values first (anything over 4 bytes)
        placed = []
        for code, kind, count, payload in tags:
            if len(payload) <= 4 and kind != 2:      # an NDPI string is never inline
                low, high = int.from_bytes(payload.ljust(4, b"\x00"), "little"), 0
            else:
                where = blob(payload)
                low, high = where & 0xFFFFFFFF, where >> 32
            placed.append((code, kind, count, low, high))
        at = len(out)
        ifd_positions.append(at)
        body = bytearray(struct.pack("<H", len(placed)))
        for code, kind, count, low, _high in placed:
            body += struct.pack("<HHII", code, kind, count, low)
        body += b"\x00" * 8
        for *_rest, high in placed:
            body += struct.pack("<I", high)
        out.extend(body)
    # chain the IFDs (the next-offset field is the last 8 bytes of each)
    struct.pack_into("<Q", out, 4, ifd_positions[0])
    for here, following in zip(ifd_positions, ifd_positions[1:] + [0]):
        tagno = struct.unpack_from("<H", out, here)[0]
        struct.pack_into("<Q", out, here + 2 + tagno * 12, following)
    Path(path).write_bytes(bytes(out))
    return Path(path)


def write_ndpis(path, members, *, count=None, newline="\r\n") -> Path:
    """The text file: `members` is the list of `.ndpi` file names."""
    path = Path(path)
    lines = ["[NanoZoomer Digital Pathology Image Set]",
             f"NoImages={len(members) if count is None else count}"]
    lines += [f"Image{i}={name}" for i, name in enumerate(members)]
    path.write_bytes((newline.join(lines) + newline).encode("utf-8"))
    return path
