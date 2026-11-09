// Files too big to hold in memory in the website's project folder
// (client workers/storeSync.js, `open(handle, {shared: true})`, HELD_BYTES):
// a TIFF Python built into a project is read where it lies -- WORKERFS
// mounted under /held/<n>, a link at its path -- never copied into the heap,
// so a later visit finds it instead of building it again. One Python writes
// anew there (beside, then `os.replace` over the link) is written to the
// folder and swapped back for a link; a link Python removed stays removed; a
// file the desktop app rewrites is linked again; anything else too big (a
// SQLite database, written in place) is left in the folder as before. Where
// the file system cannot read through a link, a file Python pushed that grew
// past the limit stays in memory for the visit instead of being dropped.
//
// An in-memory stand-in for Emscripten's FS with links and WORKERFS mounts,
// and folder handles whose File snapshots go stale as Chrome's do.
//
//   node tests/js/store_sync_held_probe.mjs

// The probe of whether links work makes a File: one whose bytes the stand-in
// WORKERFS can read synchronously, as FileReaderSync does in a worker.
globalThis.File = class {
    constructor(parts, name, options = {}) {
        const chunks = parts.map((p) => (p instanceof Uint8Array ? p : new TextEncoder().encode(String(p))));
        const total = chunks.reduce((n, c) => n + c.length, 0);
        this.bytes = new Uint8Array(total);
        let at = 0;
        for (const chunk of chunks) { this.bytes.set(chunk, at); at += chunk.length; }
        this.name = name;
        this.size = total;
        this.lastModified = options.lastModified ?? Date.now();
    }

    sync() { return this.bytes.slice(); }
};

const { createStore, HELD_BYTES, linkable } = await import("../../plexora/client/src/js/workers/storeSync.js");

let clock = Date.now();
const tick = () => (clock += 5);
const bytes = (text) => new TextEncoder().encode(text);
const text = (data) => new TextDecoder().decode(data);

const DIR = 0o040000;
const REG = 0o100000;
const LNK = 0o120000;
class ErrnoError extends Error {
    constructor(errno) {
        super(`errno ${errno}`);
        this.errno = errno;
    }
}

function fs({ links = true } = {}) {
    const nodes = new Map([["/", { dir: true, mtime: tick() }]]);
    const parent = (path) => path.slice(0, path.lastIndexOf("/")) || "/";
    const raw = (path) => {
        const node = nodes.get(path);
        if (!node) throw Object.assign(new ErrnoError(44), { code: "ENOENT" });
        return node;
    };
    const follow = (path, depth = 0) => {
        const node = raw(path);
        if (node.link && depth < 8) return follow(node.link, depth + 1);
        return node;
    };
    const modeOf = (node) => (node.dir ? DIR : node.link ? LNK : REG);
    const sizeOf = (node) => (node.dir ? 4096 : node.worker ? node.worker.size : node.link ? node.link.length : node.usedBytes);
    const workerReads = { count: 0 };
    const out = {
        streams: [],
        workerReads,
        readdir(path) {
            follow(path);
            const prefix = path === "/" ? "/" : `${path}/`;
            return [".", "..", ...[...nodes.keys()].filter((k) => k !== path && k.startsWith(prefix)
                && !k.slice(prefix.length).includes("/")).map((k) => k.slice(prefix.length))];
        },
        stat(path) {
            const node = follow(path);
            return { mode: modeOf(node), mtime: new Date(node.mtime), size: sizeOf(node) };
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
            follow(parent(path));
            const given = typeof data === "string" ? bytes(data) : data;
            const there = nodes.get(path);
            if (there && (there.link ? follow(path).worker : there.worker)) throw new ErrnoError(2); // read-only
            nodes.set(path, { file: true, contents: given.slice(), usedBytes: given.length, mtime: tick() });
        },
        utime(path, atime, mtime) { follow(path).mtime = mtime; },
        readFile(path) {
            const node = follow(path);
            if (node.worker) {
                workerReads.count += 1;
                return node.worker.sync();
            }
            return node.contents ? node.contents.subarray(0, node.usedBytes) : new Uint8Array(0);
        },
        rename(from, to) {
            const node = raw(from);
            nodes.delete(from);
            nodes.set(to, node); // replaces whatever is there, a link itself included
        },
        ErrnoError,
        unlink(path) { raw(path); nodes.delete(path); },
        rmdir(path) { raw(path); nodes.delete(path); },
        lookupPath(path) { return { node: follow(path) }; },
        exists: (path) => nodes.has(path),
        mounts: new Set(),
    };
    if (links) {
        Object.assign(out, {
            lstat(path) {
                const node = raw(path);
                return { mode: modeOf(node), mtime: new Date(node.mtime), size: sizeOf(node) };
            },
            isLink: (mode) => mode === LNK,
            symlink(target, path) {
                follow(parent(path));
                nodes.set(path, { link: target, mtime: tick() });
            },
            readlink(path) { return raw(path).link; },
            mount(type, { files }, dir) {
                out.mkdirTree(dir);
                for (const file of files) nodes.set(`${dir}/${file.name}`, { worker: file, mtime: file.lastModified });
                out.mounts.add(dir);
            },
            unmount(dir) {
                for (const key of [...nodes.keys()]) if (key.startsWith(`${dir}/`)) nodes.delete(key);
                out.mounts.delete(dir);
            },
            filesystems: { WORKERFS: { createNode() {} } },
        });
    }
    return out;
}

class FileHandle {
    constructor(name, data, size = null) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.claimed = size;
        this.lastModified = tick();
        this.version = 0;
        this.writes = 0;
        this.reads = 0;
        this.bigAfterWrite = false;
    }

    async getFile() {
        const handle = this;
        const version = this.version;
        const snapshot = this.data;
        return {
            name: this.name,
            size: this.claimed ?? this.data.length,
            lastModified: this.lastModified,
            async arrayBuffer() { handle.reads += 1; return snapshot.slice().buffer; },
            sync() {
                if (handle.version !== version) throw Object.assign(new Error("changed"), { name: "NotReadableError" });
                handle.reads += 1;
                return snapshot.slice();
            },
        };
    }

    rewrite(data, big = true) {
        this.data = bytes(data);
        this.claimed = big ? HELD_BYTES + this.data.length : null;
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
                // A pyramid as big as the real one: past the limit once written.
                this.claimed = this.bigAfterWrite ? HELD_BYTES + total : null;
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
        if (!child && create) this.children.set(name, (child = new FileHandle(name, new Uint8Array())));
        if (!child || child.kind !== "file") throw Object.assign(new Error(name), { name: "NotFoundError" });
        return child;
    }

    async removeEntry(name) { this.children.delete(name); }

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
        for (const part of rel.split("/")) node = node && node.kind === "directory" ? node.children.get(part) : null;
        return node || null;
    }
}

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: Boolean(ok), detail });
const isLinkAt = (FS, path) => { try { return FS.isLink(FS.lstat(path).mode); } catch (error) { return false; } };

const PYRAMID = "lung/derived/mask.outlines.pyramid.ome.tiff";
const DB = "lung/lung.db";

// 1. Opened shared: the big TIFF is read where it lies, the big database is
// left in the folder as before.
{
    const FS = fs();
    const store = createStore(FS, "/data", () => {});
    const folder = new DirHandle("projects");
    folder.put("config.json", "{}");
    const pyramid = folder.put(PYRAMID, "the pyramid", HELD_BYTES + 1);
    const db = folder.put(DB, "a big database", HELD_BYTES + 5);
    const opened = await store.open(folder, { shared: true });
    const probed = FS.workerReads.count; // the one-time check that links work
    check("only TIFFs are linked", linkable(PYRAMID) && !linkable(DB) && linkable("a/b.tif"));
    check("the pyramid is a link", isLinkAt(FS, `/data/${PYRAMID}`), JSON.stringify(opened));
    check("its bytes are not read at open", pyramid.reads === 0 && probed <= 1);
    check("Python reads it through the link", text(FS.readFile(`/data/${PYRAMID}`)) === "the pyramid"
          && FS.workerReads.count === probed + 1);
    check("the database is left in the folder", !FS.exists(`/data/${DB}`) && db.reads === 0);
    check("held lists both, linked the pyramid", JSON.stringify(store.held().sort()) === JSON.stringify([DB, PYRAMID].sort())
          && JSON.stringify(store.linked()) === JSON.stringify([PYRAMID]), JSON.stringify([store.held(), store.linked()]));

    // 2. A write-back writes nothing for a link, and leaves the database.
    await store.push();
    check("a link is never written back", pyramid.writes === 0 && db.writes === 0);

    // 3. Python builds it again: beside, then os.replace over the link.
    FS.writeFile("/data/lung/derived/.mask-tmp.ome.tiff", "a new pyramid");
    FS.rename("/data/lung/derived/.mask-tmp.ome.tiff", `/data/${PYRAMID}`);
    check("the link is replaced by Python's file", !isLinkAt(FS, `/data/${PYRAMID}`));
    pyramid.bigAfterWrite = true;
    await store.push();
    check("Python's file is written to the folder", pyramid.writes === 1 && text(pyramid.data) === "a new pyramid");
    const pulled = await store.pull();
    check("then swapped back for a link (the heap gets its bytes back)", isLinkAt(FS, `/data/${PYRAMID}`)
          && text(FS.readFile(`/data/${PYRAMID}`)) === "a new pyramid", JSON.stringify(pulled));
    await store.push();
    check("and not written again", pyramid.writes === 1 && Boolean(folder.at(PYRAMID)));

    // 4. Python removes it: the link stays removed while the folder's file is the same.
    FS.unlink(`/data/${PYRAMID}`);
    await store.push();
    check("removing the link deletes nothing in the folder", Boolean(folder.at(PYRAMID)));
    await store.pull();
    check("a removed link is not made again", !FS.exists(`/data/${PYRAMID}`));

    // 5. The desktop app rewrites it: linked again, with its new bytes.
    pyramid.rewrite("the desktop's pyramid");
    await store.pull();
    check("a file the folder changed is linked again", isLinkAt(FS, `/data/${PYRAMID}`)
          && text(FS.readFile(`/data/${PYRAMID}`)) === "the desktop's pyramid");

    // 6. Gone from the folder: its link goes too.
    folder.at("lung/derived").children.delete("mask.outlines.pyramid.ome.tiff");
    await store.pull();
    check("a file gone from the folder loses its link", !FS.exists(`/data/${PYRAMID}`) && store.linked().length === 0);

    // 7. Reopened (Settings moved the store): the old mounts are let go.
    const before = FS.mounts.size;
    folder.put(PYRAMID, "back", HELD_BYTES + 1);
    await store.pull();
    await store.open(folder, { shared: true });
    check("reopening lets the old mounts go", FS.mounts.size === 1 && before === 0, String(FS.mounts.size));
}

// 8. This browser's own storage is not shared: nothing is linked.
{
    const FS = fs();
    const store = createStore(FS, "/data", () => {});
    const folder = new DirHandle("state");
    folder.put("config.json", "{}");
    await store.open(folder);
    check("own storage: nothing linked", store.linked().length === 0 && FS.mounts.size === 0);
}

// 9. Where links do not work: a file Python pushed that grew past the limit
// stays in memory for the visit (it used to be dropped, and built again), and
// the folder's copy is left alone.
{
    const FS = fs({ links: false });
    const store = createStore(FS, "/data", () => {});
    const folder = new DirHandle("projects");
    folder.put("config.json", "{}");
    await store.open(folder, { shared: true });
    FS.mkdirTree("/data/lung/derived");
    FS.writeFile(`/data/${PYRAMID}`, "built this visit");
    await store.push();
    folder.at(PYRAMID).bigAfterWrite = true;
    folder.at(PYRAMID).claimed = HELD_BYTES + 20;
    await store.pull();
    check("no links: Python keeps what it built", FS.exists(`/data/${PYRAMID}`)
          && text(FS.readFile(`/data/${PYRAMID}`)) === "built this visit");
    await store.push();
    check("no links: the folder's copy is not deleted", Boolean(folder.at(PYRAMID)));
}

const failed = results.filter((r) => !r.ok);
for (const r of results) console.log(`${r.ok ? "ok  " : "FAIL"} ${r.name}${r.ok || !r.detail ? "" : ` -- ${r.detail}`}`);
if (failed.length) {
    console.log(`store sync held probe FAILED: ${failed.length} of ${results.length}`);
    process.exit(1);
}
console.log(`store sync held probe OK (${results.length} checks)`);
