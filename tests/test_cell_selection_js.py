"""The app-wide cell selection and the ways it is drawn, in node.

`services/cellSelection.js` is core's one selection (project + mask label),
`cellExplorerSelection.js` turns it into Cell Explorer's colour table
(Highlight / Dim others / Only), and `scimapEmbed.js` sends a selection to the
server as a bitmask and reads the server's binary answers. Each is checked
here on its pure parts; the page wiring is driven in the real app.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUS = ROOT / "plexora" / "client" / "src" / "js" / "services" / "cellSelection.js"
WINDOW = ROOT / "plexora" / "client" / "src" / "js" / "views" / "canvasWindow.js"
COMPOSE = ROOT / "plexora" / "plugins" / "cell_explorer" / "static" / "cellExplorerSelection.js"
EMBED = ROOT / "plexora" / "plugins" / "scimap" / "static" / "scimapEmbed.js"
PICKER = ROOT / "plexora" / "plugins" / "scimap" / "static" / "scimapPicker.js"
SCATTER = ROOT / "plexora" / "plugins" / "scimap" / "static" / "scimapScatterGL.js"
DROPDOWN = ROOT / "plexora" / "client" / "src" / "js" / "views" / "plexoraSelect.js"

PROBE = r"""
const events = [];
globalThis.window = globalThis;
globalThis.CustomEvent = class { constructor(name, init) { this.type = name; this.detail = init && init.detail; } };
globalThis.dispatchEvent = (e) => { events.push(e.type); return true; };
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } };
const bus = require(process.argv[1]);
globalThis.CellExplorerColors = { DENSE_MAX_ID: 8000000 };
const CellExplorerSelection = require(process.argv[2]);
globalThis.document = { getElementById: () => null };
require(process.argv[3]);
const out = {};

out.normalise = Array.from(bus.normalise([5, 3, 0, 3, 9]));
const rev = bus.set({ A: [4, 2, 2], B: [] }, { scope: ["A", "B", "C"], source: "test" });
out.rev = rev;
out.getA = Array.from(bus.get("A"));
out.getB = Array.from(bus.get("B"));
out.getElsewhere = bus.get("Z");
out.has = [bus.has("A", 2), bus.has("A", 3), bus.has("C", 2)];
out.count = [bus.count(), bus.count("A")];
bus.setMode("only");
bus.setMode("nonsense");
out.mode = bus.mode();
out.storedMode = store["plexora:cell-selection-mode"];
bus.setRegion("A", [7, 1], "Follicle");
out.region = { project: bus.region().project, ids: Array.from(bus.region().ids), name: bus.region().name };
bus.setRegion(null);
out.regionCleared = bus.region();
bus.clear();
out.cleared = bus.current();
out.events = events.slice();

// One history for every view: undo walks back a clear and a replace, redo
// forward; a new change drops what could be redone.
bus.set({ A: [1, 2] }, { scope: ["A"] });
bus.set({ A: [3] }, { scope: ["A"] });
bus.clear();
const hist = [];
bus.undo(); hist.push(Array.from(bus.get("A")));
bus.undo(); hist.push(Array.from(bus.get("A")));
out.canRedo = bus.canRedo();
bus.redo(); hist.push(Array.from(bus.get("A")));
bus.set({ A: [9] }, { scope: ["A"] });
out.redoAfterChange = bus.canRedo();
bus.undo(); hist.push(Array.from(bus.get("A")));
out.history = hist;
let back = 0;
while (bus.canUndo() && back < 100) { bus.undo(); back += 1; }
out.undoneToStart = bus.current();

// Cell Explorer's table: ids 1..4, every cell red; 2 and 4 selected.
const colors = new Uint8Array(4 * 5);
for (let id = 1; id <= 4; id += 1) colors.set([200, 0, 0, 255], id * 4);
const lut = { colors, maxId: 4 };
const sel = new Uint32Array([2, 4]);
const rgba = (t, id) => Array.from(t.colors.subarray(id * 4, id * 4 + 4));
const only = CellExplorerSelection.compose(lut, sel, "only");
out.only = [rgba(only, 1), rgba(only, 2)];
const dim = CellExplorerSelection.compose(lut, sel, "dim");
out.dim = [rgba(dim, 1), rgba(dim, 2)];
const high = CellExplorerSelection.compose(lut, sel, "highlight");
out.highlight = [rgba(high, 1), rgba(high, 2)];
out.untouched = rgba(lut, 2);
const bare = CellExplorerSelection.compose(null, sel, "dim");
out.bare = [rgba(bare, 1), rgba(bare, 2)];
out.bareOnlyEmpty = CellExplorerSelection.compose(null, new Uint32Array(0), "only").map.size;
const sparse = CellExplorerSelection.compose({ map: new Map([[1, [9, 9, 9, 255]], [2, [9, 9, 9, 255]]]) }, sel, "only");
out.sparseOnly = Array.from(sparse.map.keys()).sort();

// The embed's wire formats.
const E = globalThis.ScimapEmbed;
const rows = new Uint8Array(10); rows[0] = 1; rows[3] = 1; rows[9] = 1;
out.bitmask = Buffer.from(E.bitmaskBase64(rows), "base64").toJSON().data;
const head = Buffer.from(JSON.stringify({ n: 2, arrays: [{ name: "v", dtype: "uint16", length: 2, offset: 0 }] }));
const pad = (4 - ((8 + head.length) % 4)) % 4;
const buf = Buffer.concat([Buffer.from("PXB1"), Buffer.from(new Uint32Array([head.length + pad]).buffer),
                           head, Buffer.alloc(pad, 32), Buffer.from(new Uint16Array([7, 65535]).buffer)]);
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length);
const got = E.unpack(ab);
out.unpack = { n: got.meta.n, v: Array.from(got.arrays.v) };
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", PROBE, str(BUS), str(COMPOSE), str(EMBED)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("path", [BUS, WINDOW, COMPOSE, EMBED, PICKER, SCATTER, DROPDOWN])
def test_syntax(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_ids_are_sorted_unique_and_never_background(result):
    assert result["normalise"] == [3, 5, 9]


def test_a_selection_speaks_for_its_scope(result):
    assert result["getA"] == [2, 4]
    # In scope with nothing chosen is "none here", not "no selection".
    assert result["getB"] == []
    assert result["getElsewhere"] is None
    assert result["has"] == [True, False, False]
    assert result["count"] == [2, 2]


def test_mode_and_region(result):
    assert result["mode"] == "only" and result["storedMode"] == "only"
    assert result["region"] == {"project": "A", "ids": [1, 7], "name": "Follicle"}
    assert result["regionCleared"] is None
    assert result["cleared"] is None
    assert result["events"].count("plexora:cells-selected") == 2
    assert "plexora:cells-selection-mode" in result["events"]
    assert result["events"].count("plexora:cells-region") == 2


def test_the_tissue_shows_the_selection_three_ways(result):
    white = [255, 255, 255, 255]
    assert result["only"] == [[0, 0, 0, 0], [200, 0, 0, 255]]
    dimmed, selected = result["dim"]
    assert selected == white and dimmed[0] < 80 and dimmed[3] <= 150
    assert result["highlight"] == [[200, 0, 0, 255], white]
    # Cell Explorer's own table is never changed in place.
    assert result["untouched"] == [200, 0, 0, 255]
    # Nothing coloured by a column: the selection alone, in white.
    assert result["bare"] == [[0, 0, 0, 0], white]
    assert result["bareOnlyEmpty"] == 0
    assert result["sparseOnly"] == [2, 4]


def test_the_wire_formats(result):
    # Little-endian bits: rows 0, 3 and 9.
    assert result["bitmask"] == [0b00001001, 0b00000010]
    assert result["unpack"] == {"n": 2, "v": [7, 65535]}


def test_undo_and_redo_are_one_history_for_every_view(result):
    assert result["history"] == [[3], [1, 2], [3], [3]]
    assert result["canRedo"] is True
    assert result["redoAfterChange"] is False
    # Undo goes all the way back to before the first selection.
    assert result["undoneToStart"] is None
