"""JPEG XR and CZI's Zstandard on the website: the website's Python decodes
them through imagecodecs_shim -- JPEG XR with the worker's jxrlib
(client/src/js/workers/jpegxr.js, client/external/jpegxr-0.3.0), a CZI's zstd
and ZSTD1 subblocks with the stdlib's zstd -- pixel for pixel what imagecodecs
gives on the desktop (tests/browser/czi_codecs.mjs, in Chrome).

JPEG XR comes in TIFFs tifffile writes (czifile hands a CZI's JPEG XR
subblocks to the same `jpegxr_decode`); zstd as the subblocks a CZI holds,
decoded with the arguments czifile passes.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

imagecodecs = pytest.importorskip("imagecodecs")

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "czi_codecs.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "client" / "external" / "jpegxr-0.3.0"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_dir() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build) and playwright-core")


def _facts(array):
    return {"shape": list(array.shape), "dtype": str(array.dtype),
            "sha256": hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()}


def _read_on_the_website(folder, spec):
    (folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    done = subprocess.run(["node", str(DRIVER), "--dir", str(folder), "--spec", str(folder / "spec.json")],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])["files"]


def test_the_website_decodes_jpeg_xr_as_the_desktop_does(tmp_path):
    rng = np.random.default_rng(11)
    smooth = (np.add.outer(np.arange(150), np.arange(203)) * 160).astype(np.uint16)
    noisy = (smooth + rng.integers(0, 300, smooth.shape)).astype(np.uint16)
    tiles = {"tile": (64, 64), "photometric": "minisblack"}
    cases = {
        "gray8.tif": (np.clip(smooth // 160, 0, 255).astype(np.uint8), tiles),
        "gray16.tif": (noisy, tiles),
        # Lossy, as an Axioscan writes: the decoding is still exact.
        "gray16_lossy.tif": (noisy, {**tiles, "compressionargs": {"level": 0.8}}),
        "rgb8.tif": (rng.integers(0, 255, (130, 170, 3), dtype=np.uint8),
                     {"tile": (64, 64), "photometric": "rgb"}),
        "rgb16.tif": ((rng.integers(0, 255, (90, 111, 3)) * 257).astype(np.uint16),
                      {"photometric": "rgb", "rowsperstrip": 32}),
        "float32.tif": (rng.random((70, 81), dtype=np.float32), {"tile": (32, 32), "photometric": "minisblack"}),
    }
    want = {}
    for name, (data, options) in cases.items():
        tifffile.imwrite(tmp_path / name, data, compression="jpegxr", **options)
        want[name] = _facts(tifffile.imread(tmp_path / name))
    got = _read_on_the_website(tmp_path, {name: {"kind": "tiff"} for name in cases})
    for name, facts in want.items():
        assert {k: got[name].get(k) for k in facts} == facts, (name, got[name])


def test_the_website_decodes_czi_zstd_as_the_desktop_does(tmp_path):
    if not hasattr(imagecodecs, "zstd1_encode"):
        pytest.skip("this imagecodecs has no ZSTD1 (2026.6.6 or later)")
    rng = np.random.default_rng(12)
    gray16 = (np.add.outer(np.arange(120), np.arange(97)) * 200 + rng.integers(0, 90, (120, 97))).astype(np.uint16)
    bgr24 = rng.integers(0, 255, (64, 80, 3), dtype=np.uint8)
    bgr48 = bgr24.astype(np.uint16) * 257
    bgra32 = rng.integers(0, 255, (33, 35, 4), dtype=np.uint8)
    # A subblock and its pixels: compression 5 is a plain frame, 6 is ZSTD1
    # (16-bit with hi/lo byte packing, as ZEN writes it).
    cases = {
        "gray16_plain.zst": (imagecodecs.zstd_encode(gray16.tobytes()), gray16),
        "gray16_hilo.zst": (imagecodecs.zstd1_encode(gray16.tobytes(), itemsize=2, hilo=True), gray16),
        "bgr24.zst": (imagecodecs.zstd1_encode(bgr24.tobytes()), bgr24),
        "bgr48_hilo.zst": (imagecodecs.zstd1_encode(bgr48.tobytes(), itemsize=2, hilo=True), bgr48),
        "bgra32.zst": (imagecodecs.zstd1_encode(bgra32.tobytes()), bgra32),
    }
    spec, want = {}, {}
    for name, (encoded, pixels) in cases.items():
        (tmp_path / name).write_bytes(encoded)
        args = {"itemsize": pixels.dtype.itemsize, "samples": pixels.shape[2] if pixels.ndim == 3 else 1,
                "out": pixels.nbytes}
        spec[name] = {"kind": "zstd1", "dtype": str(pixels.dtype), **args}
        want[name] = _facts(np.frombuffer(imagecodecs.zstd1_decode(encoded, **args), pixels.dtype))
    got = _read_on_the_website(tmp_path, spec)
    for name, facts in want.items():
        assert {k: got[name].get(k) for k in facts} == facts, (name, got[name])
