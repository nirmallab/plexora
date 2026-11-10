/**
 * The website's workspace on disk (services/browserStore.js), against a fake
 * File System Access tree.
 *
 * What it has to get right, each a check below:
 *
 *   - **No nesting.** The folder the user picks is the workspace, whatever
 *     its name: `Imaging` becomes `Imaging/{workspace.json, projects,
 *     pyramids}` and never `Imaging/Plexora`, which the old `chooseHome` made
 *     and the desktop app never looked in.
 *   - **What a folder is**, read without writing: a workspace (its marker, or
 *     a projects/config.json), the library an old version made inside it
 *     (legacy nested), a workspace's own projects folder, empty, or other.
 *   - **Nothing half made.** A write probe before anything is created;
 *     workspace.json only where there is none; the choice remembered before
 *     Python is waited for, and Python moved last.
 *   - **Nothing asked on load.** `prepare` never prompts: a folder the
 *     browser no longer grants is left closed and reconnected from a click.
 *   - What the browser holds is merged in, but never one machine's caches.
 *
 * Run directly:  node tests/js/browser_store_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { webcrypto } from "node:crypto";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/browserStore.js"), "utf8");

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

// -- a File System Access tree ------------------------------------------------

function domError(name, message = name) {
    const error = new Error(message);
    error.name = name;
    return error;
}

class FakeFile {
    constructor(name, data = "", parent = null) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.parent = parent;
    }

    async getFile() {
        const data = this.data;
        return { name: this.name, size: data.length, async text() { return data; } };
    }

    async createWritable() {
        const root = this.parent && this.parent.root();
        if (root && root.readOnly) throw domError("NotAllowedError");
        let buffer = "";
        const file = this;
        return {
            async write(chunk) {
                if (root && root.full) throw domError("QuotaExceededError");
                buffer += chunk && typeof chunk.text === "function" ? await chunk.text() : String(chunk);
            },
            async close() { file.data = buffer; },
        };
    }

    async isSameEntry(other) { return other === this; }
}

class FakeDir {
    constructor(name, parent = null) {
        this.kind = "directory";
        this.name = name;
        this.parent = parent;
        this.children = new Map();
        this.permission = "granted";
        this.asked = 0;
        this.answer = "granted";
    }

    root() { return this.parent ? this.parent.root() : this; }

    async *entries() { for (const entry of [...this.children.entries()]) yield entry; }

    async *keys() { for (const name of [...this.children.keys()]) yield name; }

    async getDirectoryHandle(name, { create = false } = {}) {
        const found = this.children.get(name);
        if (found) {
            if (found.kind !== "directory") throw domError("TypeMismatchError");
            return found;
        }
        if (!create) throw domError("NotFoundError");
        if (this.root().readOnly) throw domError("NotAllowedError");
        const dir = new FakeDir(name, this);
        this.children.set(name, dir);
        return dir;
    }

    async getFileHandle(name, { create = false } = {}) {
        const found = this.children.get(name);
        if (found) {
            if (found.kind !== "file") throw domError("TypeMismatchError");
            return found;
        }
        if (!create) throw domError("NotFoundError");
        if (this.root().readOnly) throw domError("NotAllowedError");
        const file = new FakeFile(name, "", this);
        this.children.set(name, file);
        return file;
    }

    async removeEntry(name) {
        if (!this.children.delete(name)) throw domError("NotFoundError");
    }

    async queryPermission() { return this.permission; }

    async requestPermission() {
        this.asked += 1;
        this.permission = this.answer;
        return this.permission;
    }

    async isSameEntry(other) { return other === this; }

    // -- for the checks --
    dir(path) {
        return path.split("/").reduce((at, part) => {
            let next = at.children.get(part);
            if (!next) {
                next = new FakeDir(part, at);
                at.children.set(part, next);
            }
            return next;
        }, this);
    }

    file(path, data) {
        const parts = path.split("/");
        const name = parts.pop();
        const at = parts.length ? this.dir(parts.join("/")) : this;
        at.children.set(name, new FakeFile(name, data, at));
        return this;
    }

    has(path) {
        let at = this;
        for (const part of path.split("/")) {
            at = at && at.children ? at.children.get(part) : null;
            if (!at) return false;
        }
        return true;
    }

    read(path) {
        let at = this;
        for (const part of path.split("/")) at = at.children.get(part);
        return at.data;
    }

    names() { return [...this.children.keys()].sort(); }
}

// -- IndexedDB: one store, values kept by reference ---------------------------

function fakeIndexedDB() {
    const data = new Map();
    return {
        data,
        open() {
            const request = {};
            setTimeout(() => {
                request.result = {
                    transaction() {
                        const tx = {};
                        tx.objectStore = () => ({
                            get: (key) => ({ result: data.get(key) }),
                            put: (value, key) => {
                                data.set(key, value);
                                return { result: key };
                            },
                        });
                        setTimeout(() => tx.oncomplete && tx.oncomplete(), 0);
                        return tx;
                    },
                    close() {},
                };
                if (request.onsuccess) request.onsuccess();
            }, 0);
            return request;
        },
    };
}

/** A page with the store loaded: its OPFS, its IndexedDB, Python's half. */
function boot({ record = undefined, picker = null } = {}) {
    const opfs = new FakeDir("");
    const idb = fakeIndexedDB();
    if (record !== undefined) idb.data.set("data", record);
    const calls = [];
    const python = {
        flush: async () => { calls.push("flush"); },
        useStore: async (folder) => { calls.push(["useStore", folder]); return "folder"; },
    };
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Date, Math, Uint8Array,
        setTimeout, clearTimeout,
        indexedDB: idb,
        crypto: webcrypto,
        navigator: { storage: { getDirectory: async () => opfs, estimate: async () => ({ usage: 1, quota: 2 }) } },
        PlexoraPython: python,
        location: { reloaded: 0, reload() { this.reloaded += 1; } },
    };
    if (picker) context.showDirectoryPicker = picker;
    context.window = context;
    createContext(context);
    runInContext(SOURCE, context);
    return { store: context.PlexoraStore, context, opfs, idb, calls };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

// -- the checks -----------------------------------------------------------------

await check("the picked folder is returned as it is: nothing is made inside it", async () => {
    const research = new FakeDir("Research");
    const options = [];
    const { store } = boot({ picker: async (o) => { options.push(o); return research; } });
    const handle = await store.pickFolder({ startIn: "documents" });
    assert.equal(handle, research);
    assert.deepEqual(research.names(), []);
    assert.equal(options[0].mode, "readwrite");
    assert.equal(options[0].id, "plexora-data");
    assert.equal(options[0].startIn, "documents");
});

await check("a cancelled dialog is null; a refusal is `denied`; no dialog is `unsupported`", async () => {
    let { store } = boot({ picker: async () => { throw domError("AbortError"); } });
    assert.equal(await store.pickFolder(), null);
    ({ store } = boot({ picker: async () => { throw domError("NotAllowedError"); } }));
    await assert.rejects(store.pickFolder(), (error) => error.code === "denied");
    ({ store } = boot());
    await assert.rejects(store.pickFolder(), (error) => error.code === "unsupported");
});

await check("a folder with workspace.json is a workspace, with its id", async () => {
    const { store } = boot();
    const root = new FakeDir("Plexora").file("workspace.json", JSON.stringify({ schema: 1, id: "a".repeat(32) }));
    const look = await store.inspectFolder(root);
    assert.equal(look.kind, "workspace");
    assert.equal(look.workspaceId, "a".repeat(32));
    assert.deepEqual(Array.from(look.projects), []);
});

await check("a projects/config.json alone is a workspace too, its projects listed", async () => {
    const { store } = boot();
    const root = new FakeDir("Plexora")
        .file("projects/config.json", JSON.stringify({ tonsil: { title: "Tonsil panel", id: "1".repeat(32) },
                                                     lung: { path: "/x" } }))
        .file("projects/.figures/f1/figure.db", "x").file("projects/.figures/f2/figure.db", "x")
        .file("projects/.captures/captures.db", "x");
    const look = await store.inspectFolder(root);
    assert.equal(look.kind, "workspace");
    assert.deepEqual(JSON.parse(JSON.stringify(look.projects)),
                     [{ name: "tonsil", title: "Tonsil panel", id: "1".repeat(32) },
                      { name: "lung", title: "lung", id: null }]);
    assert.equal(look.figures, 2);
    assert.equal(look.captures, true);
    assert.equal(look.writable, true);
});

await check("the library an old version made inside is legacy nested, and named for both folders", async () => {
    const { store } = boot();
    const root = new FakeDir("Plexora").file("Plexora/projects/config.json", JSON.stringify({ tonsil: {} }));
    const look = await store.inspectFolder(root);
    assert.equal(look.kind, "legacy-nested");
    assert.equal(look.nested, root.children.get("Plexora"));
    assert.equal(look.label, "Plexora › Plexora");
    assert.equal(look.projects.length, 1);
});

await check("a workspace's own projects folder is told apart", async () => {
    const { store } = boot();
    const projects = new FakeDir("projects").file("config.json", JSON.stringify({ tonsil: {} })).dir("tonsil").parent;
    assert.equal((await store.inspectFolder(projects)).kind, "projects-inside");
});

await check("only the system's own files is empty; anything else is foreign", async () => {
    const { store } = boot();
    assert.equal((await store.inspectFolder(new FakeDir("New").file(".DS_Store", "x"))).kind, "empty");
    assert.equal((await store.inspectFolder(new FakeDir("Research").file("notes.txt", "x"))).kind, "foreign");
    // A random config.json is not mistaken for a registry.
    assert.equal((await store.inspectFolder(new FakeDir("Tool").file("config.json", "{\"a\": 1}"))).kind, "foreign");
});

await check("inspecting writes nothing", async () => {
    const { store } = boot();
    const root = new FakeDir("Research").file("notes.txt", "x");
    await store.inspectFolder(root);
    assert.deepEqual(root.names(), ["notes.txt"]);
});

await check("adopting a folder makes it the root: Imaging/{workspace.json, projects, pyramids}, no Imaging/Plexora", async () => {
    const imaging = new FakeDir("Imaging");
    const { store, idb, calls } = boot();
    const steps = [];
    const done = await store.adoptWorkspace(imaging, { onProgress: (step) => steps.push(step) });
    assert.deepEqual(imaging.names(), ["projects", "pyramids", "workspace.json"]);
    assert.ok(!imaging.has("Plexora"));
    const marker = JSON.parse(imaging.read("workspace.json"));
    assert.equal(marker.schema, 1);
    assert.match(marker.id, /^[0-9a-f]{32}$/);
    assert.match(marker.createdAt, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    assert.equal(marker.createdBy, "website");
    assert.equal(marker.name, "Imaging");
    const record = idb.data.get("data");
    assert.equal(record.kind, "folder");
    assert.equal(record.home, imaging);
    assert.equal(record.workspaceId, marker.id);
    assert.equal(record.name, "Imaging");
    assert.equal(record.label, "Imaging");
    assert.equal(record.chosen, true);
    assert.equal(record.onboarded, true);
    assert.equal(record.desktopFinds, false);
    assert.ok(Array.isArray(record.locations));
    assert.equal(done.desktopFinds, false);
    assert.equal(store.current().kind, "folder");
    assert.equal(store.current().label, "Imaging");
    assert.deepEqual(calls[0], "flush");
    assert.equal(calls.at(-1)[0], "useStore");
    assert.equal(calls.at(-1)[1], imaging.children.get("projects"));
    assert.deepEqual(steps, ["probe", "folders", "merge", "remember", "open", "done"]);
});

await check("a folder named Plexora is the one the desktop app finds", async () => {
    const { store } = boot();
    const done = await store.adoptWorkspace(new FakeDir("plexora"));
    assert.equal(done.desktopFinds, true);
    assert.equal(store.current().desktopFinds, true);
});

await check("workspace.json is written only where there is none", async () => {
    const { store } = boot();
    const text = JSON.stringify({ schema: 1, id: "b".repeat(32), createdBy: "desktop", name: "Plexora" });
    const root = new FakeDir("Plexora").file("workspace.json", text).file("projects/config.json", "{}");
    const done = await store.adoptWorkspace(root, { init: false });
    assert.equal(root.read("workspace.json"), text);
    assert.equal(done.workspaceId, "b".repeat(32));
    // A desktop-made library with no marker gets one; its registry is not touched.
    const registry = JSON.stringify({ tonsil: { id: "c".repeat(32), extra_key: 1 } });
    const desktop = new FakeDir("Plexora").file("projects/config.json", registry);
    await boot().store.adoptWorkspace(desktop, { init: false });
    assert.ok(desktop.has("workspace.json"));
    assert.equal(JSON.parse(desktop.read("projects/config.json")).tonsil.extra_key, 1);
});

await check("a legacy nested library is adopted where it is: nothing moved, the outer folder untouched", async () => {
    const { store, idb } = boot();
    const outer = new FakeDir("Plexora").file("Plexora/projects/config.json", JSON.stringify({ tonsil: {} }))
        .file("Plexora/projects/tonsil/tonsil.db", "x");
    const done = await store.adoptWorkspace(outer, { init: false });
    assert.deepEqual(outer.names(), ["Plexora"]);
    const inner = outer.children.get("Plexora");
    assert.ok(inner.has("workspace.json") && inner.has("projects/tonsil/tonsil.db") && inner.has("pyramids"));
    assert.equal(idb.data.get("data").home, inner);
    assert.equal(done.label, "Plexora › Plexora");
    assert.equal(done.desktopFinds, true);
});

await check("what the browser holds is merged in; a project of the same name there is kept; caches never", async () => {
    const { store, opfs } = boot();
    opfs.file("plexora/state/config.json", JSON.stringify({ mine: { title: "Mine" }, both: { title: "Browser's" } }))
        .file("plexora/state/mine/mine.db", "mine")
        .file("plexora/state/both/both.db", "browser")
        .file("plexora/state/datasets.json", "{}")
        .file("plexora/state/.remote_cache/chunk", "x")
        .file("plexora/state/.models/weights", "x")
        .file("plexora/state/server_token.json", "x")
        .file("plexora/state/.plexora-write-probe-1", "x");
    const root = new FakeDir("Plexora").file("projects/config.json", JSON.stringify({ both: { title: "Desktop's" } }))
        .file("projects/both/both.db", "desktop");
    const done = await store.adoptWorkspace(root, { init: false });
    const registry = JSON.parse(root.read("projects/config.json"));
    assert.equal(registry.mine.title, "Mine");
    assert.equal(registry.both.title, "Desktop's");
    assert.equal(root.read("projects/both/both.db"), "desktop");
    assert.equal(root.read("projects/mine/mine.db"), "mine");
    assert.ok(root.has("projects/datasets.json"));
    for (const kept of [".remote_cache", ".models", "server_token.json", ".plexora-write-probe-1"]) {
        assert.ok(!root.has(`projects/${kept}`), kept);
    }
    assert.deepEqual(Array.from(done.moved), ["mine"]);
});

await check("a folder that cannot be written is found before anything is made in it", async () => {
    const { store, idb } = boot();
    const root = new FakeDir("Shared");
    root.readOnly = true;
    await assert.rejects(store.adoptWorkspace(root), (error) => error.code === "not-writable");
    assert.deepEqual(root.names(), []);
    assert.equal(idb.data.get("data"), undefined);
});

await check("a full disk is `quota`", async () => {
    const { store } = boot();
    const root = new FakeDir("Plexora");
    root.full = true;
    await assert.rejects(store.adoptWorkspace(root), (error) => error.code === "quota");
});

await check("a folder that is not a workspace is not made one without `init`; a projects folder never", async () => {
    const { store } = boot();
    await assert.rejects(store.adoptWorkspace(new FakeDir("Research").file("a.txt", "x"), { init: false }),
                         (error) => error.code === "not-a-workspace");
    const projects = new FakeDir("projects").file("config.json", JSON.stringify({ tonsil: {} })).dir("tonsil").parent;
    await assert.rejects(store.adoptWorkspace(projects), (error) => error.code === "projects-inside");
});

await check("a folder the page may not change is asked for, and a refusal is `denied`", async () => {
    const { store } = boot();
    const root = new FakeDir("Plexora");
    root.permission = "prompt";
    root.answer = "denied";
    await assert.rejects(store.adoptWorkspace(root), (error) => error.code === "denied");
    assert.equal(root.asked, 1);
    assert.deepEqual(root.names(), []);
});

await check("the choice is remembered before Python is waited for; Python is moved once it has started", async () => {
    const { store, context, idb, calls } = boot();
    let started;
    context.PlexoraShellStarted = new Promise((resolve) => { started = resolve; });
    const root = new FakeDir("Plexora");
    const steps = [];
    const adopting = store.adoptWorkspace(root, { onProgress: (step) => steps.push(step) });
    for (let i = 0; i < 20 && !steps.includes("python"); i += 1) await tick();
    assert.ok(steps.includes("python"));
    assert.equal(idb.data.get("data").home, root);
    assert.ok(!calls.some((call) => Array.isArray(call) && call[0] === "useStore"));
    assert.ok(!calls.includes("flush"), "no flush before Python is up");
    started({});
    await adopting;
    assert.deepEqual(calls.slice(-2).map((call) => (Array.isArray(call) ? call[0] : call)), ["flush", "useStore"]);
});

await check("prepare never asks: no record is a first visit, and onboarding is due", async () => {
    const { store } = boot();
    assert.equal(await store.prepare(), null);
    assert.equal(store.needsOnboarding(), true);
    assert.equal(store.needsReconnect(), false);
});

await check("an old record keeps working: its folder opens, labelled as it was", async () => {
    const home = new FakeDir("Plexora");
    let { store } = boot({ record: { kind: "folder", home, name: "Plexora", parent: null } });
    assert.equal(await store.prepare(), home.children.get("projects"));
    assert.equal(store.current().label, "Plexora");
    assert.equal(store.current().desktopFinds, true);
    assert.equal(store.needsOnboarding(), false);
    const nested = new FakeDir("Plexora");
    ({ store } = boot({ record: { kind: "folder", home: nested, name: "Plexora", parent: "Research" } }));
    await store.prepare();
    assert.equal(store.current().label, "Research › Plexora");
    assert.equal(store.current().desktopFinds, false);
    ({ store } = boot({ record: { kind: "browser" } }));
    assert.equal(await store.prepare(), null);
    assert.equal(store.needsOnboarding(), false);
    assert.equal(store.current().kind, "browser");
});

await check("a folder the browser no longer grants is left closed, with no question, until a click", async () => {
    const home = new FakeDir("Plexora");
    home.permission = "prompt";
    const { store } = boot({ record: { kind: "folder", home, name: "Plexora", label: "Plexora", chosen: true } });
    assert.equal(await store.prepare((record) => { throw new Error("asked"); }), null);
    assert.equal(home.asked, 0);
    assert.equal(store.needsReconnect(), true);
    assert.equal(store.current().kind, "browser");
    assert.equal(store.current().notOpened, "Plexora");
    assert.equal(store.current().needsPermission, true);
});

await check("Reconnect asks the browser, brings this visit's work, and moves Python there", async () => {
    const home = new FakeDir("Plexora");
    home.permission = "prompt";
    const { store, opfs, calls } = boot({ record: { kind: "folder", home, name: "Plexora", label: "Plexora" } });
    opfs.file("plexora/state/config.json", JSON.stringify({ today: {} }));
    await store.prepare();
    const done = await store.reconnect();
    assert.equal(done.ok, true);
    assert.equal(home.asked, 1);
    assert.ok(JSON.parse(home.read("projects/config.json")).today);
    assert.equal(calls.at(-1)[1], home.children.get("projects"));
    assert.equal(store.current().kind, "folder");
    assert.equal(store.needsReconnect(), false);
    // Refused: said so, nothing moved.
    const other = new FakeDir("Plexora");
    other.permission = "prompt";
    other.answer = "denied";
    const second = boot({ record: { kind: "folder", home: other, name: "Plexora" } });
    await second.store.prepare();
    assert.deepEqual(JSON.parse(JSON.stringify(await second.store.reconnect())), { ok: false, reason: "denied" });
    assert.deepEqual(second.calls, []);
});

await check("Continue with browser storage sets the folder aside for this visit only", async () => {
    const home = new FakeDir("Plexora");
    home.permission = "prompt";
    const { store, idb } = boot({ record: { kind: "folder", home, name: "Plexora" } });
    await store.prepare();
    store.continueInBrowser();
    assert.equal(store.needsReconnect(), false);
    assert.equal(idb.data.get("data").kind, "folder");
});

await check("browser storage is made and written to once, then remembered as the choice", async () => {
    const { store, opfs, idb } = boot();
    const prepared = await store.prepareBrowser();
    assert.equal(prepared.probe, "ok");
    assert.ok(opfs.has("plexora/state"));
    assert.deepEqual(opfs.dir("plexora/state").names(), []);
    await store.useBrowser();
    const record = idb.data.get("data");
    assert.equal(record.kind, "browser");
    assert.equal(record.chosen, true);
    assert.equal(record.onboarded, true);
    assert.equal(store.needsOnboarding(), false);
});

await check("useFolder and switchTo go through the same adoption", async () => {
    const imaging = new FakeDir("Imaging");
    let { store, context } = boot({ picker: async () => imaging });
    const done = await store.useFolder(null);
    assert.equal(done.label, "Imaging");
    assert.ok(imaging.has("workspace.json") && !imaging.has("Plexora"));
    const other = new FakeDir("Plexora");
    ({ store, context } = boot({ picker: async () => other }));
    await store.switchTo("folder");
    assert.ok(other.has("workspace.json"));
    assert.equal(context.location.reloaded, 1);
    ({ store, context } = boot({ picker: async () => new FakeDir("Plexora") }));
    await store.switchTo("folder", null, { reload: false });
    assert.equal(context.location.reloaded, 0);
});

await check("other workspaces are kept by handle: added once, listed after the default, removed", async () => {
    const home = new FakeDir("Plexora");
    const { store, idb } = boot({ record: { kind: "folder", home, name: "Plexora", label: "Plexora",
                                            workspaceId: "d".repeat(32) } });
    await store.prepare();
    const lab = new FakeDir("Lab").file("projects/config.json", JSON.stringify({ shared: {} }));
    const added = await store.addLocation({ handle: lab });
    assert.equal(added.added, true);
    assert.match(added.location.id, /^[0-9a-f]{12}$/);
    assert.ok(lab.has("workspace.json"));
    assert.equal((await store.addLocation({ handle: lab })).reason, "already");
    const loose = new FakeDir("Loose").file("a.txt", "x");
    assert.equal((await store.addLocation({ handle: loose })).reason, "not-a-workspace");
    assert.ok(!loose.has("workspace.json"));
    const listed = await store.locations();
    assert.deepEqual(JSON.parse(JSON.stringify(listed.map((row) => [row.label, row.isDefault]))),
                     [["Plexora", true], ["Lab", false]]);
    assert.equal(idb.data.get("data").locations.length, 1);
    await assert.rejects(store.removeLocation("d".repeat(32)), (error) => error.code === "default");
    await store.removeLocation(added.location.id);
    assert.equal(idb.data.get("data").locations.length, 0);
});

await check("making another workspace the default moves Python there and keeps the old one listed", async () => {
    const home = new FakeDir("Plexora");
    const { store, idb, calls } = boot({ record: { kind: "folder", home, name: "Plexora", label: "Plexora",
                                                   workspaceId: "e".repeat(32) } });
    await store.prepare();
    const lab = new FakeDir("Lab").file("projects/config.json", JSON.stringify({ shared: {} }));
    const { location } = await store.addLocation({ handle: lab });
    await store.setDefault(location.id);
    const record = idb.data.get("data");
    assert.equal(record.home, lab);
    assert.equal(record.locations.length, 1);
    assert.equal(record.locations[0].handle, home);
    assert.equal(calls.at(-1)[1], lab.children.get("projects"));
    assert.equal(store.current().label, "Lab");
    // Not merged: the old default's projects stay where they are.
    assert.deepEqual(JSON.parse(JSON.stringify(Object.keys(JSON.parse(lab.read("projects/config.json"))))), ["shared"]);
});

console.log(`${checks} checks passed`);
