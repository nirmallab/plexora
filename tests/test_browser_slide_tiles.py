"""The browser node's whole-slide tile arithmetic (client node/slideTiles.js)
against brightfield.py's own, in Node: for slides of every shape a scanner
writes (halving and quartering pyramids, odd sizes, levels a pixel short),
the page reads a tile itself exactly when `_Level.read_rgb` would not resample
it, from the same level-0 origin `_OpenSlideSource.read` asks for, and its
RGBA-over-white is the same bytes. With brightfield.page_tile_layout's
description of a page slide's pyramid.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from plexora.server.utils import brightfield

ROOT = Path(__file__).resolve().parents[1]
TILES = ROOT / "plexora" / "client" / "src" / "js" / "node" / "slideTiles.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


class _Recording:
    """A slide shaped like openslide.OpenSlide that remembers what was read."""

    def __init__(self, dims, downsamples):
        self.level_dimensions = tuple(dims)
        self.level_downsamples = tuple(downsamples)
        self.level_count = len(dims)
        self.reads = []

    def read_region(self, location, level, size):
        self.reads.append((tuple(location), level, tuple(size)))
        return np.broadcast_to(np.uint8(255), (size[1], size[0], 4))


def _slide(width, height, step, count, short=0):
    dims, downs = [], []
    for level in range(count):
        factor = step ** level
        w, h = max(1, width // factor - (short if level else 0)), max(1, height // factor - (short if level else 0))
        dims.append((w, h))
        downs.append(((width / w) + (height / h)) / 2)  # OpenSlide's own mean
    return _Recording(dims, downs)


def _node(script: str, payload) -> object:
    code = (f"import * as t from {json.dumps(TILES.as_uri())};\n"
            "import { readFileSync } from 'node:fs';\n"
            f"const input = JSON.parse(readFileSync(0, 'utf8'));\n{script}\n")
    done = subprocess.run(["node", "--input-type=module", "-e", code], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def _layout(recording):
    sources = [brightfield._OpenSlideSource(recording, i) for i in range(recording.level_count)]
    shapes = brightfield._dyadic_shapes(sources[0].height, sources[0].width)
    levels = []
    for height, width in shapes:
        source = brightfield._pick_source(sources, height, width)
        if levels and not brightfield._affordable(source, height, width):
            break
        levels.append(brightfield._Level(source, height, width))
    return levels


@pytest.mark.parametrize("width,height,step,count,short", [
    (7436, 15494, 2, 6, 0),       # MRXS, a halving pyramid
    (46000, 32914, 4, 3, 0),      # SVS, quartering, an odd height
    (51200, 38144, 4, 5, 0),      # NDPI
    (15374, 17497, 4, 3, 0),      # SVS JPEG 2000
    (10001, 9999, 2, 5, 1),       # levels a pixel short of a halving
])
def test_the_page_reads_a_tile_exactly_when_python_would_not_resample(width, height, step, count, short):
    recording = _slide(width, height, step, count, short)
    levels = _layout(recording)
    tw = th = brightfield.TILE_SIZE
    cases, want = [], []
    # The first rows and columns, the middle and the last two of each level:
    # every way a tile meets a level's edges and the rounding in between.
    def sample(n):
        return sorted({i for i in (0, 1, 2, n // 2, n - 2, n - 1) if 0 <= i < n})

    for index, level in enumerate(levels):
        h, w = level.shape[1], level.shape[2]
        for ty in sample(-(-h // th)):
            for tx in sample(-(-w // tw)):
                recording.reads.clear()
                block = level.rgb[ty * th:ty * th + th, tx * tw:tx * tw + tw]
                (origin, native, size), = recording.reads
                read_whole = (size[1], size[0]) == block.shape[:2]
                want.append({"x": origin[0], "y": origin[1], "level": native,
                             "w": size[0], "h": size[1]} if read_whole else None)
                source = level._source
                cases.append({"level": {"height": h, "width": w, "native": source._level,
                                        "downsample": source._downsample,
                                        "sourceHeight": source.height, "sourceWidth": source.width,
                                        "scaleY": level.scale_y, "scaleX": level.scale_x},
                              "tx": tx, "ty": ty})
    got = _node("console.log(JSON.stringify(input.map((c) => t.tileWindow(c.level, c.tx, c.ty, 1024, 1024))));",
                cases)
    assert got == want
    # Every case the page takes is one Python reads whole; both kinds occur
    # wherever the scanner's levels are not exact halvings.
    assert any(w is not None for w in want)


def test_page_tile_layout_describes_a_page_slide(monkeypatch):
    from plexora.server.providers import browser

    recording = _slide(46000, 32914, 4, 3)
    page = browser.PageSlide("/picked/abc/slide.svs", {
        "levels": recording.level_count, "dims": [list(d) for d in recording.level_dimensions],
        "downsamples": list(recording.level_downsamples), "properties": {"openslide.vendor": "aperio"}})
    sources = [brightfield._OpenSlideSource(page, i) for i in range(page.level_count)]
    levels = [brightfield._Level(brightfield._pick_source(sources, h, w), h, w)
              for h, w in brightfield._dyadic_shapes(32914, 46000)]
    pyramid = brightfield.RgbPyramid(levels, path=page.path, handle=page)
    layout = brightfield.page_tile_layout(pyramid)
    assert layout["path"] == "/picked/abc/slide.svs"
    assert [[lv["height"], lv["width"]] for lv in layout["levels"]] == pyramid.level_shapes
    assert layout["levels"][2]["native"] == 1 and layout["levels"][2]["downsample"] == page.level_downsamples[1]
    # Anything else is Python's.
    assert brightfield.page_tile_layout(brightfield.RgbPyramid(levels, path=page.path, handle=recording)) is None


def test_over_white_and_rounding_are_pythons():
    rng = np.random.default_rng(3)
    rgba = rng.integers(0, 256, (64, 64, 4), dtype=np.uint8)
    rgba[:8, :, 3] = 255
    rgba[8:16, :, 3] = 0
    alpha = rgba[..., 3:4].astype(np.uint16)
    want = ((rgba[..., :3].astype(np.uint16) * alpha + 255 * (255 - alpha)) // 255).astype(np.uint8)
    got = _node("console.log(JSON.stringify(Array.from(t.overWhite(Uint8Array.from(input), input.length / 4))));",
                rgba.reshape(-1).tolist())
    assert np.array_equal(np.array(got, dtype=np.uint8).reshape(64, 64, 3), want)
    values = [0.5, 1.5, 2.5, -0.5, 3.4999, 3.5001, 1234.5, 1235.5, 7.0]
    assert _node("console.log(JSON.stringify(input.map(t.roundHalfEven)));", values) == [round(v) for v in values]
