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
- JPEG XR (czifile's Axioscan tiles, tifffile's JPEG XR) the same way
  (jxrlib in WebAssembly, `set_jpegxr`);
- CZI's Zstandard and ZSTD1 (`zstd1_decode`: hi/lo byte packing, BGR) from
  the same zstd as `zstd_decode`;
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
_jpegxr = None


def set_jpeg2k(decode) -> None:
    """`decode(data: bytes) -> numpy array`: the page's JPEG 2000 decoder."""
    global _jpeg2k
    _jpeg2k = decode


def set_jpegxr(decode) -> None:
    """`decode(data: bytes) -> dict`: the page's JPEG XR decoder (jxrlib),
    which answers with the pixels as jxrlib writes them -- `data`, rows
    packed in the stream's own pixel format -- and that format: `width`,
    `height`, `channels`, `colorformat` and `bitdepth` (jxrlib's names: "RGB",
    "YOnly"; "8", "16", "16Float", ...), `bitsperpixel`, `alpha` and `bgr`.
    `jpegxr_decode` makes the array imagecodecs would."""
    global _jpegxr
    _jpegxr = decode


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


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def zstd1_decode(data, *, itemsize=1, samples=1, out=None):
    """A CZI's Zstandard subblock (compression 5 and 6), as imagecodecs
    decodes it: a plain Zstandard frame, or ZSTD1 -- a header before the
    frame (its size in the first byte, then typed chunks; type 1's low bit
    says the bytes were packed "hi/lo": every item's first byte, then every
    second, ...), undone here for `itemsize` > 1. Then, for `samples` 3 or 4,
    B and R swapped and an 8-bit BGRA's alpha made 255. An int `out` is the
    most the frame may hold, as in imagecodecs; another `out` is ignored, as
    elsewhere in this module."""
    data = bytes(data)
    if len(data) < 2:
        raise ValueError(f"srcsize={len(data)} < 2")
    if itemsize < 1:
        raise ValueError(f"itemsize={itemsize} < 1")
    if samples not in (1, 3, 4):
        raise ValueError(f"samples={samples} not in {{1, 3, 4}}")
    start, hilo = 0, False
    if data[:4] != _ZSTD_MAGIC:
        start = data[0]
        if start < 1 or start >= len(data):
            raise ValueError(f"header_size={start} out of range for srcsize={len(data)}")
        at = 1
        while at < start:
            kind = data[at]
            at += 1
            if kind != 1:
                break  # a chunk type it does not know ends the header, as in imagecodecs
            if at >= start:
                raise ValueError("truncated chunk type 1")
            hilo = bool(data[at] & 1)
            at += 1
    result = _zstd()[0](data[start:])
    if isinstance(out, int) and out >= 0 and len(result) > out:
        raise RuntimeError("ZSTD_decompress returned 'Destination buffer is too small'")
    if hilo and itemsize > 1:
        if len(result) % itemsize:
            raise ValueError("decompressed size not a multiple of itemsize")
        result = np.frombuffer(result, np.uint8).reshape(itemsize, -1).T.tobytes()
    if samples in (3, 4):
        if len(result) % (samples * itemsize):
            raise ValueError("decompressed size not a multiple of samples*itemsize")
        pixels = np.frombuffer(result, np.uint8).reshape(-1, samples, itemsize).copy()
        pixels[:, [0, 2]] = pixels[:, [2, 0]]
        if samples == 4 and itemsize == 1:
            pixels[:, 3] = 255
        result = pixels.tobytes()
    return result


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
    if isinstance(outcolorspace, str):
        outcolorspace = outcolorspace.upper()
    if outcolorspace in (3, "YCBCR") and image.mode == "RGB":
        # The components as stored, not converted to colour, as imagecodecs
        # gives them: a fluorescence MRXS keeps a filter in each
        # (server/utils/formats/mrxs.py).
        image.draft("YCbCr", None)
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


#: jxrlib's sample depths that are a numpy type as they are. Fixed-point,
#: packed (RGB555, RGB565, RGB101010) and 1-bit samples, which imagecodecs
#: converts, are refused.
_JXR_DTYPES = {"8": np.uint8, "16": np.uint16, "16Float": np.float16, "32Float": np.float32}


def jpegxr_decode(data, index=None, fp2int=False, out=None, **kwargs):
    """As imagecodecs decodes JPEG XR: grey as (height, width), colour as
    (height, width, 3) in R, G, B order -- a BGR stream (a CZI's 24-bit
    colour) is turned round, as imagecodecs' format converter turns it.

    Only what has been held to imagecodecs is decoded: grey and RGB/BGR in
    8 or 16-bit integers or 16 or 32-bit floats. The rest is refused: alpha
    (the page's build never decodes an alpha plane), padded pixels
    (32bppBGR, 128bppRGBFloat, ...), CMYK and n-channel streams."""
    if _jpegxr is None:
        raise NotImplementedError("JPEG XR decoding in the browser needs the page's "
                                  "jxrlib decoder, which is not loaded")
    found = _jpegxr(bytes(data))
    depth, colour = str(found["bitdepth"]), str(found["colorformat"])
    channels, bits = int(found["channels"]), int(found["bitsperpixel"])
    dtype = _JXR_DTYPES.get(depth)
    if (dtype is None or (colour, channels) not in (("YOnly", 1), ("RGB", 3)) or found["alpha"]
            or bits != channels * 8 * np.dtype(dtype).itemsize):
        alpha = " with alpha" if found["alpha"] else ""
        raise NotImplementedError(f"JPEG XR in {bits}-bit {colour}{alpha} ({depth}-bit samples) "
                                  "is not supported in the browser")
    image = np.frombuffer(found["data"], dtype=dtype)
    image = image.reshape(int(found["height"]), int(found["width"]), *((3,) if channels == 3 else ()))
    if found["bgr"]:
        image = image[..., ::-1]
    if not (image.flags.writeable and image.flags.c_contiguous):
        image = image.copy()
    return image


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
ZSTD1 = _Available(_have_zstd)

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
JPEGXR = _Available(lambda: _jpegxr is not None)
#: Windows Imaging Component, which czifile asks for before `jpegxr_decode`
#: (it prefers it on Windows) when it reads a JPEG XR CZI's directory.
WIC = _Available(lambda: False)


def _absent(name):
    def refuse(*args, **kwargs):
        raise NotImplementedError(f"{name} is not available in the browser")

    refuse.__name__ = name
    return refuse


jpegls_decode = _absent("jpegls_decode")
jpegxl_decode = _absent("jpegxl_decode")
dicomrle_decode = _absent("dicomrle_decode")
#: CZI's experimental "chunked" compression (type 7): czifile asks for it when
#: it reads such a file's directory, so the file opens and its tiles say so.
chunked_decode = _absent("chunked_decode")
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
