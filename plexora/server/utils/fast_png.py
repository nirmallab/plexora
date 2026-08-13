"""Fast, lossless PNG encoding using a libdeflate-backed compressor instead
of PIL's stdlib-zlib encoder.

PIL's PNG encoder is stdlib-zlib-backed, which is markedly slower than a
libdeflate-backed compressor for the same compression ratio on this kind of
data (benchmarked on 1024x1024 tiles: PIL compress_level=9 takes ~207ms for
~1.17MB of 16-bit grayscale channel data that imagecodecs' zlib-ng backed
compressor gets in ~25ms; on RGBA segmentation-mask tiles, PIL
compress_level=0 -- current production -- takes ~20ms for ~4.1MB where this
module gets ~56KB in ~13ms). PIL has no way to select a different
compression backend, so this module hand-assembles minimal, spec-valid PNGs
instead of going through PIL at all.

Used for:
  - the "HD" full-precision channel-tile quality mode (encode_gray16_png)
  - segmentation/label tiles (encode_rgba8_png) -- safe to use PNG's own
    fast path here despite segmentation's alpha=0-everywhere data (unlike
    WebP, PNG decode never goes through a premultiplied-alpha compositing
    step; the frontend parses PNG bytes directly via UPNG.js, not a canvas)
"""
import struct
import zlib

import imagecodecs
import numpy as np

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ZLIBNG_LEVEL = 6


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data))
    )


def _encode_png(width: int, height: int, bit_depth: int, color_type: int, row_bytes: np.ndarray) -> bytes:
    """row_bytes: 2D uint8 array, one PNG scanline (without a filter byte) per row."""
    stride = row_bytes.shape[1]
    filtered = np.empty((height, stride + 1), dtype="u1")
    filtered[:, 0] = 0  # filter type 0 ("None") on every scanline
    filtered[:, 1:] = row_bytes

    idat_data = imagecodecs.zlibng_encode(filtered.tobytes(), level=_ZLIBNG_LEVEL)
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)

    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat_data)
        + _chunk(b"IEND", b"")
    )


def encode_gray16_png(tile: np.ndarray) -> bytes:
    """Encode a 2D uint16 array as a minimal 16-bit grayscale PNG (IHDR
    bit depth 16, color type 0), byte-for-byte lossless.
    """
    if tile.ndim != 2:
        raise ValueError(f"expected a 2D array, got shape {tile.shape}")
    if tile.dtype != np.uint16:
        tile = tile.astype(np.uint16)

    height, width = tile.shape
    # PNG samples are big-endian regardless of host byte order.
    be_tile = np.ascontiguousarray(tile).astype(">u2")
    stride = width * 2
    row_bytes = np.frombuffer(be_tile.tobytes(), dtype="u1").reshape(height, stride)

    return _encode_png(width, height, bit_depth=16, color_type=0, row_bytes=row_bytes)


def encode_rgba8_png(tile: np.ndarray) -> bytes:
    """Encode a (H, W, 4) uint8 array as a minimal 8-bit RGBA PNG (IHDR bit
    depth 8, color type 6), byte-for-byte lossless. Used for segmentation
    tiles (packed uint32 label IDs, alpha channel unused/zero).
    """
    if tile.ndim != 3 or tile.shape[2] != 4:
        raise ValueError(f"expected an (H, W, 4) array, got shape {tile.shape}")
    if tile.dtype != np.uint8:
        tile = tile.astype(np.uint8)

    height, width, _ = tile.shape
    row_bytes = np.ascontiguousarray(tile).reshape(height, width * 4)

    return _encode_png(width, height, bit_depth=8, color_type=6, row_bytes=row_bytes)
