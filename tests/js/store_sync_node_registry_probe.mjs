// The website's node registry stays in this browser (client workers/storeSync.js,
// `open(handle, {shared: true})`). The website's Python keeps its data root at
// /data, which is the workspace's projects folder on the computer, and writes
// there `nodes.json`: each data node it reaches through the relay, with that
// node's bearer token, at a room only this browser holds the key to
// (services/webRemote.js keeps its rooms in this browser's localStorage).
// plexora/server/models/nodes.py: "Never a shared root ... a user's node
// tokens are the user's". So the registry is never written into the shared
// folder, and one another browser left there is never read in: two browsers
// on one workspace would otherwise copy each other's nodes, and each one's
// Python (server/models/web_remote.py `_reconcile`) would take the other's
// nodes off the map as rooms it does not hold.
//
// Two stores on one folder stand in for two browsers (Chrome and Edge, or two
// computers syncing Documents) granted the same workspace. An in-memory file
// system and folder handles stand in for Emscripten's MEMFS and the File
// System Access API, as in store_sync_shared_probe.mjs.
//
//   node tests/js/store_sync_node_registry_probe.mjs
import { createStore } from "../../plexora/client/src/js/workers/storeSync.js";

let clock = Date.now();
const tick = () => (clock += 5);
const bytes = (text) => new TextEncoder().encode(text);
const text = (data) => new TextDecoder().decode(data);

const DIR = 0o040000;
function memfs() {
    const nodes = new Map([["/", { dir: true, mtime: tick() }]]);
    const parent = (path) => path.slice(0, path.lastIndexOf("/")) || "/";
    const need = (path) => {
        const node = nodes.get(path);
        if (!node) throw Object.assign(new Error(`ENOENT ${path}`), { code: "ENOENT" });
        return node;
    };
    return {
        streams: [],
        readdir(path) {
            need(path);
            const prefix = path === "/" ? "/" : `${path}/`;
            const names = [...nodes.keys()].filter((key) => key !== path && key.startsWith(prefix)
                && !key.slice(prefix.length).includes("/")).map((key) => key.slice(prefix.length));
            return [".", "..", ...names];
        },
        stat(path) {
            const node = need(path);
            return { mode: node.dir ? DIR : 0o100000, mtime: new Date(node.mtime),
                     size: node.dir ? 4096 : node.usedBytes };
        },
        isDir: (mode) => mode === DIR,
        mkdirTree(path) {
            let at = "";
            for (const part of path.split("/").filter(Boolean)) {
                at += `/${part}`;
                if (!nodes.has(at)) nodes.set(at, { dir: true, mtime: tick() });
            }
        },
        writeFile(path, data) {
            need(parent(path));
            const given = typeof data === "string" ? bytes(data) : data;
            let node = nodes.get(path);
            if (!node) nodes.set(path, (node = { dir: false, contents: null, usedBytes: 0, mtime: tick() }));
            node.contents = given.slice();
            node.usedBytes = given.length;
            node.mtime = tick();
        },
        utime(path, atime, mtime) { need(path).mtime = mtime; },
        readFile(path) {
            const node = need(path);
            return node.contents ? node.contents.subarray(0, node.usedBytes) : new Uint8Array(0);
        },
        unlink(path) { need(path); nodes.delete(path); },
        rmdir(path) { need(path); nodes.delete(path); },
        lookupPath(path) { return { node: need(path) }; },
        exists: (path) => nodes.has(path),
    };
}

class FileHandle {
    constructor(name, data = new Uint8Array()) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.lastModified = tick();
    }

    async getFile() {
        const snapshot = this.data;
        return {
            size: snapshot.length,
            lastModified: this.lastModified,
            async arrayBuffer() { return snapshot.slice().buffer; },
        };
    }

    async createWritable() {
        const chunks = [];
        return {
            write: async (chunk) => chunks.push(chunk instanceof Uint8Array ? chunk : bytes(String(chunk))),
            close: async () => {
                const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
                let at = 0;
                for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
                this.data = out;
                this.lastModified = tick();
            },
        };
    }
}

class DirHandle {
    constructor(name) {
        this.kind = "directory";
        this.name = name;
        this.children = new Map();
    }

    async *entries() { for (const entry of [...this.children]) yield entry; }

    keys() { return (async function* (names) { for (const name of names) yield name; })([...this.children.keys()]); }

    async getDirectoryHandle(name, { create = false } = {}) {
        let child = this.children.get(name);
        if (!child && create) this.children.set(name, (child = new DirHandle(name)));
        if (!child || child.kind !== "directory") throw Object.assign(new Error(name), { name: "NotFoundError" });
        return child;
    }

    async getFileHandle(name, { create = false } = {}) {
        let child = this.children.get(name);
        if (!child && create) this.children.set(name, (child = new FileHandle(name)));
        if (!child || child.kind !== "file") throw Object.assign(new Error(name), { name: "NotFoundError" });
        return child;
    }

    async removeEntry(name) { this.children.delete(name); }

    put(rel, data) {
        const file = new FileHandle(rel, typeof data === "string" ? bytes(data) : data);
        this.children.set(rel, file);
        return file;
    }
}

const results = [];
function check(name, ok, detail = "") {
    results.push({ name, ok: Boolean(ok), detail });
}

// Browser 1's relay node, as web_remote._register writes it (nodes.json).
const OWN = JSON.stringify({ hpc: { endpoint: "https://viewer.plexoraapp.com/__plexora/relay/room-of-browser-1",
                                    token: "node-bearer-token-1", managed_by: "connect:hpc" } });

// The workspace's projects folder, granted to both browsers.
const folder = new DirHandle("projects");
folder.put("config.json", JSON.stringify({ lung: { path: "/x/lung.ome.tif" } }));

const FS1 = memfs();
const FS2 = memfs();
const browser1 = createStore(FS1, "/data");
const browser2 = createStore(FS2, "/data");
await browser1.open(folder, { shared: true });
await browser2.open(folder, { shared: true });

// 1. Browser 1 connects to a node through the relay: its Python registers it.
FS1.writeFile("/data/nodes.json", OWN);
await browser1.push();
const written = folder.children.get("nodes.json");
check("the node registry and its bearer token are not written into the shared folder",
      !written || !text(written.data).includes("node-bearer-token-1"),
      written ? text(written.data) : "");

// 2. Browser 2 does not get browser 1's nodes.
await browser2.pull();
check("another browser does not read this browser's node registry in",
      !FS2.exists("/data/nodes.json") || !text(FS2.readFile("/data/nodes.json")).includes("node-bearer-token-1"),
      FS2.exists("/data/nodes.json") ? text(FS2.readFile("/data/nodes.json")) : "");

// 3. Browser 2's Python reconciles: a room it does not hold comes off its map,
// and its registry is written back. Browser 1 keeps its own node.
FS2.writeFile("/data/nodes.json", "{}");
await browser2.push();
await browser1.pull();
check("another browser's registry never replaces this browser's: its relay node stays registered",
      FS1.exists("/data/nodes.json") && text(FS1.readFile("/data/nodes.json")).includes("node-bearer-token-1"),
      FS1.exists("/data/nodes.json") ? text(FS1.readFile("/data/nodes.json")) : "gone");

// The projects themselves are still shared, as before.
check("the project registry is still shared", FS2.exists("/data/config.json")
      && text(FS2.readFile("/data/config.json")).includes("lung"));

// The website's settings (plexora/browser/boot.py XDG_CONFIG_HOME: /data/.config):
// this browser's, kept in its own storage -- never in the shared folder, never
// read from another browser's -- and back after a reload.
{
    const shared = new DirHandle("projects");
    shared.put("config.json", "{}");
    const mine = new DirHandle("state");
    const FS3 = memfs();
    const store = createStore(FS3, "/data", () => {}, { own: async () => mine });
    await store.open(shared, { shared: true });
    FS3.mkdirTree("/data/.config/plexora");
    FS3.writeFile("/data/.config/plexora/settings.json", JSON.stringify({ path_picker: { view: { layout: "grid" } } }));
    await store.push();
    check("the website's settings are not written into the shared folder", !shared.children.has(".config"),
          [...shared.children.keys()].join(","));
    check("...they are kept in this browser's own storage", mine.children.has(".config"),
          [...mine.children.keys()].join(","));
    const FS4 = memfs();
    const again = createStore(FS4, "/data", () => {}, { own: async () => mine });
    await again.open(shared, { shared: true });
    check("...and a reload finds them", FS4.exists("/data/.config/plexora/settings.json")
          && text(FS4.readFile("/data/.config/plexora/settings.json")).includes("grid"));
}

const failed = results.filter((r) => !r.ok);
for (const r of results) console.log(`${r.ok ? "ok  " : "FAIL"} ${r.name}${r.ok || !r.detail ? "" : ` -- ${r.detail}`}`);
if (failed.length) {
    console.log(`store sync node registry probe FAILED: ${failed.length} of ${results.length}`);
    process.exit(1);
}
console.log(`store sync node registry probe OK (${results.length} checks)`);
