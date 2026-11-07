"""DICOM whole-slide images on the website (plan L3): a slide's folder of
instances picked with the landing's Folder, imported and drawn in Chrome
(tests/browser/slides.mjs), read by the same Python the desktop reads them
with (server/utils/dicom_wsi.py: pydicom and wsidicom, in the website's
`dicom` stage), frames decoded through imagecodecs_shim -- JPEG by Pillow,
JPEG 2000 by the page's OpenJPEG.

Every tile the website draws is the desktop's tile (`data_model.read_tile`
over `dicom_wsi.open_image`, this environment's wsidicom and imagecodecs):
byte for byte for an uncompressed slide; for JPEG and JPEG 2000 within a
stated tolerance (the website's Pillow is not libjpeg-turbo, and its OpenJPEG
is another build): PSNR >= 45 dB a tile, max |difference| <= 16 at a pixel. Every
channel of the fluorescence slide is checked, not only those drawn. Synthetic slides (tests/dicom_fixtures.py: a
three-marker fluorescence slide and an H&E, three levels each) always run;
OpenSlide's public Leica-4 (JPEG, sparse) and JP2K-33003-1-v2 (JPEG 2000,
YBR_FULL_422 -- the one OpenSlide refuses) when `PLEXORA_SLIDE_FIXTURES` has
them.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from tests.dicom_fixtures import write_he_slide, write_if_slide

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "slides.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"
FIXTURES = Path(os.environ.get("PLEXORA_SLIDE_FIXTURES", "C:/plexora-bench-slides/fixtures"))
BUNDLE = Path(os.environ.get("PLEXORA_BENCH_HOME", "C:/plexora-bench")) / "browser_node" / "web" / "py"
REAL = {"leica": "DICOM/Leica-4", "jp2k": "DICOM/JP2K-33003-1-v2"}


def _bundle_has_dicom() -> bool:
    for stages in BUNDLE.glob("*/stages.json"):
        if "dicom" in json.loads(stages.read_text(encoding="utf-8")):
            return True
    return False


needs_website = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_dir() or not PLAYWRIGHT.is_dir() or not _bundle_has_dicom(),
    reason="needs node, a static build, playwright-core and a website bundle with the dicom stage "
           "(tools/browser_build/build.py)")


def _pixels(encoded: str) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        return np.asarray(image)


@pytest.fixture(scope="module")
def slides(tmp_path_factory):
    root = tmp_path_factory.mktemp("dicom")
    found = {"fluorescence": write_if_slide(root / "if", height=2048, width=2560, levels=3),
             "he": write_he_slide(root / "he", height=2048, width=2560, levels=3)}
    for name, rel in REAL.items():
        if (FIXTURES / rel).is_dir():
            found[name] = FIXTURES / rel
    return found


@pytest.fixture(scope="module")
def website(slides, tmp_path_factory):
    shots = tmp_path_factory.mktemp("shots")
    pairs = ",".join(f"{name}={path}" for name, path in slides.items())
    done = subprocess.run(["node", str(DRIVER), "--slides", pairs, "--shots", str(shots), "--tiles", "2",
                           "--channels", "3"],
                          capture_output=True, text=True, encoding="utf-8", timeout=2400, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-3000:] + done.stdout[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1]), shots


def _desktop(folder):
    from plexora.server.utils import dicom_wsi

    detection = dicom_wsi.detect_image_type(folder)
    return dicom_wsi.open_image(folder, rgb=detection.is_brightfield), detection


SHIM_PROBE = r"""
import hashlib, json, sys
from plexora.browser import imagecodecs_shim
imagecodecs_shim.install()
from wsidicom.codec import optionals
from plexora.server.utils import dicom_wsi
from plexora.server.models import data_model
pyramid = dicom_wsi.open_image(sys.argv[1], rgb=True)
tiles = [data_model.read_tile(pyramid, "rgb", level, "1_1.png", 1024, 1024) for level in (0, 1)]
print(json.dumps({"imagecodecs": optionals.IMAGE_CODECS_AVAILABLE,
                  "sha": [hashlib.sha256(t.tobytes()).hexdigest() for t in tiles]}))
"""


def test_wsidicom_decodes_through_the_shim_as_through_imagecodecs():
    # In CPython, without a browser: the website's stand-in for imagecodecs
    # put where wsidicom imports it, against the real one, on a JPEG slide.
    import hashlib
    import sys

    folder = FIXTURES / REAL["leica"]
    if not folder.is_dir():
        pytest.skip(f"{REAL['leica']} is not in {FIXTURES}")
    from plexora.server.models import data_model
    from plexora.server.utils import dicom_wsi

    pyramid = dicom_wsi.open_image(folder, rgb=True)
    want = [hashlib.sha256(data_model.read_tile(pyramid, "rgb", level, "1_1.png", 1024, 1024).tobytes())
            .hexdigest() for level in (0, 1)]
    done = subprocess.run([sys.executable, "-c", SHIM_PROBE, str(folder)], capture_output=True,
                          text=True, encoding="utf-8", timeout=300, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-3000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["imagecodecs"] is True  # wsidicom took the shim as imagecodecs
    assert got["sha"] == want


@needs_website
@pytest.mark.parametrize("name", ["fluorescence", "he", "leica", "jp2k"])
def test_a_dicom_slide_opens_and_its_tiles_are_the_desktops(slides, website, name):
    if name not in slides:
        pytest.skip(f"{REAL[name]} is not in {FIXTURES}")
    from plexora.server.models import data_model

    out, shots = website
    row = out["slides"][name]
    assert row.get("viewerUp"), (row.get("stuck"), row.get("errors"))
    assert (shots / f"slides-{name}.png").stat().st_size > 5_000
    pyramid, detection = _desktop(slides[name])
    assert row["layers"]
    exact = name in ("fluorescence", "he")  # uncompressed
    checked, channels = 0, set()
    for tile in row["tiles"]:
        assert tile["python"], (tile["channel"], tile["pythonStatus"])
        number = data_model._parse_channel(tile["channel"])[0]
        want = data_model.read_tile(pyramid, number, tile["level"], f"{tile['tx']}_{tile['ty']}.png",
                                    *row["spec"]["tile"])
        got = _pixels(tile["python"])
        assert got.shape == want.shape, (tile["channel"], tile["level"], got.shape, want.shape)
        diff = np.abs(got.astype(np.int32) - want.astype(np.int32))
        where = (tile["channel"], tile["level"], tile["tx"], tile["ty"], int(diff.max()))
        if exact:
            assert not diff.any(), where
        else:
            mse = float(np.mean(diff.astype(np.float64) ** 2))
            assert diff.max() <= 16 and (mse == 0 or 10 * np.log10(255 ** 2 / mse) >= 45), (where, mse)
        checked += 1
        channels.add(tile["channel"])
    assert checked >= 3
    assert len(channels) == (1 if detection.is_brightfield else 3), channels
