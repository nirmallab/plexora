"""The website's TIFF LZW decoder (client src/js/node/lzw.js) decodes what
imagecodecs and libtiff encode.

geotiff.js's own decoder fails on a tile whose encoder fills the code table to
its last entries before clearing it (tifffile's does: imagecodecs), so the
browser node reads LZW with its own. The vectors are written here --
imagecodecs for the random, run and image cases, libtiff through Pillow for a
whole strip -- and decoded in node (tests/js/tiff_lzw_probe.mjs).
"""

import io
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tiff_lzw_probe.mjs"


def _vectors(out: Path) -> None:
    imagecodecs = pytest.importorskip("imagecodecs")
    rng = np.random.default_rng(3)
    y, x = np.mgrid[0:1024, 0:1024]
    cases = {
        "random": rng.integers(0, 256, 262144, dtype=np.uint8).tobytes(),
        "zeros": bytes(1 << 20),
        "one_byte": b"\x07",
        "runs": np.repeat(rng.integers(0, 256, 4096, dtype=np.uint8), rng.integers(1, 400, 4096)).tobytes(),
        "gradient16": ((x * 37 + y * 11) % 65536).astype("<u2").tobytes(),
        "noisy16": ((x + y) * 20 + rng.integers(0, 900, (1024, 1024))).astype("<u2").tobytes(),
        "text": b"the quick brown fox jumps over the lazy dog " * 5000,
    }
    for name, raw in cases.items():
        (out / f"{name}.raw").write_bytes(raw)
        (out / f"{name}.lzw").write_bytes(imagecodecs.lzw_encode(raw))
    # libtiff's encoder, through Pillow: the strips of a whole image.
    Image = pytest.importorskip("PIL.Image")
    tifffile = pytest.importorskip("tifffile")
    plane = ((x // 3 + y // 5) % 251 + rng.integers(0, 4, (1024, 1024))).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(plane).save(buffer, format="TIFF", compression="tiff_lzw")
    data = buffer.getvalue()
    with tifffile.TiffFile(io.BytesIO(data)) as tif:
        page = tif.pages[0]
        assert page.compression == 5
        rows = page.rowsperstrip
        strips = list(zip(page.dataoffsets, page.databytecounts))
    for k, (start, count) in enumerate(strips[:4]):
        (out / f"libtiff_strip{k}.lzw").write_bytes(data[start:start + count])
        (out / f"libtiff_strip{k}.raw").write_bytes(plane[k * rows:(k + 1) * rows].tobytes())


def test_the_browser_nodes_lzw_decodes_imagecodecs_and_libtiff(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    _vectors(tmp_path)
    done = subprocess.run([node, str(PROBE), str(tmp_path)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "lzw probe OK" in done.stdout
