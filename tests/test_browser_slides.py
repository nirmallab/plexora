"""Whole-slide images on the website (plan L2): an MRXS (with the folder beside
it), an NDPI and an SVS with JPEG 2000 tiles, picked, imported and drawn in
Chrome (tests/browser/slides.mjs). The page reads them with openslide-js
(client/external/openslide-js-1.0.0-beta.5) for the viewer and for Python
alike, so:

- every tile the browser node draws is brightfield.py's tile of the same
  slide over native OpenSlide 4.0.0 (tests/browser/slides_truth.py, with
  openslide-python), byte for byte -- for the MRXS, the desktop's own tile;
- every tile Python draws on the website (a resampled level) is that tile,
  byte for byte, too;
- the viewer drew the slide (a screenshot, and colour on its canvas).

Needs the OpenSlide test slides (`PLEXORA_SLIDE_FIXTURES`, laid out as
https://openslide.cs.cmu.edu/download/openslide-testdata/ has them, the zips
unpacked beside themselves), an interpreter with openslide-python and
openslide-bin 4.0.0.x (`PLEXORA_OPENSLIDE_PYTHON`), a static build, the
website's Python bundle and playwright-core; skipped otherwise.
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

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "slides.mjs"
TRUTH = ROOT / "tests" / "browser" / "slides_truth.py"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "client" / "external" / "openslide-js-1.0.0-beta.5"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"
FIXTURES = Path(os.environ.get("PLEXORA_SLIDE_FIXTURES", "C:/plexora-bench-slides/fixtures"))
OPENSLIDE = Path(os.environ.get("PLEXORA_OPENSLIDE_PYTHON",
                                "C:/plexora-bench-slides/venv-truth400/Scripts/python.exe"))
BUNDLE = Path(os.environ.get("PLEXORA_BENCH_HOME", "C:/plexora-bench")) / "browser_node" / "web" / "py"

SLIDES = {
    "mrxs": "Mirax/CMU-1-Saved-1_16/CMU-1-Saved-1_16.mrxs",
    "ndpi": "Hamamatsu/CMU-1.ndpi",
    "svsj2k": "Aperio/JP2K-33003-1.svs",
}

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_dir() or not PLAYWRIGHT.is_dir() or not BUNDLE.is_dir()
    or not OPENSLIDE.is_file() or not all((FIXTURES / rel).exists() for rel in SLIDES.values()),
    reason="needs node, a static build, the website's bundle, playwright-core, the OpenSlide test "
           "slides (PLEXORA_SLIDE_FIXTURES) and openslide-python (PLEXORA_OPENSLIDE_PYTHON)")


def _rgb(encoded: str) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        return np.asarray(image.convert("RGB"))


@pytest.fixture(scope="module")
def website(tmp_path_factory):
    shots = tmp_path_factory.mktemp("shots")
    pairs = ",".join(f"{name}={FIXTURES / rel}" for name, rel in SLIDES.items())
    done = subprocess.run(["node", str(DRIVER), "--slides", pairs, "--shots", str(shots)],
                          capture_output=True, text=True, encoding="utf-8", timeout=1800, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-3000:] + done.stdout[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    return out, shots


@pytest.fixture(scope="module")
def desktop(website, tmp_path_factory):
    out, _ = website
    spec = {}
    for name, row in out["slides"].items():
        assert row.get("tiles"), (name, row.get("stuck"), row.get("errors"))
        spec[name] = {"path": str(FIXTURES / SLIDES[name]), "tile": row["spec"]["tile"],
                      "tiles": [{"level": t["level"], "tx": t["tx"], "ty": t["ty"]} for t in row["tiles"]]}
    folder = tmp_path_factory.mktemp("truth")
    (folder / "spec.json").write_text(json.dumps(spec))
    done = subprocess.run([str(OPENSLIDE), "-I", str(TRUTH), str(folder / "spec.json"), str(folder)],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads((folder / "truth.json").read_text()), folder


@pytest.mark.parametrize("name", list(SLIDES))
def test_the_slide_opens_and_is_drawn(website, name):
    out, shots = website
    row = out["slides"][name]
    assert row.get("viewerUp"), (row.get("stuck"), row.get("errors"))
    assert (shots / f"slides-{name}.png").stat().st_size > 10_000
    # Stained tissue on the canvas, not an empty or black viewer.
    assert row["canvas"] and row["canvas"]["tintedFraction"] > 0.02, row["canvas"]
    spec = row["spec"]
    assert spec["served"] and spec["kind"] == "slide", spec
    if name == "mrxs":
        assert "Choose the folder" in (row.get("asked") or "")


@pytest.mark.parametrize("name", list(SLIDES))
def test_tiles_are_the_desktops(website, desktop, name):
    out, _ = website
    truth, folder = desktop
    row = out["slides"][name]
    # The same pyramid: every level the page serves is the desktop's level.
    levels = [[level["height"], level["width"]] for level in row["spec"]["slide"]["levels"]]
    assert levels == truth[name]["levels"][:len(levels)]
    by_node = 0
    for tile in row["tiles"]:
        want = np.load(folder / name / f"{tile['level']}_{tile['tx']}_{tile['ty']}.npy")
        where = (tile["level"], tile["tx"], tile["ty"])
        assert tile["python"], (where, tile["pythonStatus"])
        assert np.array_equal(_rgb(tile["python"]), want), ("python", where)
        if tile["node"]:
            by_node += 1
            assert np.array_equal(_rgb(tile["node"]), want), ("node", where)
    # The browser node draws the tiles of the levels the file holds as they are.
    assert by_node >= len(row["spec"]["slide"]["levels"]) // 2, by_node
