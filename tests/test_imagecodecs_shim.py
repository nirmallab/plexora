"""The website's stand-in for imagecodecs, held to the real one.

imagecodecs has no WebAssembly build, so the website's Python decodes TIFF
tiles through plexora/browser/imagecodecs_shim.py. Each codec here is checked
against what imagecodecs itself produces, and tifffile is checked to read
files written by the real codecs when the shim is all it has.
"""

import json
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
import tifffile

imagecodecs = pytest.importorskip("imagecodecs")
from plexora.browser import imagecodecs_shim as shim  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("data", [
    b"",
    b"a",
    bytes(range(256)) * 40,
    np.random.default_rng(0).integers(0, 256, 300_000, dtype=np.uint8).tobytes(),
    np.repeat(np.arange(60_000, dtype=np.uint16), 3).tobytes(),
], ids=["empty", "one", "ramp", "noise", "runs"])
def test_lzw_decodes_what_imagecodecs_encodes(data):
    """`noise` overflows the 4096-code table, so the Clear code is exercised.
    Both decoders: the Python one, and plexora-kernels' port of it (what
    `lzw_decode` runs when that package is installed)."""
    encoded = imagecodecs.lzw_encode(data)
    assert shim.lzw_decode_python(encoded) == data
    assert shim.lzw_decode(encoded) == data
    try:
        import plexora_kernels
    except ImportError:
        return
    assert plexora_kernels.lzw_decode(encoded) == data


def test_zlibng_is_a_deflate_stream():
    data = np.arange(10_000, dtype=np.uint16).tobytes()
    assert zlib.decompress(shim.zlibng_encode(data)) == data


def test_png_decodes_a_palette_and_sixteen_bits_as_imagecodecs_does():
    from PIL import Image
    import io

    indices = np.random.default_rng(1).integers(0, 7, (40, 30), dtype=np.uint8)
    image = Image.fromarray(indices, mode="P")
    image.putpalette([value for i in range(7) for value in (i * 30, 255 - i * 30, 7)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert np.array_equal(shim.png_decode(buffer.getvalue()),
                          imagecodecs.png_decode(buffer.getvalue()))
    deep = np.random.default_rng(2).integers(0, 65535, (20, 25), dtype=np.uint16)
    encoded = imagecodecs.png_encode(deep)
    assert np.array_equal(shim.png_decode(encoded), deep)


def test_lz4h5_decodes_what_imagecodecs_encodes():
    pytest.importorskip("lz4")
    data = np.repeat(np.arange(50_000, dtype=np.uint16), 4).tobytes()
    assert shim.lz4h5_decode(imagecodecs.lz4h5_encode(data)) == data
    assert shim.LZ4H5.available


def test_jpeg_2000_without_the_pages_decoder_says_so():
    with pytest.raises(NotImplementedError, match="OpenJPEG"):
        shim.jpeg2k_decode(b"\x00")


def test_jpeg_xr_without_the_pages_decoder_says_so():
    with pytest.raises(NotImplementedError, match="jxrlib"):
        shim.jpegxr_decode(b"\x00")
    assert not shim.JPEGXR.available and not shim.WIC.available


def test_czi_chunked_compression_says_so():
    """czifile asks for it as it reads such a CZI's directory: the file opens
    and its tiles say so."""
    with pytest.raises(NotImplementedError, match="chunked_decode"):
        shim.chunked_decode(b"\x00\x00")


# -- JPEG XR through the worker's jxrlib (tests/js/jpegxr_probe.mjs) -----------

def _jpegxr_cases():
    rng = np.random.default_rng(9)
    smooth = (np.add.outer(np.arange(91), np.arange(133)) * 300).astype(np.uint16)
    noisy = (smooth + rng.integers(0, 400, smooth.shape)).astype(np.uint16)
    gray = np.clip(smooth // 120, 0, 255).astype(np.uint8)
    rgb = rng.integers(0, 255, (77, 101, 3), dtype=np.uint8)
    return {
        "gray8.jxr": (gray, {"level": 1.0}),
        "gray8_lossy.jxr": (gray, {"level": 0.8}),
        "gray16.jxr": (noisy, {"level": 1.0}),
        "gray16_lossy.jxr": (noisy, {"level": 0.85}),
        "rgb8.jxr": (rgb, {"level": 1.0}),
        "rgb16_lossy.jxr": (rgb.astype(np.uint16) * 257, {"level": 0.9}),
        "float16.jxr": ((noisy / 65535).astype(np.float16), {"level": 1.0}),
        "float32.jxr": ((noisy / 65535).astype(np.float32), {"level": 1.0}),
        "one_pixel.jxr": (np.array([[7]], np.uint8), {"level": 1.0}),
        "rgba8.jxr": (np.dstack([rgb, rgb[..., :1]]), {"level": 1.0, "hasalpha": True}),
    }


def test_jpeg_xr_through_the_workers_jxrlib_is_what_imagecodecs_decodes(tmp_path):
    """The website's JPEG XR: the worker's jxrlib (client/src/js/workers/
    jpegxr.js, run in node with eval off, as the site's CSP has it) and the
    shim's `jpegxr_decode` on its answer -- the dict pythonWorker.js's `_jxr`
    makes -- pixel for pixel imagecodecs' decode of the same bytes. Alpha is
    refused: the build never decodes an alpha plane. (A CZI's BGR tiles were
    held to imagecodecs the same way on real Axioscan scans, which the repo
    does not carry; the encoder here writes RGB.)"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    files = {}
    for name, (data, options) in _jpegxr_cases().items():
        files[name] = tmp_path / name
        files[name].write_bytes(imagecodecs.jpegxr_encode(data, **options))
    done = subprocess.run([node, "--disallow-code-generation-from-strings",
                           str(ROOT / "tests" / "js" / "jpegxr_probe.mjs"), *map(str, files.values())],
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    lines = [json.loads(line) for line in done.stdout.strip().splitlines()]
    # Fetched once, on the first tile, from the vendored folder; eval was off.
    assert lines[-1] == {"evalBlocked": True, "fetched": ["plexora/client/external/jpegxr-0.3.0/jpegxr.wasm"]}
    assert sorted(found["name"] for found in lines[:-1]) == sorted(files)
    for found in lines[:-1]:
        assert "error" not in found, found
        path = files[found["name"]]
        page = {"width": found["width"], "height": found["height"], "channels": found["channels"],
                "colorformat": found["colorFormat"], "bitdepth": found["bitDepth"],
                "bitsperpixel": found["bitsPerPixel"], "alpha": found["alpha"], "bgr": found["bgr"],
                "data": path.with_name(path.name + ".raw").read_bytes()}
        shim.set_jpegxr(lambda data, page=page: page)
        try:
            if found["alpha"]:
                with pytest.raises(NotImplementedError, match="alpha"):
                    shim.jpegxr_decode(path.read_bytes())
                continue
            got = shim.jpegxr_decode(path.read_bytes(), out=1)
        finally:
            shim.set_jpegxr(None)
        want = imagecodecs.jpegxr_decode(path.read_bytes())
        assert (got.shape, got.dtype) == (want.shape, want.dtype), found
        assert np.array_equal(got, want), found["name"]
        assert got.flags.writeable


# -- CZI's Zstandard (czifile's compression 5 and 6) ---------------------------

ZSTD1_CASES = ("gray8_plain", "gray8_zstd1", "gray16_plain", "gray16_zstd1", "gray16_hilo",
               "bgr24_plain", "bgr24_zstd1", "bgr48_plain", "bgr48_hilo", "bgra32_zstd1",
               "float32_hilo", "unknown_chunk", "hilo_flag_off")


@pytest.fixture(scope="module")
def zstd1_cases():
    """{name: (subblock, itemsize, samples)}: subblocks as CZI writers make
    them -- a plain frame (compression 5), ZSTD1 with and without hi/lo
    packing (6) -- in each pixel type czifile hands over, and the header
    variants imagecodecs reads."""
    if not hasattr(imagecodecs, "zstd1_encode"):
        pytest.skip("this imagecodecs has no ZSTD1 (2026.6.6 or later)")
    rng = np.random.default_rng(4)
    gray8 = rng.integers(0, 255, (37, 41), dtype=np.uint8)
    gray16 = (np.add.outer(np.arange(50), np.arange(61)) * 300 + rng.integers(0, 90, (50, 61))).astype(np.uint16)
    bgr24 = rng.integers(0, 255, (30, 33, 3), dtype=np.uint8)
    bgr48 = bgr24.astype(np.uint16) * 257
    bgra32 = rng.integers(0, 255, (21, 19, 4), dtype=np.uint8)
    float32 = rng.random((17, 23), dtype=np.float32)

    def frame(array):
        return imagecodecs.zstd_encode(array.tobytes())

    def zstd1(array, hilo=False):
        return imagecodecs.zstd1_encode(array.tobytes(), itemsize=array.dtype.itemsize, hilo=hilo)

    return {
        "gray8_plain": (frame(gray8), 1, 1),
        "gray8_zstd1": (zstd1(gray8), 1, 1),
        "gray16_plain": (frame(gray16), 2, 1),
        "gray16_zstd1": (zstd1(gray16), 2, 1),
        "gray16_hilo": (zstd1(gray16, hilo=True), 2, 1),
        "bgr24_plain": (frame(bgr24), 1, 3),
        "bgr24_zstd1": (zstd1(bgr24), 1, 3),
        "bgr48_plain": (frame(bgr48), 2, 3),
        "bgr48_hilo": (zstd1(bgr48, hilo=True), 2, 3),
        "bgra32_zstd1": (zstd1(bgra32), 1, 4),
        "float32_hilo": (zstd1(float32, hilo=True), 4, 1),
        # A chunk type it does not know ends the header: no unpacking.
        "unknown_chunk": (bytes([3, 7, 1]) + frame(gray16), 2, 1),
        "hilo_flag_off": (bytes([3, 1, 0]) + frame(gray16), 2, 1),
    }


@pytest.mark.parametrize("name", ZSTD1_CASES)
def test_zstd1_decodes_what_imagecodecs_decodes(zstd1_cases, name):
    """As czifile calls it (itemsize, samples, the subblock's size as `out`),
    and without `out`, and with room to spare."""
    subblock, itemsize, samples = zstd1_cases[name]
    size = len(imagecodecs.zstd1_decode(subblock, itemsize=itemsize, samples=samples))
    for out in (size, None, size + 10):
        want = imagecodecs.zstd1_decode(subblock, itemsize=itemsize, samples=samples, out=out)
        assert shim.zstd1_decode(subblock, itemsize=itemsize, samples=samples, out=out) == bytes(want)
    assert shim.ZSTD1.available


def test_zstd1_refuses_what_imagecodecs_refuses(zstd1_cases):
    hilo = zstd1_cases["bgr48_hilo"][0]
    plain = zstd1_cases["gray8_plain"][0]  # 37 x 41 bytes
    for data, kwargs, error in [
        (hilo, dict(itemsize=2, samples=3, out=100), RuntimeError),  # more than `out` holds
        (hilo, dict(itemsize=2, samples=2), ValueError),
        (hilo, dict(itemsize=0), ValueError),
        (b"\x00", {}, ValueError),
        (bytes([2, 1]) + plain, {}, ValueError),  # chunk type 1 without its byte
        (bytes([3, 1, 1]) + imagecodecs.zstd_encode(b"abc"), dict(itemsize=2), ValueError),
        (plain, dict(itemsize=2, samples=3), ValueError),  # not whole pixels
    ]:
        with pytest.raises(error):
            imagecodecs.zstd1_decode(data, **kwargs)
        with pytest.raises(error):
            shim.zstd1_decode(data, **kwargs)


#: Written here with the real codecs; read in a fresh interpreter with the shim.
FILES = {
    "lzw": dict(compression="lzw"),
    "deflate": dict(compression="zlib"),
    "zstd": dict(compression="zstd"),
    "lzma": dict(compression="lzma"),
    "packbits": dict(compression="packbits"),
    "lzw_predictor": dict(compression="lzw", predictor=True),
    "jpeg_ycbcr": dict(compression="jpeg", photometric="rgb", rgb=True),
    # Stored as RGB with no YCbCr transform, as an Aperio SVS writes it.
    "jpeg_rgb": dict(compression="jpeg", photometric="rgb", rgb=True,
                     compressionargs={"level": 90, "outcolorspace": "RGB"}),
    "jpeg_gray": dict(compression="jpeg", photometric="minisblack", gray=True),
}

READER = """
import json, sys
import numpy as np
from plexora.browser import imagecodecs_shim
imagecodecs_shim.install()
import tifffile
assert tifffile.tifffile.imagecodecs is imagecodecs_shim, tifffile.tifffile.imagecodecs
out = {}
for name, path in json.loads(sys.argv[1]).items():
    try:
        array = tifffile.imread(path)
        np.save(path + ".npy", array)
        out[name] = "ok"
    except Exception as exc:
        out[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    folder = tmp_path_factory.mktemp("codecs")
    rng = np.random.default_rng(3)
    smooth = (np.add.outer(np.arange(96), np.arange(128)) * 120).astype(np.uint16)
    noisy = (smooth + rng.integers(0, 200, smooth.shape)).astype(np.uint16)
    rgb = np.stack([smooth // 256, (noisy // 300).astype(np.uint16), 255 - smooth // 256],
                   axis=-1).astype(np.uint8)
    paths, truth = {}, {}
    for name, options in FILES.items():
        options = dict(options)
        is_rgb, is_gray = options.pop("rgb", False), options.pop("gray", False)
        data = rgb if is_rgb else (rgb[..., 0] if is_gray else noisy)
        path = folder / f"{name}.tif"
        tifffile.imwrite(path, data, tile=(32, 32), **options)
        paths[name] = str(path)
        truth[name] = tifffile.imread(path)  # read with the real imagecodecs
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run([sys.executable, "-c", READER, json.dumps(paths)], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    return paths, truth, json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", sorted(FILES))
def test_tifffile_reads_the_file_through_the_shim(written, name):
    paths, truth, outcome = written
    assert outcome[name] == "ok", outcome[name]
    through = np.load(paths[name] + ".npy")
    assert through.shape == truth[name].shape and through.dtype == truth[name].dtype
    if name.startswith("jpeg"):
        # Two builds of libjpeg-turbo may round an IDCT differently; a colour
        # space got wrong is off by tens of levels, not one or two.
        assert np.abs(through.astype(int) - truth[name].astype(int)).max() <= 2
    else:
        assert np.array_equal(through, truth[name])


def test_jpeg_components_kept_as_stored_when_ycbcr_is_asked_for():
    """A fluorescence MRXS keeps one filter in each of a JPEG's three
    components and reads them as stored (`outcolorspace="YCBCR"`): the shim
    must not convert them to colour, or every channel is a mix of three."""
    rng = np.random.default_rng(3)
    planes = rng.integers(0, 255, (3, 64, 80), dtype=np.uint8)
    planes[0, :, :40] = 0                         # channels that differ plainly
    planes[2, 32:, :] = 250
    stored = np.ascontiguousarray(np.moveaxis(planes, 0, -1))
    data = imagecodecs.jpeg8_encode(stored, level=95, colorspace="YCBCR",
                                    outcolorspace="YCBCR", subsampling=(1, 1))
    want = imagecodecs.jpeg8_decode(data, colorspace="YCBCR", outcolorspace="YCBCR")
    got = shim.jpeg8_decode(data, colorspace="YCBCR", outcolorspace="YCBCR")
    assert got.shape == want.shape == (64, 80, 3)
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 2
    converted = shim.jpeg8_decode(data)            # colour, as asked by default
    assert np.abs(converted.astype(int) - want.astype(int)).max() > 20
