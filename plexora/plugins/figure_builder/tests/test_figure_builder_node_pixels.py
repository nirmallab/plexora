"""Quick Edit's pixels read by the page agree with the server's
(static/figureNodePixels.js against server/pixels.read_region).

On the website Quick Edit asks the page's own reader of the picked file
(core's PlexoraNode) for a region instead of the page's Python, which answers
one request at a time. That is only worth doing if the answer is the same: a
mini view that drew a different picture from the one the server would have
drawn is a figure that changes when it is opened somewhere else. So the
transcription is held to the server's arithmetic here -- the level chosen,
the box floored, ceiled and clipped to it, and Pillow's LANCZOS resample -- on
a made-up pyramid served tile by tile through a stand-in PlexoraNode.
"""

import base64
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from plexora.plugins.figure_builder.server import pixels, render

STATIC = Path(__file__).resolve().parents[1] / "static"
TILE = 64

DRIVER = r"""
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const levels = input.levels.map((level) => ({
    shape: level.shape, bytes: Buffer.from(level.data, "base64"),
}));
const ctx = { console, Math, Promise, Uint8Array, Uint16Array, Float32Array, Float64Array, Int32Array,
              encodeURIComponent, decodeURIComponent, URL };
ctx.globalThis = ctx;
ctx.PlexoraBackend = { kind: "browser" };
ctx.PlexoraNode = {
    project: async () => ({
        spec: { levels: levels.map((l) => l.shape), tile: [input.tile, input.tile] },
        image: { pool: {}, geometry: { levels: levels.map((l) => l.shape) } },
    }),
    decoded: async (url) => {
        const m = /^\/generated\/data\/([^/]+)\/([^/]+)\/(\d+)\/(\d+)_(\d+)\.png\?q=hd$/.exec(url);
        if (!m) return null;
        const channel = Number(/_(\d+)$/.exec(decodeURIComponent(m[2]))[1]);
        const level = levels[Number(m[3])];
        const [, h, w] = level.shape;
        const left = Number(m[4]) * input.tile, top = Number(m[5]) * input.tile;
        const tw = Math.min(input.tile, w - left), th = Math.min(input.tile, h - top);
        if (tw <= 0 || th <= 0) return null;
        const out = new Uint8Array(tw * th * 2);
        for (let y = 0; y < th; y += 1) {
            for (let x = 0; x < tw; x += 1) {
                const at = ((channel * h + top + y) * w + left + x) * 2;
                out[(y * tw + x) * 2] = level.bytes[at];
                out[(y * tw + x) * 2 + 1] = level.bytes[at + 1];
            }
        }
        return { array: out, width: tw, height: th, format: "u16" };
    },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(input.script, "utf8") + "\nglobalThis.FigureNodePixels = FigureNodePixels;", ctx);
(async () => {
    const results = [];
    for (const ask of input.asks) {
        const got = await ctx.FigureNodePixels.read("ds", ask.key, ask);
        const level = ctx.FigureNodePixels.chooseViewLevel(levels.length, ask.w, ask.out_w);
        results.push(got ? { level, width: got.width, height: got.height,
                             data: Buffer.from(got.data.buffer).toString("base64") } : null);
    }
    process.stdout.write(JSON.stringify({
        results, available: ctx.FigureNodePixels.available("ds", "ds"),
        elsewhere: ctx.FigureNodePixels.available("other", "ds"),
        plane: await ctx.FigureNodePixels.read("ds", "CD3_1", { x: 0, y: 0, w: 10, h: 10, out_w: 10, out_h: 10, view: "z3" }),
    }));
})();
"""


class _Source:
    def __init__(self, levels):
        self.levels = levels


def _pyramid(dtype):
    rng = np.random.default_rng(7 if dtype == np.uint16 else 8)
    shape0 = (3, 301, 457)
    levels = []
    for k in range(4):
        h, w = math.ceil(shape0[1] / 2 ** k), math.ceil(shape0[2] / 2 ** k)
        high = 65535 if dtype == np.uint16 else 255
        levels.append(rng.integers(0, high + 1, size=(3, h, w)).astype(dtype))
    return levels


def _expected(levels, ask):
    level = render.choose_view_level(_Source(len(levels)), ask["w"], ask["out_w"])
    array = levels[level]
    _, height, width = array.shape
    divisor = 2 ** level
    x0 = max(0, min(int(math.floor(ask["x"] / divisor)), width))
    y0 = max(0, min(int(math.floor(ask["y"] / divisor)), height))
    x1 = max(x0, min(int(math.ceil((ask["x"] + ask["w"]) / divisor)), width))
    y1 = max(y0, min(int(math.ceil((ask["y"] + ask["h"]) / divisor)), height))
    channel = int(ask["key"].rsplit("_", 1)[1])
    if x1 <= x0 or y1 <= y0:
        plane = np.zeros((1, 1), dtype=np.float32)
    else:
        plane = array[channel, y0:y1, x0:x1]
    return level, pixels._resample(plane, ask["out_w"], ask["out_h"])


ASKS = [
    # the box is the output size: no resample at all
    {"key": "DAPI_0", "x": 10, "y": 20, "w": 200, "h": 120, "out_w": 200, "out_h": 120},
    # downsampled within level 0, and across tile edges
    {"key": "CD3_1", "x": 33, "y": 41, "w": 300, "h": 200, "out_w": 230, "out_h": 150},
    # a coarser level, then resampled
    {"key": "CD8_2", "x": 0, "y": 0, "w": 457, "h": 301, "out_w": 120, "out_h": 80},
    # upsampled
    {"key": "CD3_1", "x": 100.4, "y": 80.6, "w": 40.5, "h": 30.2, "out_w": 160, "out_h": 120},
    # off the image's edge
    {"key": "DAPI_0", "x": 400, "y": 250, "w": 120, "h": 90, "out_w": 96, "out_h": 72},
    # entirely outside it
    {"key": "DAPI_0", "x": 900, "y": 900, "w": 50, "h": 50, "out_w": 20, "out_h": 20},
]


@pytest.mark.parametrize("dtype", [np.uint16, np.uint8], ids=["uint16", "uint8"])
def test_the_page_reads_what_the_server_reads(dtype):
    if not shutil.which("node"):
        pytest.skip("needs node")
    levels = _pyramid(dtype)
    payload = {
        "script": str(STATIC / "figureNodePixels.js"),
        "tile": TILE,
        # Two bytes a sample, big-endian, as the HD tile carries them; an
        # 8-bit source widened as Python's astype("uint16") widens it.
        "levels": [{"shape": list(level.shape),
                    "data": base64.b64encode(level.astype(">u2").tobytes()).decode()}
                   for level in levels],
        "asks": ASKS,
    }
    done = subprocess.run(["node", "-e", DRIVER], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout)
    assert out["available"] is True
    # Never about a datasource the viewer has not got open: asking the node
    # would load it in the page's Python.
    assert out["elsewhere"] is False
    # A z-stack's plane or projection is the server's.
    assert out["plane"] is None

    for ask, got in zip(ASKS, out["results"]):
        level, expected = _expected(levels, ask)
        assert got is not None, ask
        assert got["level"] == level, ask
        assert (got["width"], got["height"]) == (ask["out_w"], ask["out_h"])
        data = np.frombuffer(base64.b64decode(got["data"]), dtype="<u2").reshape(
            ask["out_h"], ask["out_w"])
        difference = np.abs(data.astype(np.int64) - expected.astype(np.int64))
        # The same numbers; a resample's last bit may differ from C's sin.
        assert difference.max() <= 1, ask
        assert (difference == 0).mean() >= 0.99, ask
        if (ask["w"], ask["h"]) == (ask["out_w"], ask["out_h"]):
            assert difference.max() == 0, ask


def test_quick_edit_asks_the_page_first_and_the_server_otherwise():
    source = (STATIC / "figureQuickEdit.js").read_text(encoding="utf-8")
    body = source[source.index("    async readPixels(params) {"):source.index("    activeSlots() {")]
    assert body.index("FigureNodePixels.read(") < body.index("this.api.readPixels(")
    assert "FigureNodePixels.available(datasource, this.loadedDatasource)" in body
