"""Hand-written slide files for the first view's tests: an Aperio-like SVS
and an Akoya-like QPTIFF, small, with the parts of the real formats the
preview has to get right.

- `write_svs_like`: a tiled baseline, a thumbnail of the same shape in strips,
  a label of another shape smaller than the thumbnail, and a macro. With
  `jpeg=True` every tile and strip is an abbreviated JPEG stream with the
  tables kept once in JPEGTables (tag 347), in RGB with no colour marker --
  Aperio's own layout, which a decoder reads as YCbCr unless told otherwise.
- `write_qptiff_like`: one full-resolution page per channel, each naming its
  biomarker in PerkinElmer-QPI XML, the same at a reduced resolution, and an
  RGB thumbnail, with tifffile's `Software` mark so it reads it as a QPTIFF.
"""

from __future__ import annotations

import io

import numpy as np
import tifffile

APERIO = ("Aperio Image Library v12.0.15 \n{w}x{h} [0,0 {w}x{h}] (240x240) JPEG/RGB Q=95"
          "|AppMag = 20|MPP = 0.499")


def slide_picture(height, width, seed=0):
    """Smooth RGB content (JPEG keeps it closely): gradients and a few discs."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width]
    out = np.empty((height, width, 3), np.float32)
    out[..., 0] = 230 - 120 * y / height
    out[..., 1] = 90 + 100 * x / width
    out[..., 2] = 200 - 60 * (x + y) / (width + height)
    for _ in range(4):
        cy, cx, r = rng.uniform(0, height), rng.uniform(0, width), rng.uniform(width / 12, width / 5)
        disc = (y - cy) ** 2 + (x - cx) ** 2 < r * r
        out[disc] = (out[disc] * 0.4 + np.array([120, 40, 150]) * 0.6)
    return np.clip(out, 0, 255).astype(np.uint8)


def _segments(stream):
    """A JPEG stream's marker segments up to the scan, and the scan with its EOI."""
    out, at = [], 2
    while at < len(stream):
        marker = stream[at:at + 2]
        if marker == b"\xff\xda":
            return out, stream[at:]
        length = int.from_bytes(stream[at + 2:at + 4], "big")
        out.append(stream[at:at + 2 + length])
        at += 2 + length
    raise ValueError("no scan")


def _jpeg(block):
    """`block` (RGB, uint8) as an RGB JPEG without a colour marker: (tables, abbreviated)."""
    buffer = io.BytesIO()
    from PIL import Image

    Image.fromarray(block).save(buffer, "JPEG", quality=95, keep_rgb=True, subsampling=0)
    segments, scan = _segments(buffer.getvalue())
    tables = [s for s in segments if s[1] in (0xDB, 0xC4)]           # DQT, DHT
    frame = [_aperio_ids(s) for s in segments if s[1] not in (0xDB, 0xC4) and not 0xE0 <= s[1] <= 0xEF]
    return (b"\xff\xd8" + b"".join(tables) + b"\xff\xd9",
            b"\xff\xd8" + b"".join(frame) + _aperio_ids(scan))


#: Pillow names an RGB JPEG's components "R", "G", "B", which tells a decoder
#: they are RGB; Aperio numbers them 0, 1, 2 -- which says nothing, so a
#: decoder takes YCbCr.
_APERIO_IDS = {0x52: 0, 0x47: 1, 0x42: 2}


def _aperio_ids(segment):
    """A frame (SOF) or scan (SOS) header with Aperio's component ids."""
    out = bytearray(segment)
    if out[1] in (0xC0, 0xC1, 0xC2):
        for i in range(out[9]):
            at = 10 + 3 * i
            out[at] = _APERIO_IDS.get(out[at], out[at])
    elif out[1] == 0xDA:
        for i in range(out[4]):
            at = 5 + 2 * i
            out[at] = _APERIO_IDS.get(out[at], out[at])
    return bytes(out)


def _write_jpeg(tif, picture, tables_out, *, tile=None, rowsperstrip=None, description=None, subfiletype=0):
    height, width = picture.shape[:2]
    blocks = []
    if tile:
        for ty in range(0, height, tile):
            for tx in range(0, width, tile):
                block = np.zeros((tile, tile, 3), np.uint8)
                part = picture[ty:ty + tile, tx:tx + tile]
                block[:part.shape[0], :part.shape[1]] = part
                tables, body = _jpeg(block)
                blocks.append(body)
    else:
        for y0 in range(0, height, rowsperstrip):
            tables, body = _jpeg(np.ascontiguousarray(picture[y0:y0 + rowsperstrip]))
            blocks.append(body)
    tables_out.append(tables)
    options = {"tile": (tile, tile)} if tile else {"rowsperstrip": rowsperstrip}
    tif.write(iter(blocks), shape=picture.shape, dtype="uint8", compression="jpeg", photometric="rgb",
              description=description, subfiletype=subfiletype, metadata=None,
              extratags=[(347, 7, len(tables), tables, True)], **options)


def write_svs_like(path, *, jpeg=True, height=720, width=480, seed=0):
    """An Aperio-like slide; returns {baseline, thumbnail, label, macro} pictures."""
    pictures = {
        "baseline": slide_picture(height, width, seed),
        "thumbnail": slide_picture(height // 4, width // 4, seed),
        "label": slide_picture(50, 60, seed + 1),        # another shape, smaller than the thumbnail
        "macro": slide_picture(40, 120, seed + 2),
    }
    tables = []
    with tifffile.TiffWriter(path) as tif:
        description = APERIO.format(w=width, h=height)
        if jpeg:
            _write_jpeg(tif, pictures["baseline"], tables, tile=240, description=description)
            _write_jpeg(tif, pictures["thumbnail"], tables, rowsperstrip=16)
        else:
            tif.write(pictures["baseline"], tile=(240, 240), compression="lzw", photometric="rgb",
                      description=description, metadata=None)
            tif.write(pictures["thumbnail"], rowsperstrip=16, compression="lzw", photometric="rgb", metadata=None)
        tif.write(pictures["label"], compression="lzw", photometric="rgb", subfiletype=1, metadata=None)
        tif.write(pictures["macro"], compression="lzw", photometric="rgb", subfiletype=9, metadata=None)
    if jpeg:
        # tifffile writes a JPEG-compressed RGB image as YCbCr; Aperio's say RGB.
        with tifffile.TiffFile(path, mode="r+") as tif:
            for index in (0, 1):
                tif.pages[index].tags["PhotometricInterpretation"].overwrite(2)
    return pictures


def _qpi(kind, name):
    return ('<?xml version="1.0" encoding="utf-8"?><PerkinElmer-QPI-ImageDescription>'
            f"<DescriptionVersion>2</DescriptionVersion><ImageType>{kind}</ImageType>"
            f"<Name>{name}</Name><Biomarker>{name}</Biomarker></PerkinElmer-QPI-ImageDescription>")


def write_qptiff_like(path, names=("DAPI", "CD8", "PanCK"), size=256, seed=0):
    """An Akoya-like QPTIFF; returns the channel stack (C, Y, X) and its reduced level."""
    rng = np.random.default_rng(seed)
    stack = rng.integers(0, 4000, size=(len(names), size, size), dtype=np.uint16)
    reduced = stack[:, ::4, ::4].copy()
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        for channel, name in enumerate(names):
            tif.write(stack[channel], tile=(64, 64), description=_qpi("FullResolution", name),
                      software="PerkinElmer-QPI", metadata=None)
        tif.write(slide_picture(32, 32, seed), photometric="rgb", description=_qpi("Thumbnail", "thumb"),
                  software="PerkinElmer-QPI", subfiletype=1, metadata=None)
        for channel, name in enumerate(names):
            tif.write(reduced[channel], tile=(64, 64), description=_qpi("ReducedResolution", name),
                      software="PerkinElmer-QPI", subfiletype=1, metadata=None)
    return stack, reduced


def _full_jpeg(picture):
    """A whole JFIF stream (YCbCr, its own tables): what an NDPI strip is."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(picture).save(buffer, "JPEG", quality=95, subsampling=0)
    return buffer.getvalue()


def write_ndpi_like(path, *, beyond=0, jpeg=True, height=384, width=512, seed=0):
    """A Hamamatsu-like NDPI, written by hand: classic little-endian TIFF with
    NDPI's 64-bit offsets (the first directory's 8 bytes at 4; each
    directory's 12-byte entries, its next directory's 8-byte offset, then each
    entry's high 32 bits). Three images, one strip each: level 0, a reduced
    level, and a macro of another shape. `beyond` bytes of hole (a sparse
    file) before the reduced level put it and its directory past 4 GB, as in
    a large slide. Returns the pictures by name."""
    import struct

    pictures = {
        "level0": slide_picture(height, width, seed),
        "level1": slide_picture(height // 4, width // 4, seed),
        "macro": slide_picture(40, 120, seed + 2),
    }
    magnification = {"level0": 20.0, "level1": 5.0, "macro": -1.0}

    def entry(tag, kind, count, value):
        return (tag, kind, count, value)

    ifds = []
    with open(path, "wb") as out:
        out.write(b"II" + struct.pack("<H", 42) + struct.pack("<Q", 0))  # first offset patched below
        for name in ("level0", "macro", "level1"):
            picture = pictures[name]
            if name == "level1" and beyond:
                out.seek(beyond, 1)
            data = _full_jpeg(picture) if jpeg else picture.tobytes()
            data_at = out.tell()
            out.write(data)
            bits_at = out.tell()
            out.write(struct.pack("<3H", 8, 8, 8))
            make_at = out.tell()
            out.write(b"Hamamatsu\0")                      # NDPI keeps strings out of line
            h, w = picture.shape[:2]
            tags = [
                entry(256, 4, 1, w), entry(257, 4, 1, h), entry(258, 3, 3, bits_at),
                entry(259, 3, 1, 7 if jpeg else 1), entry(262, 3, 1, 6 if jpeg else 2),
                entry(271, 2, 10, make_at),
                entry(273, 4, 1, data_at), entry(277, 3, 1, 3), entry(278, 4, 1, h),
                entry(279, 4, 1, len(data)), entry(65420, 4, 1, 1),
                entry(65421, 11, 1, struct.unpack("<I", struct.pack("<f", magnification[name]))[0]),
            ]
            ifd_at = out.tell()
            body = struct.pack("<H", len(tags))
            highs = b""
            for tag, kind, count, value in tags:
                if kind == 3 and count == 1:
                    field = struct.pack("<HH", value, 0)
                    high = 0
                else:
                    field = struct.pack("<I", value & 0xFFFFFFFF)
                    high = value >> 32
                body += struct.pack("<HHI", tag, kind, count) + field
                highs += struct.pack("<I", high)
            next_at = ifd_at + len(body)
            out.write(body + struct.pack("<Q", 0) + highs)
            ifds.append((ifd_at, next_at))
        out.seek(4)
        out.write(struct.pack("<Q", ifds[0][0]))
        for (_, next_at), (following, _) in zip(ifds, ifds[1:]):
            out.seek(next_at)
            out.write(struct.pack("<Q", following))
    return pictures
