// The website's one index of the copies its page makes of the user's files
// (client services/derivedIndex.js, `window.PlexoraDerived`): version 1's
// records moved into version 2; the site's storage and Plexora's folder
// scanned for copies nobody recorded; copies the browser cleared forgotten;
// a copy in a folder not open this visit *waiting* (and Python told so);
// one lookup order over every home; the site's copies moved into Plexora's
// folder and removed from the site's storage on the next visit.
//
// An IndexedDB, OPFS and folder handles small enough to read stand in for the
// browser's; the script runs as the host page loads it (node:vm).
//
//   node tests/js/derived_index_probe.mjs
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/derivedIndex.js"), "utf8");

let clock = 1_700_000_000_000;
const tick = () => (clock += 7);
const bytes = (text) => new TextEncoder().encode(text);

// -- files and folders (OPFS and the File System Access API) ---------------
class FakeFile {
    constructor(data, name, lastModified) {
        this.data = data;
        this.name = name;
        this.size = data.length;
        this.lastModified = lastModified;
    }

    async text() { return new TextDecoder().decode(this.data); }

    stream() {
        const data = this.data;
        return { async pipeTo(writable) { await writable.write(data); await writable.close(); } };
    }
}

class FileHandle {
    constructor(name, data = new Uint8Array()) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.lastModified = tick();
    }

    async getFile() { return new FakeFile(this.data, this.name, this.lastModified); }

    async createWritable() {
        const chunks = [];
        const handle = this;
        return {
            async write(chunk) { chunks.push(typeof chunk === "string" ? bytes(chunk) : chunk); },
            async close() {
                const total = chunks.reduce((n, c) => n + c.length, 0);
                const out = new Uint8Array(total);
                let at = 0;
                for (const c of chunks) { out.set(c, at); at += c.length; }
                handle.data = out;
                handle.lastModified = tick();
            },
            async abort() {},
        };
    }
}

class DirHandle {
    constructor(name, permission = "granted") {
        this.kind = "directory";
        this.name = name;
        this.children = new Map();
        this.permission = permission;
        this.asked = 0;
    }

    async queryPermission() { return this.permission; }

    async requestPermission() {
        this.asked += 1;
        if (this.permission === "prompt") this.permission = "granted";
        return this.permission;
    }

    async *entries() { for (const entry of [...this.children]) yield entry; }

    keys() { return (async function* (names) { for (const n of names) yield n; })([...this.children.keys()]); }

    async getDirectoryHandle(name, { create = false } = {}) {
        let child = this.children.get(name);
        if (!child && create) this.children.set(name, (child = new DirHandle(name)));
        if (!child || child.kind !== "directory") throw Object.assign(new Error(name), { name: "NotFoundError" });
        return child;
    }

    async getFileHandle(name, { create = false } = {}) {
        if (this.permission !== "granted") throw Object.assign(new Error("not allowed"), { name: "NotAllowedError" });
        let child = this.children.get(name);
        if (!child && create) this.children.set(name, (child = new FileHandle(name)));
        if (!child || child.kind !== "file") throw Object.assign(new Error(name), { name: "NotFoundError" });
        return child;
    }

    async removeEntry(name) { this.children.delete(name); }

    put(rel, data) {
        const parts = rel.split("/");
        let dir = this;
        for (const part of parts.slice(0, -1)) {
            if (!dir.children.has(part)) dir.children.set(part, new DirHandle(part));
            dir = dir.children.get(part);
        }
        const file = new FileHandle(parts.at(-1), typeof data === "string" ? bytes(data) : data);
        dir.children.set(parts.at(-1), file);
        return file;
    }

    at(rel) {
        let node = this;
        for (const part of rel.split("/")) node = node && node.kind === "directory" ? node.children.get(part) : null;
        return node || null;
    }
}

// A finished copy in the site's storage, as workers/pyramidWorker.js leaves it.
function opfsCopy(opfs, key, name, data) {
    const file = opfs.put(`plexora/derived/${key}/${name}`, data);
    opfs.put(`plexora/derived/${key}/manifest.json`, JSON.stringify({ key, name, kind: "image", bytes: file.data.length }));
    return file;
}

// -- IndexedDB, as far as the index uses it ----------------------------------
function fakeIndexedDB() {
    const databases = new Map(); // name -> {version, stores: Map(name -> {keyPath, rows: Map})}
    const later = (fn) => setTimeout(fn, 0);
    function storeApi(store, pending) {
        const request = (compute) => {
            const req = { result: undefined, onsuccess: null, onerror: null };
            pending.count += 1;
            later(() => {
                req.result = compute();
                if (req.onsuccess) req.onsuccess();
                pending.count -= 1;
                pending.done();
            });
            return req;
        };
        return {
            get: (key) => request(() => store.rows.get(key)),
            getAll: () => request(() => [...store.rows.values()]),
            put: (value) => request(() => { store.rows.set(value[store.keyPath], value); return value[store.keyPath]; }),
            delete: (key) => request(() => { store.rows.delete(key); }),
        };
    }
    function dbApi(db, tx) {
        return {
            objectStoreNames: { contains: (name) => db.stores.has(name) },
            createObjectStore(name, { keyPath }) { db.stores.set(name, { keyPath, rows: new Map() }); },
            deleteObjectStore(name) { db.stores.delete(name); },
            transaction(name, mode) {
                const pending = { count: 0, done: () => {} };
                const transaction = { oncomplete: null, onerror: null,
                                      objectStore: (n) => storeApi(db.stores.get(n), pending) };
                pending.done = () => {
                    if (pending.count === 0) later(() => { if (pending.count === 0 && transaction.oncomplete) transaction.oncomplete(); });
                };
                later(() => pending.done());
                return transaction;
            },
            close() {},
            _tx: tx,
        };
    }
    return {
        databases,
        open(name, version) {
            const req = { result: null, transaction: null, onsuccess: null, onerror: null, onupgradeneeded: null };
            later(() => {
                let db = databases.get(name);
                const oldVersion = db ? db.version : 0;
                if (!db) databases.set(name, (db = { version: 0, stores: new Map() }));
                const api = dbApi(db);
                req.result = api;
                if (version > oldVersion) {
                    db.version = version;
                    const pending = { count: 0, done: () => {} };
                    req.transaction = { objectStore: (n) => storeApi(db.stores.get(n), pending) };
                    let finished = false;
                    pending.done = () => {
                        if (pending.count === 0 && !finished) {
                            finished = true;
                            later(() => req.onsuccess && req.onsuccess());
                        }
                    };
                    if (req.onupgradeneeded) req.onupgradeneeded({ oldVersion });
                    if (pending.count === 0) pending.done();
                } else {
                    later(() => req.onsuccess && req.onsuccess());
                }
            });
            return req;
        },
    };
}

// -- the host page -------------------------------------------------------------
function page({ idb, opfs, store, timers = [] }) {
    const mounted = [];
    const adopted = [];
    const answerers = new Map();
    const window = {
        indexedDB: idb,
        navigator: { storage: { getDirectory: async () => opfs } },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        PlexoraStore: store,
        PlexoraPython: { answers: (kind, fn) => answerers.set(kind, fn),
                         mount: async (dir, files) => { mounted.push([dir, files.map((f) => f.name)]); } },
        PlexoraPicks: { adopt: (path) => adopted.push(path) },
        File: class { constructor(parts, name, options = {}) { this.parts = parts; this.name = name; this.size = parts[0].size; this.lastModified = options.lastModified; } },
    };
    window.window = window;
    const context = createContext({ window, setTimeout, console, Promise, Map, Set, JSON, String, Boolean, Number, Error, Object, Date });
    runInContext(SOURCE, context);
    return { derived: window.PlexoraDerived, mounted, adopted, answerers, timers };
}

function plexoraStore({ open = true, chosen = true, pyramids }) {
    const state = { open, reopened: 0 };
    return {
        state,
        pyramids: () => (state.open ? pyramids : null),
        current: () => (state.open ? { kind: "folder", chosen: true, label: "Documents › Plexora" }
            : chosen ? { kind: "browser", chosen: true, notOpened: "Documents › Plexora" } : { kind: "browser", chosen: true }),
        reopen: async () => { state.reopened += 1; state.open = true; return true; },
    };
}

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: Boolean(ok), detail });

const K1 = "a".repeat(24);
const K2 = "b".repeat(24);
const K3 = "c".repeat(24);
const K4 = "d".repeat(24);
const K5 = "e".repeat(24);

// 1. Version 1's records, the site's storage and Plexora's folder, at boot.
const idb = fakeIndexedDB();
const opfs = new DirHandle("opfs-root");
const home = new DirHandle("Plexora");
const pyramids = await home.getDirectoryHandle("pyramids", { create: true });
const beside = new DirHandle("data", "prompt");
beside.put("slide.pyramid.ome.tiff", "beside copy");
// Version 1, as browserPyramid.js wrote it before the index.
{
    const v1 = await new Promise((resolve) => {
        const req = idb.open("plexora-copies", 1);
        req.onupgradeneeded = () => req.result.createObjectStore("copies", { keyPath: "path" });
        req.onsuccess = () => resolve(req.result);
    });
    const pyramidsKey = await pyramids.getDirectoryHandle(K2, { create: true });
    pyramidsKey.put("lung.pyramid.ome.tiff", "plexora copy");
    await new Promise((resolve) => {
        const tx = v1.transaction("copies", "readwrite");
        const rows = tx.objectStore("copies");
        rows.put({ path: `/derived/${K2}/lung.pyramid.ome.tiff`, key: K2, name: "lung.pyramid.ome.tiff",
                   folder: pyramidsKey, bytes: 12, made: 1 });
        rows.put({ path: `/derived/${K3}/slide.pyramid.ome.tiff`, key: K3, name: "slide.pyramid.ome.tiff",
                   folder: beside, bytes: 11, made: 2 });
        tx.oncomplete = resolve;
    });
}
opfsCopy(opfs, K1, "liver.pyramid.ome.tiff", "opfs copy");
// A record of a copy the browser has since cleared (Safari after a week unused).
const store = plexoraStore({ pyramids });
let visit = page({ idb, opfs, store });
const first = await visit.derived.prepare();
const db = idb.databases.get("plexora-copies");
check("v1 store moved into v2 and dropped", db.version === 2 && db.stores.has("derived") && !db.stores.has("copies"));
const rows = db.stores.get("derived").rows;
check("v1 folder record of Plexora's pyramids is home plexora, no handle kept",
      rows.get(`/derived/${K2}/lung.pyramid.ome.tiff`)?.home === "plexora"
      && !rows.get(`/derived/${K2}/lung.pyramid.ome.tiff`)?.folder);
check("v1 record of a chosen folder is home beside", rows.get(`/derived/${K3}/slide.pyramid.ome.tiff`)?.home === "beside");
check("the site's copy nobody recorded is recorded", rows.get(`/derived/${K1}/liver.pyramid.ome.tiff`)?.home === "opfs");
check("ready: the site's and Plexora's", first.ready.includes(`/derived/${K1}/liver.pyramid.ome.tiff`)
      && first.ready.includes(`/derived/${K2}/lung.pyramid.ome.tiff`), JSON.stringify(first));
check("waiting: a folder the browser asks about again", first.waiting.includes(`/derived/${K3}/slide.pyramid.ome.tiff`)
      && beside.asked === 0, JSON.stringify(first));

// 2. What Python is told (providers/browser.py derived_index).
const told = (await visit.answerers.get("derived_index")({})).value.copies;
check("Python hears waiting and ready", told[`/derived/${K3}/slide.pyramid.ome.tiff`]?.state === "waiting"
      && told[`/derived/${K1}/liver.pyramid.ome.tiff`]?.state === "ready");

// 3. After Python starts: folder copies mounted, every ready copy adopted.
const restored = await visit.derived.restore();
check("folder copies mounted for Python; the site's left to the worker",
      visit.mounted.some(([dir]) => dir === `/derived/${K2}`) && !visit.mounted.some(([dir]) => dir === `/derived/${K1}`),
      JSON.stringify(visit.mounted));
check("every ready copy is the page's file", visit.adopted.includes(`/derived/${K1}/liver.pyramid.ome.tiff`)
      && restored.length === 2, JSON.stringify(restored));
check("a move into Plexora's folder is scheduled, not run at once", visit.timers.some((t) => t.ms >= 10000));

// 4. Lookup order: the index first, then Plexora's folder, then the site's storage.
const indexed = await visit.derived.lookup(K2, "lung.pyramid.ome.tiff");
check("lookup: an indexed copy is trusted", indexed?.trusted === true && indexed.home === "plexora");
const stray = await pyramids.getDirectoryHandle(K4, { create: true });
stray.put("kidney.pyramid.ome.tiff", "found, not recorded");
const found = await visit.derived.lookup(K4, "kidney.pyramid.ome.tiff");
check("lookup: a file merely found in Plexora's folder is Python's to judge", found && !found.trusted && found.home === "plexora");
check("lookup: nothing anywhere is null", (await visit.derived.lookup(K5, "none.pyramid.ome.tiff")) === null);
const browserOnly = await visit.derived.lookup(K2, "lung.pyramid.ome.tiff", { browserOnly: true });
check("lookup: browserOnly skips the folders", browserOnly === null);

// 5. One click: the waiting folder asked for, its copy mounted.
const again = await visit.derived.reconnect();
check("reconnect asks the folder and mounts its copy", beside.asked === 1
      && again.mounted.includes(`/derived/${K3}/slide.pyramid.ome.tiff`) && again.waiting.length === 0, JSON.stringify(again));

// 6. The site's copy moved into Plexora's folder; its manifest gone now, its file next visit.
const moved = await visit.derived.migrate();
check("migrate moves the site's copy into Plexora's folder", moved === 1
      && pyramids.at(`${K1}/liver.pyramid.ome.tiff`) && rows.get(`/derived/${K1}/liver.pyramid.ome.tiff`)?.home === "plexora"
      && rows.get(`/derived/${K1}/liver.pyramid.ome.tiff`)?.movedFrom === "opfs");
check("the site's copy loses its manifest, keeps its file this visit",
      !opfs.at(`plexora/derived/${K1}/manifest.json`) && Boolean(opfs.at(`plexora/derived/${K1}/liver.pyramid.ome.tiff`)));
check("migrate is idempotent", (await visit.derived.migrate()) === 0);

// 7. The next visit: the moved copy's leftovers removed; the projects' folder
// not open this time ("Use browser this time") -- Plexora's copies wait.
const closed = plexoraStore({ open: false, pyramids });
visit = page({ idb, opfs, store: closed });
const second = await visit.derived.prepare();
check("next visit removes what a move left in the site's storage", !opfs.at(`plexora/derived/${K1}`));
check("Plexora's copies wait while its folder is not open", second.waiting.includes(`/derived/${K1}/liver.pyramid.ome.tiff`)
      && second.waiting.includes(`/derived/${K2}/lung.pyramid.ome.tiff`), JSON.stringify(second));
const listed = await visit.derived.list();
check("Settings lists every copy with its place", listed.length === 3
      && listed.find((c) => c.path.endsWith("lung.pyramid.ome.tiff"))?.place === "Documents › Plexora", JSON.stringify(listed));
const back = await visit.derived.reconnect();
check("one click opens Plexora's folder again (reopen) and mounts its copies", closed.state.reopened === 1
      && back.mounted.includes(`/derived/${K2}/lung.pyramid.ome.tiff`), JSON.stringify(back));

// 8. A copy recorded, then the browser cleared the site's storage: forgotten.
opfsCopy(opfs, K5, "spleen.pyramid.ome.tiff", "x");
visit = page({ idb, opfs, store });
await visit.derived.prepare();
opfs.at("plexora/derived").children.delete(K5);
visit = page({ idb, opfs, store });
await visit.derived.prepare();
check("a copy the browser cleared is forgotten", !rows.has(`/derived/${K5}/spleen.pyramid.ome.tiff`));

// 9. Settings' delete: Plexora's own copy removed and forgotten; reload said.
const removed = await visit.derived.remove(`/derived/${K2}/lung.pyramid.ome.tiff`);
check("delete removes Plexora's copy and asks for a restart", removed.reload === true
      && !pyramids.at(`${K2}`) && !rows.has(`/derived/${K2}/lung.pyramid.ome.tiff`));

// 10. A build recorded with its home; IndexedDB's own copies carry their bytes.
const kept = await visit.derived.record({ path: `/derived/${K5}/m.labels.pyramid.ome.tiff`, key: K5,
                                          name: "m.labels.pyramid.ome.tiff", home: "indexeddb",
                                          blob: { size: 3 }, bytes: 3 }, new FakeFile(bytes("abc"), "m", 1));
check("record keeps a build, kind from its name", kept?.kind === "labels" && rows.get(kept.path)?.home === "indexeddb");

const failed = results.filter((r) => !r.ok);
for (const r of results) console.log(`${r.ok ? "ok  " : "FAIL"} ${r.name}${r.ok || !r.detail ? "" : ` -- ${r.detail}`}`);
if (failed.length) {
    console.log(`derived index probe FAILED: ${failed.length} of ${results.length}`);
    process.exit(1);
}
console.log(`derived index probe OK (${results.length} checks)`);
