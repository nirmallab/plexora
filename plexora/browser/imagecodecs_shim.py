"""`imagecodecs` for the website, where the real package has no build.

imagecodecs is C (libjpeg-turbo, zlib-ng, liblzf, OpenJPEG and some forty
others) with no WebAssembly wheel, and porting it is weeks. What Plexora's
Python asks of it in the browser is narrow: tifffile's TIFF compressions,
`fast_png`'s deflate, `segmentation_pyramid`'s PNG, and Imaris' LZ4. So this
module stands in for it (`install` puts it in `sys.modules`), built from what
the website's Python already has:

- tifffile's own fallbacks (deflate, LZMA, zstd, PackBits, delta, bit order,
  packed integers, float24), re-exported as they are;
- `zlibng_encode` from the stdlib's zlib -- the same stream format, slower;
- LZW from plexora-kernels (compiled C, WebAssembly on the website) when it
  is there, else decoded here in Python (`lzw_decode_python`);
- JPEG, PNG and WebP through Pillow, with a TIFF's JPEG tables and an RGB
  JPEG's colour space handled as libjpeg-turbo would (`jpeg_decode`);
- HDF5's LZ4 framing (Imaris) through the `lz4` package;
- JPEG 2000 through a decoder the page provides (OpenJPEG in WebAssembly,
  `set_jpeg2k`), or a NotImplementedError saying so;
- the codec names wsidicom imports for a DICOM slide's frames (`JPEG8`,
  `JPEG2K`, ... with `.available`), so it decodes them here too.

A codec missing here raises NotImplementedError, which tifffile reports as
"cannot decode", the same as a missing codec on the desktop. Every function is
held to the real imagecodecs under CPython (tests/test_imagecodecs_shim.py).
"""

from __future__ import annotations

import io
import struct
import sys
import types
import zlib

import numpy as np

__version__ = "0+plexora-browser"

#: The tifffile fallbacks this module re-exports under imagecodecs' names.
_FALLBACKS = ("bitorder_decode", "delta_decode", "delta_encode", "float24_decode",
              "lzma_decode", "lzma_encode", "packbits_decode", "packints_decode",
              "packints_encode", "zlib_decode", "zlib_encode")

_jpeg2k = None


def set_jpeg2k(decode) -> None:
    """`decode(data: bytes) -> numpy array`: the page's JPEG 2000 decoder."""
    global _jpeg2k
    _jpeg2k = decode


def zlibng_encode(data, level=6, out=None):
    return zlib.compress(bytes(data), level)


def deflate_decode(data, out=None):
    return zlib.decompress(bytes(data))


def _zstd():
    """(decompress, compress) from whichever zstd this Python has: the
    stdlib's (3.14), the `zstandard` package, or numcodecs' (the website's
    numcodecs has it compiled in)."""
    try:
        from compression import zstd

        return zstd.decompress, lambda data, level: zstd.compress(data, level)
    except ImportError:
        pass
    try:
        import zstandard

        return (lambda data: zstandard.ZstdDecompressor().decompressobj().decompress(data),
                lambda data, level: zstandard.ZstdCompressor(level=level).compress(data))
    except ImportError:
        pass
    from numcodecs import Zstd

    return (lambda data: bytes(Zstd().decode(data)),
            lambda data, level: bytes(Zstd(level=level).encode(data)))


def zstd_decode(data, out=None):
    return _zstd()[0](bytes(data))


def zstd_encode(data, level=None, out=None):
    return _zstd()[1](bytes(data), 3 if level is None else level)


# -- LZW (TIFF 6: MSB-first codes, early change) ----------------------------

_CLEAR, _EOI = 256, 257


def _lzw():
    """The LZW decoder this process has: plexora-kernels' (a port of the one
    below, held to it and to imagecodecs by tests/test_imagecodecs_shim.py),
    else the one below."""
    from plexora.server.utils import jit

    package = jit.kernels_module()
    return package.lzw_decode if package is not None else lzw_decode_python


def lzw_decode(data, buffersize=0, out=None):
    """TIFF's LZW. The pre-TIFF 6 variant (LSB-first) is refused, as by
    libtiff without its old-LZW support."""
    return _lzw()(data, buffersize, out)


def lzw_decode_python(data, buffersize=0, out=None):
    """`lzw_decode` in Python: what runs without plexora-kernels."""
    data = bytes(data)
    if data[:2] == b"\x00\x01":
        raise NotImplementedError("old-style (pre-TIFF 6) LZW is not supported")
    padded = data + b"\x00\x00\x00"
    total = len(data) * 8
    table = [bytes((value,)) for value in range(256)] + [b"", b""]
    result = bytearray()
    width, position, previous = 9, 0, None
    while position + width <= total:
        at = position >> 3
        word = (padded[at] << 16) | (padded[at + 1] << 8) | padded[at + 2]
        code = (word >> (24 - (position & 7) - width)) & ((1 << width) - 1)
        position += width
        if code == _EOI:
            break
        if code == _CLEAR:
            del table[258:]
            width, previous = 9, None
            continue
        if previous is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(previous + entry[:1])
        else:
            entry = previous + previous[:1]
            table.append(entry)
        result += entry
        previous = entry
        if len(table) >= (1 << width) - 1 and width < 12:
            width += 1
    return bytes(result)


# -- JPEG, PNG, WebP through Pillow ------------------------------------------

#: An Adobe APP14 marker with transform 0: "these components are RGB, not
#: YCbCr". libjpeg guesses YCbCr for a 3-component JPEG with neither a JFIF nor
#: an Adobe marker, which is what an Aperio SVS writes for RGB tiles.
_ADOBE_RGB = b"\xff\xee\x00\x0eAdobe\x00\x64\x00\x00\x00\x00\x00"


def _merge_tables(tables, data):
    """A TIFF JPEG tile with its JPEGTables: the tables' markers (without
    their EOI) followed by the tile's (without its SOI)."""
    if not tables:
        return data
    tables = bytes(tables)
    if tables.endswith(b"\xff\xd9"):
        tables = tables[:-2]
    if data[:2] == b"\xff\xd8":
        data = data[2:]
    return tables + data


def jpeg_decode(data, bitspersample=None, tables=None, header=None, colorspace=None,
                outcolorspace=None, shape=None, out=None, **kwargs):
    from PIL import Image

    if bitspersample not in (None, 8):
        raise NotImplementedError(f"{bitspersample}-bit JPEG is not supported in the browser")
    stream = _merge_tables(tables, bytes(header or b"") + bytes(data))
    if isinstance(colorspace, str):
        # wsidicom names it ("RGB" for a DICOM frame whose metadata says RGB).
        colorspace = {"RGB": 2, "YCBCR": 3, "GRAYSCALE": 1}.get(colorspace.upper(), colorspace)
    if colorspace == 2 and _components(stream) == 3 and b"Adobe" not in stream[:512]:
        stream = stream[:2] + _ADOBE_RGB + stream[2:]
    image = Image.open(io.BytesIO(stream))
    image.load()
    if outcolorspace in (1, "MINISBLACK") and image.mode not in ("L", "I;16"):
        image = image.convert("L")
    array = np.asarray(image)
    if shape is not None and tuple(array.shape[:2]) != tuple(shape[:2]):
        # A strip at the image's edge is padded in the stream; keep the rows asked for.
        array = array[:shape[0], :shape[1]]
    return array


def _components(stream):
    """The component count from the first SOF marker, or None."""
    at = 2
    while at + 4 <= len(stream):
        if stream[at] != 0xFF:
            return None
        marker = stream[at + 1]
        length = struct.unpack(">H", stream[at + 2:at + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return stream[at + 9] if at + 9 < len(stream) else None
        at += 2 + length
    return None


jpeg8_decode = jpeg_decode


def png_decode(data, out=None, **kwargs):
    """As imagecodecs decodes: a palette image's colours (RGB, or RGBA when
    it has transparency), 16-bit samples as uint16. A caller that wants a
    palette's indices reads the PNG with Pillow (segmentation_pyramid does)."""
    from PIL import Image

    image = Image.open(io.BytesIO(bytes(data)))
    image.load()
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    return np.asarray(image)


def webp_decode(data, hasalpha=None, out=None, **kwargs):
    from PIL import Image

    image = Image.open(io.BytesIO(bytes(data)))
    image.load()
    if hasalpha and image.mode != "RGBA":
        image = image.convert("RGBA")
    return np.asarray(image)


def jpeg2k_decode(data, out=None, **kwargs):
    if _jpeg2k is None:
        raise NotImplementedError("JPEG 2000 decoding in the browser needs the page's "
                                  "OpenJPEG decoder, which is not loaded")
    return np.asarray(_jpeg2k(bytes(data)))


# -- HDF5's LZ4 framing (Imaris) ---------------------------------------------


class _Available:
    def __init__(self, test):
        self._test = test

    @property
    def available(self):
        return self._test()


def _have_lz4():
    try:
        import lz4.block  # noqa: F401
    except ImportError:
        return False
    return True


LZ4H5 = _Available(_have_lz4)


def _have_zstd():
    try:
        _zstd()
    except ImportError:
        return False
    return True


#: tifffile uses this module's zstd only when it says it has one.
ZSTD = _Available(_have_zstd)

#: wsidicom imports these together with their functions (wsidicom/codec/
#: optionals.py) and takes imagecodecs for DICOM frames only when every name
#: imports; each codec then says whether it is really here. JPEG is Pillow's
#: (`jpeg_decode`), JPEG 2000 the page's OpenJPEG once it is set; JPEG-LS and
#: JPEG XL are not in the browser, and a frame in either is refused as on a
#: desktop without them.
JPEG8 = _Available(lambda: True)
#: libjpeg's colour spaces, as imagecodecs names them: wsidicom builds an
#: encoder (never used to read) to decide a transfer syntax is supported.
JPEG8.CS = types.SimpleNamespace(UNKNOWN=0, GRAYSCALE=1, RGB=2, YCbCr=3, CMYK=4, YCCK=5)
JPEG2K = _Available(lambda: _jpeg2k is not None)
JPEGLS = _Available(lambda: False)
JPEGXL = _Available(lambda: False)


def _absent(name):
    def refuse(*args, **kwargs):
        raise NotImplementedError(f"{name} is not available in the browser")

    refuse.__name__ = name
    return refuse


jpegls_decode = _absent("jpegls_decode")
jpegxl_decode = _absent("jpegxl_decode")
dicomrle_decode = _absent("dicomrle_decode")
#: Encoders Plexora never calls in the browser (wsidicom imports them).
jpeg8_encode = _absent("jpeg8_encode")
jpeg2k_encode = _absent("jpeg2k_encode")
jpegls_encode = _absent("jpegls_encode")
jpegxl_encode = _absent("jpegxl_encode")
packbits_encode = _absent("packbits_encode")


def lz4h5_decode(data, out=None):
    """HDF5's LZ4 filter framing: the total size (8 bytes) and block size (4),
    then blocks of [compressed size (4), bytes]; a block whose compressed size
    equals its size is stored raw. Big-endian throughout."""
    import lz4.block

    data = memoryview(bytes(data))
    total, block = struct.unpack(">QI", data[:12])
    at, result = 12, bytearray()
    while len(result) < total:
        size = struct.unpack(">I", data[at:at + 4])[0]
        at += 4
        want = min(block, total - len(result))
        piece = data[at:at + size]
        at += size
        result += piece if size == want else lz4.block.decompress(
            bytes(piece), uncompressed_size=want)
    return bytes(result)


def install() -> types.ModuleType:
    """Make this module `imagecodecs` for every later import. It must run
    before anything imports tifffile, which binds its codec module once, at
    import: so the module is registered first, and tifffile's fallbacks are
    read from it after (importing them is what imports tifffile)."""
    module = sys.modules[__name__]
    sys.modules["imagecodecs"] = module
    from tifffile import _imagecodecs as fallbacks

    for name in _FALLBACKS:
        setattr(module, name, getattr(fallbacks, name))
    # tifffile takes a codec only from a module named imagecodecs or tifffile
    # (a guard against a substituted codec); this one is the substitute.
    for value in vars(module).values():
        if callable(value) and getattr(value, "__module__", None) == __name__:
            value.__module__ = "imagecodecs"
    return module
