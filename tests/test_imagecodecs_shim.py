"""The website's stand-in for imagecodecs, held to the real one.

imagecodecs has no WebAssembly build, so the website's Python decodes TIFF
tiles through plexora/browser/imagecodecs_shim.py. Each codec here is checked
against what imagecodecs itself produces, and tifffile is checked to read
files written by the real codecs when the shim is all it has.
"""

import json
import os
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
