"""The website's workspace folder (client services/browserStore.js): the folder
the user picks IS the workspace, whatever it is called. Nothing is made inside it.

Until 2026-11-09 `chooseHome` made a `Plexora` folder inside any pick not named
Plexora, so a pick of `Documents/Research` became `Research/Plexora`, and a pick
of `Documents/Plexora` whose name did not match exactly became
`Plexora/Plexora` -- which this Mac still has, and which the desktop app (it
looks at `Documents/Plexora/projects`) never saw. Now `pickFolder` returns the
pick and `adoptWorkspace` writes `workspace.json`, `projects/` and `pyramids/`
into it; tests/js/browser_store_probe.mjs checks the rest (what a folder is, the
legacy nested library adopted where it is, the order of the writes).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "plexora" / "client" / "src" / "js" / "services" / "browserStore.js"

#: The page, as little of it as the store needs, and a folder for the picker
#: to hand back: what is in it afterwards, and where the store says it is.
SCRIPT = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const picked = process.argv[2];
const err = (name) => Object.assign(new Error(name), { name });
function dir(name) {
    const children = new Map();
    const self = {
        kind: "directory", name, children,
        async *entries() { yield* children.entries(); },
        async getDirectoryHandle(n, o = {}) {
            if (children.has(n)) return children.get(n);
            if (!o.create) throw err("NotFoundError");
            const d = dir(n); children.set(n, d); return d;
        },
        async getFileHandle(n, o = {}) {
            if (children.has(n)) return children.get(n);
            if (!o.create) throw err("NotFoundError");
            const f = { kind: "file", name: n, data: "",
                        async getFile() { const d = f.data; return { size: d.length, text: async () => d }; },
                        async createWritable() { let b = ""; return { write: async (c) => { b += c; }, close: async () => { f.data = b; } }; } };
            children.set(n, f); return f;
        },
        async removeEntry(n) { children.delete(n); },
        async queryPermission() { return "granted"; },
        async requestPermission() { return "granted"; },
        async isSameEntry(o) { return o === self; },
    };
    return self;
}
const home = dir(picked);
const opfs = dir("");
const kept = new Map();
const idb = { open() { const r = {}; setTimeout(() => { r.result = { transaction() { const t = { objectStore: () => ({
    get: (k) => ({ result: kept.get(k) }), put: (v, k) => { kept.set(k, v); return { result: k }; } }) };
    setTimeout(() => t.oncomplete && t.oncomplete(), 0); return t; }, close() {} }; r.onsuccess(); }, 0); return r; } };
const context = { console, setTimeout, Promise, Map, Set, JSON, Error, Object, Array, String, Uint8Array, Date,
                  indexedDB: idb, crypto: require("node:crypto").webcrypto,
                  navigator: { storage: { getDirectory: async () => opfs } },
                  showDirectoryPicker: async () => home,
                  PlexoraPython: { flush: async () => {}, useStore: async () => "folder" } };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
(async () => {
    const store = context.PlexoraStore;
    const handle = await store.pickFolder({ startIn: "documents" });
    const done = await store.adoptWorkspace(handle, { init: true });
    console.log(JSON.stringify({ same: handle === home, inside: [...home.children.keys()].sort(),
                                 home: kept.get("data").home === home, label: done.label,
                                 marker: JSON.parse(home.children.get("workspace.json").data).name }));
})();
"""


def _adopt(picked):
    done = subprocess.run(["node", "-e", SCRIPT, str(STORE), picked], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("picked", ["Plexora", "plexora", "PLEXORA", "Research", "Imaging"])
def test_the_picked_folder_is_the_workspace_and_nothing_is_nested(picked):
    if not shutil.which("node"):
        pytest.skip("needs node")
    seen = _adopt(picked)
    assert seen["same"] and seen["home"]
    assert seen["inside"] == ["projects", "pyramids", "workspace.json"]
    assert seen["label"] == picked
    assert seen["marker"] == picked


def test_the_old_nesting_branch_is_gone():
    source = STORE.read_text(encoding="utf-8")
    assert "chooseHome" not in source
    assert "getDirectoryHandle(FOLDER, { create: true })" not in source
