"""The GPU cell layer, checked without a GPU.

`tests/js/label_gpu_probe.mjs` runs the shipped files under node: the viewer's
side sliced from imageViewer.js, labelGpu.js against a recording WebGL2
context, the browser-side gate against the server's rules, and the CPU
reference it must agree with. This drives it and names every check, so a check
that is quietly deleted fails here instead of passing silently.

QC's third state rides on the same mask: a cell QC says cannot be called for a
gated marker is 2 (drawn grey), never 1, and is counted apart. The probe checks
the viewer's side of it; the last test here checks the rule against the
server's own (`ProjectData.calls`, as numpy evaluates it).

That the shader draws the same pixels as renderLabelTile needs a browser:
tests/test_label_gpu_parity.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "label_gpu_probe.mjs"

#: Every line the probe prints, grouped as the probe groups them.
CHECKS = [
    # A. the viewer
    "on the CPU a decoded tile holds one canvas per layer (unchanged)",
    "the GPU is chosen when it is up and nothing asks otherwise",
    "moving to the GPU drops every layer canvas",
    "and computes each tile's fill weight once",
    "the fill weight is labelTile's own smallCellWeight",
    "on the GPU a newly decoded tile gets no canvases",
    "only its fill weight",
    "a gate re-render on the GPU marks only that layer for redraw",
    "and renders no pixels in JavaScript",
    "a full re-render marks every layer and core's",
    "a new colour table moves the layer's lutVersion and renderVersion",
    "showing a layer again marks it for redraw",
    "unregistering a layer frees its GPU tables",
    "setLabelRenderer('cpu') moves back",
    "rebuilding every tile's canvases",
    "and releasing the GPU's textures",
    "setLabelRenderer(null) returns to the default",
    "a software renderer takes the measured default",
    "an explicit preference wins over the default",
    "a GPU path that turned itself off means the CPU",
    "and the probes' viewers without a labelGpu stay on the CPU",
    "eviction also forgets the fill weight",
    "a range gate is evaluated in the browser",
    "the mask passes exactly the cells in range",
    "and the id list is not also kept",
    "the gate change marks the layer for redraw",
    "a column that does not line up with the ids falls back to the provider",
    "so does a column the server will not give",
    "a provider that does not declare a range gate is always asked",
    "QC's not-called cells are fetched beside the columns, per gated marker",
    "and are 2 in the layer's mask, a pass included",
    "counted on the layer apart from the positives",
    "and the viewer says the layer's gate moved",
    "not-called bytes that do not line up with the ids leave every cell called",
    "a not-called fetch that failed leaves the gate drawing in the browser",
    "a numericData without not-called bytes gates as before",
    "a QC change drops the cached not-called bytes at once",
    "and gates the layer again, once for a burst, with the new bytes",
    "a QC change on another project is not this tab's",
    "a layer with no gate drawn is not gated again",
    # B. labelGpu.js and the GL state it hands back
    "the program builds and the GPU path comes up",
    "and says so, so the viewer can move across",
    "a_uv is pinned to attribute 0 before the program links",
    "a tile draws",
    "afterwards the channel program is current again",
    "with the channel viewport",
    "unpack flip back on, as selectTexture expects",
    "and texture unit 0 active",
    "the draw is 1:1 into the canvas's bottom-left tile-sized corner",
    "and blitted from the matching canvas rows",
    "the tile's alpha tables are labelTile's, uploaded for its fill weight",
    "every texture it made is NEAREST (integer textures are otherwise incomplete)",
    "an id table wider than MAX_TEXTURE_SIZE wraps into rows",
    "filled as whole rows plus the remainder, without a padded copy",
    "drawing again with the same gate and tile uploads nothing",
    "a new gate is one table upload",
    "a colour table is uploaded when it arrives",
    "and not again while it is the same",
    "but again when its version moves (edited in place)",
    "a draw that throws reports false",
    "and still hands the context back to the channel program",
    "a sparse colour map becomes the dense shape",
    "a table reaching 2^24 ids does not draw",
    "and turns the GPU path off, telling the viewer",
    # C. the gate's rules
    "gate: the low bound is exclusive",
    "gate: the high bound is inclusive",
    "gate: a bound that is not a float32",
    "gate: two keys AND",
    "gate: an unknown key is skipped",
    "gate: only unknown keys pass everything",
    "gate: string bounds",
    "gate: NaN never passes",
    "gate: a repeated id is counted once, as a Set of the server's list is",
    "gate: the mask ends at the largest id",
    "gate: a not-called row is 2, counted apart from the passing ones",
    "gate: not called wins over a pass of the same id, whichever row comes first",
    "gate: float32 rounding of a bound matches numpy's for a halfway value",
    # D. the CPU reference
    "alpha tables match the CPU loop at fill 0",
    "alpha tables match the CPU loop at fill 0.25",
    "alpha tables match the CPU loop at fill 0.5",
    "alpha tables match the CPU loop at fill 0.7",
    "alpha tables match the CPU loop at fill 1",
    "the CPU loop draws a cell the gate mask passes",
    "and skips one it does not",
    "with no gate mask it draws every cell (unchanged)",
    "the CPU loop draws a not-called cell grey, at the plain alpha",
    "...whatever the colour table says, a hidden category included",
]


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)


def test_the_gpu_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert probe.stdout.count("PASS ") == len(CHECKS), probe.stdout


def test_the_gate_matches_numpy_on_real_float32_data():
    """The browser-side gate against the server's own function, on values where
    float32 rounding decides the answer: bounds that are not float32, values a
    hair either side of them, values exactly at the (inclusive) upper bound,
    NaN, and a two-key AND."""
    import json

    import numpy as np

    from plexora.server.models.data_model import apply_range_mask

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    rng = np.random.default_rng(7)
    n = 5000
    a = rng.normal(100, 30, n).astype(np.float32)
    a[::97] = np.nan
    a[1::101] = np.float32(0.1)          # exactly the float32 nearest 0.1
    a[2::103] = np.nextafter(np.float32(0.1), np.float32(1))
    # Exactly at an upper bound: the top is inclusive, so these pass -- and the
    # column maximum passes a gate whose high is that maximum.
    a[3::107] = np.float32(120.25)
    b = rng.exponential(5, n).astype(np.float32)
    ids = rng.permutation(np.arange(1, n + 1)).astype(np.uint32)
    cases = [
        {"A": [0.1, 120.3]},
        {"A": [float(np.float32(0.1)), 1e9]},
        {"A": [60.7, 140.05], "B": [1.1, 7.3]},
        {"A": [-1e9, 1e9], "Z": [0, 1]},
        {"A": [60.7, 120.25]},
        {"A": [60.7, float(np.nanmax(a))]},
    ]
    script = """
const fs = require("fs");
const path = require("path");
const views = path.join(process.cwd(), "plexora", "client", "src", "js", "views");
(0, eval)(fs.readFileSync(path.join(views, "labelGpu.js"), "utf8"));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ids = Uint32Array.from(input.ids);
const columns = {A: Float32Array.from(input.A.map((v) => v === null ? NaN : v)),
                 B: Float32Array.from(input.B)};
const out = input.cases.map((gates) => {
    const r = globalThis.PlexoraLabelGpu.evaluateGateMask(ids, columns, gates);
    return [...r.mask.keys()].filter((i) => r.mask[i]);
});
process.stdout.write(JSON.stringify(out));
"""
    payload = json.dumps({
        "ids": ids.tolist(),
        "A": [None if np.isnan(v) else float(v) for v in a],
        "B": [float(v) for v in b],
        "cases": cases,
    })
    proc = subprocess.run([node, "-e", script], input=payload, capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    columns = {"A": a, "B": b}
    for gates, browser in zip(cases, got):
        server = sorted(ids[apply_range_mask(columns, gates)].tolist())
        assert sorted(browser) == server, (gates, len(browser), len(server))


def test_a_not_called_cell_is_two_and_counted_apart():
    """QC's third state against the server's rule (`ProjectData.calls`: the
    range mask, then -1 wherever a gated marker is not called, whatever the
    value): on real float32 data, a not-called cell is 2 in the mask and never
    1 -- a cell that passes the gate included -- `count` is the cells called
    positive and `notCalledCount` the cells not called. A key the gates do not
    name says nothing, even with every cell not called."""
    import json

    import numpy as np

    from plexora.server.models.data_model import apply_range_mask

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    rng = np.random.default_rng(11)
    n = 4000
    a = rng.normal(100, 30, n).astype(np.float32)
    b = rng.exponential(5, n).astype(np.float32)
    ids = rng.permutation(np.arange(1, n + 1)).astype(np.uint32)
    skip = {"A": rng.random(n) < 0.1, "B": rng.random(n) < 0.05, "Z": np.ones(n, dtype=bool)}
    cases = [{"A": [60.7, 140.05]}, {"A": [60.7, 140.05], "B": [1.1, 7.3]}, {"B": [-1, 1e9]}]
    script = """
const fs = require("fs");
const path = require("path");
const views = path.join(process.cwd(), "plexora", "client", "src", "js", "views");
(0, eval)(fs.readFileSync(path.join(views, "labelGpu.js"), "utf8"));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ids = Uint32Array.from(input.ids);
const columns = {A: Float32Array.from(input.A), B: Float32Array.from(input.B)};
const notCalled = Object.fromEntries(
    Object.entries(input.skip).map(([key, rows]) => [key, Uint8Array.from(rows)]));
const out = input.cases.map((gates) => {
    const r = globalThis.PlexoraLabelGpu.evaluateGateMask(ids, columns, gates, notCalled);
    const ofValue = (value) => [...r.mask.keys()].filter((i) => r.mask[i] === value);
    return {positive: ofValue(1), notCalled: ofValue(2), count: r.count,
            notCalledCount: r.notCalledCount};
});
process.stdout.write(JSON.stringify(out));
"""
    payload = json.dumps({
        "ids": ids.tolist(),
        "A": [float(v) for v in a],
        "B": [float(v) for v in b],
        "skip": {key: mask.astype(int).tolist() for key, mask in skip.items()},
        "cases": cases,
    })
    proc = subprocess.run([node, "-e", script], input=payload, capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    columns = {"A": a, "B": b}
    for gates, browser in zip(cases, got):
        calls = np.where(apply_range_mask(columns, gates), 1, 0)
        for key in gates:
            calls[skip[key]] = -1
        positive = sorted(ids[calls == 1].tolist())
        not_called = sorted(ids[calls == -1].tolist())
        assert not_called, gates
        # Somewhere in the not-called cells is one the gate alone would pass.
        assert apply_range_mask(columns, gates)[calls == -1].any(), gates
        assert sorted(browser["positive"]) == positive, gates
        assert sorted(browser["notCalled"]) == not_called, gates
        assert browser["count"] == len(positive), gates
        assert browser["notCalledCount"] == len(not_called), gates
