// The website's project store in a folder the desktop app writes too
// (client workers/storeSync.js, `open(handle, {shared: true})`): the desktop's
// caches of one machine, its server token, write probes and files too big to
// hold in memory are never read in, written or deleted; everything else is
// read and written back as before. In this browser's own storage (not shared)
// nothing is left out. A file of LAZY_BYTES or more is read only when Python
// first reads it, and one never read is never written back. An in-memory file
// system that keeps a file's bytes as MEMFS does (`node.contents`,
// `node.usedBytes`) and folder handles whose File snapshots go stale as
// Chrome's do stand in for Emscripten's MEMFS and the File System Access API.
//
//   node tests/js/store_sync_shared_probe.mjs
import { createStore, HELD_BYTES, keptOut, LAZY_BYTES } from "../../plexora/client/src/js/workers/storeSync.js";

let clock = Date.now();
const tick = () => (clock += 5);
const bytes = (text) => new TextEncoder().encode(text);
const text = (data) => new TextDecoder().decode(data);

// -- Emscripten's FS, as far as storeSync calls it ------------------------
// A file node keeps its bytes in `contents` (read only to read or write them)
// and its size in `usedBytes`, as MEMFS does; a write truncates, then sets.
const DIR = 0o040000;
class ErrnoError extends Error {
    constructor(errno) {
        super(`errno ${errno}`);
        this.errno = errno;
    }
}
function memfs() {
    const nodes = new Map([["/", { dir: true, mtime: tick() }]]);
    const parent = (path) => path.slice(0, path.lastIndexOf("/")) || "/";
    const need = (path) => {
        const node = nodes.get(path);
        if (!node) throw Object.assign(new Error(`ENOENT ${path}`), { code: "ENOENT" });
        return node;
    };
    const fs = {
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
            const parts = path.split("/").filter(Boolean);
            let at = "";
            for (const part of parts) {
                at += `/${part}`;
                if (!nodes.has(at)) nodes.set(at, { dir: true, mtime: tick() });
            }
        },
        writeFile(path, data) {
            need(parent(path));
            const given = typeof data === "string" ? bytes(data) : data;
            let node = nodes.get(path);
            if (!node) nodes.set(path, (node = { dir: false, contents: null, usedBytes: 0, mtime: tick() }));
            if (node.usedBytes !== 0) { node.contents = null; node.usedBytes = 0; } // O_TRUNC
            node.contents = given.slice();
            node.usedBytes = given.length;
            node.mtime = tick();
        },
        //: A write at the end: the bytes there are read first, as MEMFS does.
        append(path, data) {
            const node = need(path);
            const old = node.contents ? node.contents.subarray(0, node.usedBytes) : new Uint8Array(0);
            const out = new Uint8Array(old.length + data.length);
            out.set(old, 0);
            out.set(data, old.length);
            node.contents = out;
            node.usedBytes = out.length;
            node.mtime = tick();
        },
        utime(path, atime, mtime) { need(path).mtime = mtime; },
        readFile(path) {
            const node = need(path);
            const contents = node.contents;
            return contents ? contents.subarray(0, node.usedBytes) : new Uint8Array(0);
        },
        ErrnoError,
        unlink(path) { need(path); nodes.delete(path); },
        rmdir(path) { need(path); nodes.delete(path); },
        lookupPath(path) { return { node: need(path) }; },
        exists: (path) => nodes.has(path),
    };
    return fs;
}

// -- the File System Access API's folder handles ---------------------------
class FileHandle {
    constructor(name, data = new Uint8Array(), size = null) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.claimed = size; // a size claimed without the bytes: a big file
        this.lastModified = tick();
        this.version = 0;
        this.reads = 0;
        this.syncReads = 0;
        this.writes = 0;
    }

    //: A snapshot: readable only while the file is as it was (Chrome's
    //: NotReadableError otherwise).
    async getFile() {
        const handle = this;
        const version = this.version;
        const snapshot = this.data;
        return {
            size: this.claimed ?? this.data.length,
            lastModified: this.lastModified,
            async arrayBuffer() {
                handle.reads += 1;
                return snapshot.slice().buffer;
            },
            sync() {
                if (handle.version !== version) throw Object.assign(new Error("changed"), { name: "NotReadableError" });
                handle.syncReads += 1;
                return snapshot.slice();
            },
        };
    }

    //: Another app rewriting it in place.
    rewrite(data) {
        this.data = typeof data === "string" ? bytes(data) : data;
        this.version += 1;
        this.lastModified = tick();
    }

    async createWritable() {
        const chunks = [];
        return {
            write: async (chunk) => chunks.push(chunk instanceof Uint8Array ? chunk : bytes(String(chunk))),
            close: async () => {
                const total = chunks.reduce((n, c) => n + c.length, 0);
                const out = new Uint8Array(total);
                let at = 0;
                for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
                this.data = out;
                this.claimed = null;
                this.version += 1;
                this.writes += 1;
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

    // Test helpers: put a file at `rel`, find one.
    put(rel, data, size = null) {
        const parts = rel.split("/");
        let dir = this;
        for (const part of parts.slice(0, -1)) {
            if (!dir.children.has(part)) dir.children.set(part, new DirHandle(part));
            dir = dir.children.get(part);
        }
        const file = new FileHandle(parts.at(-1), typeof data === "string" ? bytes(data) : data, size);
        dir.children.set(parts.at(-1), file);
        return file;
    }

    at(rel) {
        let node = this;
        for (const part of rel.split("/")) {
            node = node && node.kind === "directory" ? node.children.get(part) : null;
        }
        return node || null;
    }
}

const results = [];
function check(name, ok, detail = "") {
    results.push({ name, ok: Boolean(ok), detail });
}

// A folder as the desktop app leaves it when Plexora's folder is its data root.
function desktopFolder() {
    const folder = new DirHandle("projects");
    folder.put("config.json", JSON.stringify({ lung: { path: "/x/lung.ome.tif" } }));
    folder.put("lung/lung.db", "gates");
    folder.put("lung/plugins/gating/table.parquet", "plugin state");
    folder.put(".remote_cache/ab/cd", "web chunk");
    folder.put(".ims_index/f.json", "chunk index");
    folder.put(".models/sam.onnx", "weights");
    folder.put(".telemetry/queue.jsonl", "events");
    folder.put("server_token.json", "desktop-secret");
    folder.put(".plexora-write-probe-12-34", "");
    folder.put(".figures/fig1/figure.db", "a figure");
    const big = folder.put("lung/cells.csv", "the desktop's table", HELD_BYTES + 1);
    return { folder, big };
}

// 1. Opened shared: what is the page's is read in; the rest is not.
{
    const FS = memfs();
    const logs = [];
    const store = createStore(FS, "/data", (line) => logs.push(line));
    const { folder, big } = desktopFolder();
    const opened = await store.open(folder, { shared: true });
    check("registry read in", FS.exists("/data/config.json"));
    check("a project's state read in", FS.exists("/data/lung/lung.db")
          && FS.exists("/data/lung/plugins/gating/table.parquet"));
    check("figures read in", FS.exists("/data/.figures/fig1/figure.db"));
    for (const rel of [".remote_cache", ".ims_index", ".models", ".telemetry", "server_token.json",
                       ".plexora-write-probe-12-34"]) {
        check(`${rel} not read in`, !FS.exists(`/data/${rel}`));
    }
    check("a file too big to hold is left", !FS.exists("/data/lung/cells.csv") && big.reads === 0,
          `reads=${big.reads}`);
    check("held lists it", JSON.stringify(store.held()) === JSON.stringify(["lung/cells.csv"]),
          JSON.stringify(store.held()));
    check("open says how many were held", opened.held === 1, JSON.stringify(opened));
    check("held is said once", logs.filter((l) => l.includes("lung/cells.csv")).length === 1, logs.join(" | "));

    // 2. The page's Python makes its own at those paths; a write-back leaves
    // the desktop's alone and writes the page's work.
    FS.writeFile("/data/server_token.json", "page-secret");
    FS.mkdirTree("/data/.remote_cache/zz");
    FS.writeFile("/data/.remote_cache/zz/chunk", "page chunk");
    FS.writeFile("/data/lung/cells.csv", "the page's own table");
    FS.writeFile("/data/lung/lung.db", "gates, changed on the website");
    FS.writeFile("/data/lung/regions.json", "[]");
    const pushed = await store.push();
    check("the desktop's token kept", text(folder.at("server_token.json").data) === "desktop-secret");
    check("no page cache written into the folder", !folder.at(".remote_cache/zz"));
    check("the held file kept", folder.at("lung/cells.csv") === big && big.claimed === HELD_BYTES + 1
          && text(big.data) === "the desktop's table");
    check("the page's work written", text(folder.at("lung/lung.db").data) === "gates, changed on the website"
          && text(folder.at("lung/regions.json").data) === "[]", JSON.stringify(pushed));

    // 3. Deletions in memory: the desktop's caches are not the page's to
    // delete; a file the page read is deleted as before.
    FS.unlink("/data/server_token.json");
    FS.unlink("/data/lung/regions.json");
    await store.push();
    check("a deletion of the page's own file reaches the folder", !folder.at("lung/regions.json"));
    check("caches still there", Boolean(folder.at(".models/sam.onnx")) && Boolean(folder.at(".remote_cache/ab/cd"))
          && Boolean(folder.at("server_token.json")));

    // 4. The desktop app writes: its caches are not read in, the rest is.
    folder.put(".remote_cache/ef/gh", "another chunk");
    folder.put("config.json", JSON.stringify({ lung: {}, liver: {} }));
    const pulled = await store.pull();
    check("a changed registry read in", text(FS.readFile("/data/config.json")).includes("liver"), JSON.stringify(pulled));
    check("a new cache file not read in", !FS.exists("/data/.remote_cache/ef"));

    // 5. The big file shrinks below the limit: read in like any other.
    folder.at("lung").children.delete("cells.csv");
    await store.pull();
    check("a held file gone from the folder is no longer held", store.held().length === 0,
          JSON.stringify(store.held()));
}

// 6. This browser's own storage is not shared: nothing is left out.
{
    const FS = memfs();
    const store = createStore(FS, "/data");
    const folder = new DirHandle("state");
    folder.put("config.json", "{}");
    folder.put(".remote_cache/ab/cd", "web chunk");
    folder.put("server_token.json", "page-secret");
    await store.open(folder);
    check("own storage: caches read in", FS.exists("/data/.remote_cache/ab/cd") && FS.exists("/data/server_token.json"));
    FS.writeFile("/data/.remote_cache/ab/new", "x");
    await store.push();
    check("own storage: caches written back", Boolean(folder.at(".remote_cache/ab/new")));
    check("own storage: nothing held", store.held().length === 0);
}

// 7. Big files are read when Python first reads them.
{
    const FS = memfs();
    const logs = [];
    const store = createStore(FS, "/data", (line) => logs.push(line), { readSync: (file) => file.sync() });
    const folder = new DirHandle("projects");
    folder.put("config.json", "{}");
    const lung = folder.put("lung/lung.db", new Uint8Array(LAZY_BYTES + 10).fill(7));
    const liver = folder.put("liver/liver.db", new Uint8Array(LAZY_BYTES).fill(3));
    const kidney = folder.put("kidney/kidney.db", new Uint8Array(LAZY_BYTES).fill(4));
    const small = folder.put("lung/notes.json", "[]");
    const opened = await store.open(folder, { shared: true });
    check("lazy: nothing big read at open", lung.reads + lung.syncReads + liver.syncReads === 0 && small.reads === 1);
    check("lazy: its size known without reading", FS.stat("/data/lung/lung.db").size === LAZY_BYTES + 10
          && opened.unread === 3, JSON.stringify(opened));
    await store.push();
    check("lazy: a write-back reads and writes nothing unread",
          lung.syncReads === 0 && lung.writes === 0 && liver.writes === 0);
    const read = FS.readFile("/data/lung/lung.db");
    check("lazy: read when Python reads it", read.length === LAZY_BYTES + 10 && read[5] === 7 && lung.syncReads === 1);
    FS.readFile("/data/lung/lung.db");
    check("lazy: read once", lung.syncReads === 1);
    await store.push();
    check("lazy: read but unchanged is not written back", lung.writes === 0);

    FS.writeFile("/data/liver/liver.db", "new liver");
    check("lazy: written anew without reading the old", liver.syncReads === 0);
    FS.append("/data/kidney/kidney.db", bytes("+"));
    const kid = FS.readFile("/data/kidney/kidney.db");
    check("lazy: a write at the end reads first", kidney.syncReads === 1 && kid.length === LAZY_BYTES + 1
          && kid[0] === 4 && kid[LAZY_BYTES] === "+".charCodeAt(0));
    check("lazy: unread lists none of them now", store.unread().length === 0, JSON.stringify(store.unread()));
    await store.push();
    check("lazy: the page's bytes written back", text(folder.at("liver/liver.db").data) === "new liver"
          && folder.at("kidney/kidney.db").data.length === LAZY_BYTES + 1);

    // Another app changes one before Python reads it: EIO, never other bytes;
    // the next pull places the new one.
    const spleen = folder.put("spleen/spleen.db", new Uint8Array(LAZY_BYTES).fill(1));
    await store.pull();
    check("lazy: a new big file is pulled unread", spleen.syncReads === 0 && store.unread().includes("spleen/spleen.db"));
    spleen.rewrite(new Uint8Array(LAZY_BYTES).fill(2));
    let errno = null;
    try {
        FS.readFile("/data/spleen/spleen.db");
    } catch (error) {
        errno = error.errno;
    }
    check("lazy: a file changed under it reads as EIO", errno === 29 && logs.some((l) => l.includes("spleen")),
          String(errno));
    await store.pull();
    check("lazy: the next pull places the new bytes", FS.readFile("/data/spleen/spleen.db")[0] === 2);

    // Deleted in memory without being read: deleted in the folder.
    const ghost = folder.put("ghost/ghost.db", new Uint8Array(LAZY_BYTES));
    await store.pull();
    FS.unlink("/data/ghost/ghost.db");
    await store.push();
    check("lazy: an unread file deleted in memory is deleted in the folder",
          !folder.at("ghost/ghost.db") && ghost.syncReads === 0);
}

// Files another worker wrote straight into the folder (an import's large
// members, services/bundleWeb.js): adopted, they are there for Python without
// a read, and neither the next push nor the next pull touches them.
for (const shared of [true, false]) {
    const FS = memfs();
    const store = createStore(FS, "/data", () => {}, { readSync: (file) => file.sync() });
    const folder = new DirHandle("projects");
    folder.put("config.json", "{}");
    await store.open(folder, { shared });
    const table = folder.put("lung/cells.csv", new Uint8Array(LAZY_BYTES * 2).fill(3));
    const note = folder.put("lung/plugins/note.json", "{}");
    const slide = folder.put("lung/media/k/slide.ome.tif", "pixels", HELD_BYTES + 1);
    const adopted = await store.adopt(["lung/cells.csv", "lung/plugins/note.json", "lung/media/k/slide.ome.tif",
                                       "lung/gone.csv"]);
    const how = shared ? "shared" : "own";
    check(`adopt (${how}): present to Python, nothing read`,
          FS.exists("/data/lung/cells.csv") && FS.stat("/data/lung/cells.csv").size === LAZY_BYTES * 2
          && FS.exists("/data/lung/plugins/note.json") && table.reads + table.syncReads + note.reads === 0,
          JSON.stringify(adopted));
    check(`adopt (${how}): a missing one is said`, JSON.stringify(adopted.missing) === JSON.stringify(["lung/gone.csv"]));
    check(`adopt (${how}): one too big to hold is held only in a shared folder`,
          shared ? store.held().includes("lung/media/k/slide.ome.tif") && !FS.exists("/data/lung/media/k/slide.ome.tif")
                 : FS.exists("/data/lung/media/k/slide.ome.tif") && slide.reads + slide.syncReads === 0,
          JSON.stringify(store.held()));
    const writes = table.writes + note.writes;
    await store.push();
    await store.pull();
    await store.push();
    check(`adopt (${how}): neither written back nor read again nor deleted`,
          table.writes + note.writes === writes && table.reads + table.syncReads === 0
          && folder.at("lung/cells.csv") && folder.at("lung/plugins/note.json") && FS.exists("/data/lung/cells.csv"));
    check(`adopt (${how}): read when Python reads it`, FS.readFile("/data/lung/cells.csv")[0] === 3 && table.syncReads === 1);
    // Python taking one back (an import undone): gone from the folder too.
    FS.unlink("/data/lung/plugins/note.json");
    await store.push();
    check(`adopt (${how}): removed in memory, removed in the folder`, !folder.at("lung/plugins/note.json"));
}

// What project bundles keep beside the projects stays out of a shared folder's sync.
{
    const FS = memfs();
    const store = createStore(FS, "/data", () => {}, { readSync: (file) => file.sync() });
    const folder = new DirHandle("projects");
    folder.put("config.json", "{}");
    folder.put(".importing-ab12/projects/x/entry.json", "{}");
    folder.put(".replaced/lung-20261109/state/lung.db", "old");
    folder.put(".bundles/tok/lung.plexora", "PK");
    await store.open(folder, { shared: true });
    check("bundles' staging and exports not read in",
          !FS.exists("/data/.importing-ab12") && !FS.exists("/data/.bundles"));
    check("an Update's or Replace's backup is read in", FS.exists("/data/.replaced/lung-20261109/state/lung.db"));
    FS.mkdirTree("/data/.importing-cd34/projects");
    FS.writeFile("/data/.importing-cd34/projects/y.json", "{}");
    await store.push();
    check("an import's staging in memory is not written to the folder", !folder.at(".importing-cd34"));
}

check("keptOut names only the top level", keptOut(".models/x") && keptOut("server_token.json")
      && !keptOut("lung/.models") && !keptOut("lung/server_token.json") && !keptOut("config.json"));
check("keptOut takes bundles' prefixes", keptOut(".importing-1f2e/x") && !keptOut(".replaced/lung-1/state")
      && keptOut(".bundles/a/b.plexora") && !keptOut("lung/.replaced") && !keptOut("importing-x"));

const failed = results.filter((r) => !r.ok);
for (const r of results) console.log(`${r.ok ? "ok  " : "FAIL"} ${r.name}${r.ok || !r.detail ? "" : ` -- ${r.detail}`}`);
if (failed.length) {
    console.log(`store sync shared probe FAILED: ${failed.length} of ${results.length}`);
    process.exit(1);
}
console.log(`store sync shared probe OK (${results.length} checks)`);
