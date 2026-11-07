"""JPEG 2000 on the website (plan L1): tifffile in the website's Python decodes
a TIFF's JPEG 2000 tiles through imagecodecs_shim and the page's OpenJPEG
(client/external/openjpeg-1.3.6), pixel for pixel what imagecodecs gives on
the desktop (tests/browser/j2k.mjs, in Chrome).
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "j2k.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "client" / "external" / "openjpeg-1.3.6"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_dir() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build) and playwright-core")


def _cases(folder):
    rng = np.random.default_rng(7)
    cases = {
        # One channel, 16-bit, tiled, odd edges.
        "u16.tif": (rng.integers(0, 60000, (2, 300, 410), dtype=np.uint16),
                    {"tile": (256, 256), "photometric": "minisblack", "metadata": {"axes": "CYX"}}),
        # 8-bit RGB: three components in one JPEG 2000 stream per tile.
        "rgb.tif": (rng.integers(0, 255, (260, 300, 3), dtype=np.uint8),
                    {"tile": (128, 128), "photometric": "rgb"}),
        # 12-bit values in 16-bit samples, stripped.
        "u12.tif": (rng.integers(0, 4095, (1, 200, 333), dtype=np.uint16),
                    {"photometric": "minisblack", "metadata": {"axes": "CYX"}, "rowsperstrip": 64}),
    }
    out = {}
    for name, (data, options) in cases.items():
        tifffile.imwrite(folder / name, data, compression="jpeg2000", **options)
        read = tifffile.imread(folder / name)
        assert np.array_equal(read, data)  # lossless on the desktop
        out[name] = {"shape": list(read.shape), "dtype": str(read.dtype),
                     "sha256": hashlib.sha256(read.tobytes()).hexdigest()}
    return out


def test_the_website_decodes_jpeg_2000_as_the_desktop_does(tmp_path):
    want = _cases(tmp_path)
    done = subprocess.run(["node", str(DRIVER), "--dir", str(tmp_path), "--files", ",".join(want)],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])["files"]
    for name, facts in want.items():
        assert {k: got[name].get(k) for k in facts} == facts, (name, got[name])
