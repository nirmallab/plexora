// The website's `.plexora` transport (client services/bundleWeb.js) driving
// its real worker (workers/bundleWorker.js) in Node, with Python, the
// project store's folder, this site's storage and the Save dialog stood in
// for: the order things happen in, where each member's bytes come from and
// go, what Python is told, and what is taken back when an import fails.
// tests/test_bundle_codec.py runs this and pins its lines.
//
//   node tests/js/bundle_web_probe.mjs
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const JS = path.join(REPO, "plexora", "client", "src", "js");
const WORKER_URL = pathToFileURL(path.join(JS, "workers", "bundleWorker.js")).href;
const codec = await import(WORKER_URL);

const text = (value) => new TextEncoder().encode(value);
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

// -- folders, as the File System Access API and OPFS give them ---------------

class FileHandle {
    constructor(name, bytes = new Uint8Array(0)) {
        this.kind = "file";
        this.name = name;
        this.bytes = bytes;
        this.lastModified = Date.now();
        this.aborted = false;
        this.removed = false;
    }

    async getFile() { return new File([this.bytes], this.name, { lastModified: this.lastModified }); }

    async createWritable() {
        const handle = this;
        let buffer = new Uint8Array(1024);
        let size = 0;
        let at = 0;
        const put = (bytes, where) => {
            const end = where + bytes.length;
            if (end > buffer.length) {
                const grown = new Uint8Array(Math.max(end, buffer.length * 2));
                grown.set(buffer.subarray(0, size));
                buffer = grown;
            }
            buffer.set(bytes, where);
            size = Math.max(size, end);
            at = end;
        };
        return {
            async write(data) {
                if (data && data.type === "write") put(new Uint8Array(data.data), data.position ?? at);
                else put(new Uint8Array(data), at);
                await tick();
            },
            async close() { handle.bytes = buffer.slice(0, size); handle.lastModified = Date.now(); },
            async abort() { handle.aborted = true; },
        };
    }

    async createSyncAccessHandle() {
        const handle = this;
        let buffer = handle.bytes.slice();
        return {
            truncate(n) { buffer = buffer.slice(0, n); },
            write(bytes, { at }) {
                if (at + bytes.length > buffer.length) {
                    const grown = new Uint8Array(at + bytes.length);
                    grown.set(buffer);
                    buffer = grown;
                }
                buffer.set(bytes, at);
                return bytes.length;
            },
            flush() { handle.bytes = buffer.slice(); },
            close() { handle.bytes = buffer.slice(); },
        };
    }

    async remove() { this.removed = true; }
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

    async removeEntry(name) {
        if (!this.children.has(name)) throw Object.assign(new Error(name), { name: "NotFoundError" });
        this.children.delete(name);
    }

    put(rel, bytes) {
        const parts = rel.split("/");
        let dir = this;
        for (const part of parts.slice(0, -1)) {
            if (!dir.children.has(part)) dir.children.set(part, new DirHandle(part));
            dir = dir.children.get(part);
        }
        const file = new FileHandle(parts.at(-1), bytes);
        dir.children.set(parts.at(-1), file);
        return file;
    }

    at(rel) {
        let node = this;
        for (const part of rel.split("/")) node = node && node.kind === "directory" ? node.children.get(part) : null;
        return node || null;
    }
}

const opfs = new DirHandle("");
Object.defineProperty(globalThis.navigator, "storage", {
    value: { getDirectory: async () => opfs, estimate: async () => ({ quota: 10e9 }) }, configurable: true });

// -- the worker: its own module, in a scope of its own ---------------------------

globalThis.WorkerGlobalScope = class WorkerGlobalScope {};
let spawned = 0;
class FakeWorker {
    constructor(url, options) {
        assert.match(String(url), /\/client\/src\/js\/workers\/bundleWorker\.js$/);
        assert.equal(options && options.type, "module");
        spawned += 1;
        this.onmessage = null;
        this.terminated = false;
        const scope = new globalThis.WorkerGlobalScope();
        scope.postMessage = (data) => setTimeout(() => { if (!this.terminated && this.onmessage) this.onmessage({ data }); }, 0);
        this.scope = scope;
        this.ready = (async () => {
            globalThis.self = scope;
            await import(`${WORKER_URL}?worker=${spawned}`);
        })();
    }

    postMessage(data) {
        this.ready.then(() => {
            globalThis.self = this.scope;
            this.scope.onmessage({ data });
        });
    }

    terminate() { this.terminated = true; }
}
globalThis.Worker = FakeWorker;

// -- Python, as the page's worker answers -----------------------------------------

const store = new DirHandle("projects");
const calls = [];
const work = {};
let routes = {};
const adopted = [];
let pulls = 0;
const python = {
    async fetch(url, init) {
        const method = (init && init.method) || "GET";
        const body = init && init.body ? JSON.parse(init.body) : undefined;
        calls.push([method, url, body]);
        const handler = routes[`${method} ${url}`] || routes[`${method} ${url.replace(/\/[^/]+$/, "/*")}`];
        if (!handler) return new Response(JSON.stringify({ error: `no route ${method} ${url}` }), { status: 404 });
        const [status, answer] = await handler(body, url);
        return new Response(JSON.stringify(answer), { status });
    },
    async flush() { calls.push(["flush"]); return true; },
    async bundleStores() { return { "/data": { kind: "folder", handle: store }, "/library/x": { kind: "indexeddb" } }; },
    async readFile(where) {
        calls.push(["readFile", where]);
        if (!(where in work)) throw new Error("ENOENT");
        return work[where];
    },
    async mount(dir, files) { calls.push(["mount", dir]); return files.map((file) => `${dir}/${file.name}`); },
    async adopt(paths) { adopted.push(...paths); return { "/data": { placed: paths.length, held: 0, linked: 0, missing: [] } }; },
    async pull() { pulls += 1; return {}; },
};

const picked = new File([new Uint8Array(70000).fill(9)], "slide.ome.tif");
let found = {};
const host = {
    PlexoraPython: python,
    PlexoraPicks: {
        fileFor: (where) => (where === "/picked/aa/slide.ome.tif" ? picked : null),
        async find(missing) { calls.push(["find", missing.length]); return found; },
    },
};
host.parent = host;
const clicks = [];
const frame = {
    parent: host,
    document: {
        createElement: () => ({ style: {}, click() { clicks.push({ href: this.href, download: this.download }); }, remove() {} }),
        body: { appendChild() {} },
    },
    URL: { createObjectURL: (blob) => { frame.lastBlob = blob; return "blob:fake"; }, revokeObjectURL() {} },
    setTimeout: () => 0,
    PlexoraProjectExport: { _internal: { createServerTransport: () => ({
        kind: "server", estimate: async () => ({ total: 1 }), locations: async () => ({ locations: [] }),
        status: async () => { throw new Error("not the server's"); }, cancel: async () => ({}),
        deliver: async () => ({ downloaded: true }) }) } },
};

// The host page's copy installs the engine; the frame's the transport.
const source = readFileSync(path.join(JS, "services", "bundleWeb.js"), "utf8");
vm.runInThisContext(`(function (window) {${source}\n})`)(host);
assert.ok(host.PlexoraBundleWeb, "the host page runs the jobs");
vm.runInThisContext(`(function (window) {${source}\n})`)(frame);
const transport = frame.PlexoraBundleTransport;
assert.equal(transport.kind, "website");

const results = [];
async function check(name, run) {
    await run();
    results.push(name);
    console.log(`ok - ${name}`);
}

async function until(kind, job, seen = []) {
    for (let i = 0; i < 2000; i += 1) {
        const status = await transport.status(kind, job);
        if (!seen.length || seen.at(-1) !== status.stage) seen.push(status.stage);
        if (["ready", "failed", "cancelled"].includes(status.status)) return status;
        await tick();
    }
    throw new Error("the job never ended");
}

// -- export ---------------------------------------------------------------------------

const csv = text("CellID,X,Y\n".repeat(4000));
store.put("p1/cells.csv", csv);
work["/tmp/plexora-bundle-1/00000.bin"] = text('{"title": "P1"}');
work["/tmp/plexora-bundle-1/00001.db"] = text("SQLite format 3\u0000 rows");
const records = {
    "projects/p1/entry.json": { path: "projects/p1/entry.json", size: 15, sha256: null },
    "projects/p1/state/p1.db": { path: "projects/p1/state/p1.db", size: 22, sha256: null, kind: "state" },
    "projects/p1/state/cells.csv": { path: "projects/p1/state/cells.csv", size: csv.length, sha256: null, kind: "table" },
    "projects/p1/state/gone.json": { path: "projects/p1/state/gone.json", size: 2, sha256: null, kind: "state" },
    "media/k1/slide.ome.tif": { path: "media/k1/slide.ome.tif", size: picked.size, sha256: null },
};
const plan = {
    format: 2, reserve: 4096, work_dir: "/tmp/plexora-bundle-1", entries: { p1: { title: "P1" } }, stamps: { p1: "s" },
    manifest: { format: 2, projects: [{ id: "p1", name: "p1", entry: records["projects/p1/entry.json"],
                                        files: Object.values(records).filter((r) => r.kind), refs: [] }],
                media: [records["media/k1/slide.ome.tif"]], captures: [], datasets: [], figures: [], left_out: [] },
    members: [
        { bundle_path: "projects/p1/entry.json", source: "/tmp/plexora-bundle-1/00000.bin", size: 15 },
        { bundle_path: "projects/p1/state/p1.db", source: "/tmp/plexora-bundle-1/00001.db", size: 22 },
        { bundle_path: "projects/p1/state/cells.csv", source: "/data/p1/cells.csv", size: csv.length },
        { bundle_path: "projects/p1/state/gone.json", source: "/data/p1/gone.json", size: 2 },
        { bundle_path: "media/k1/slide.ome.tif", source: "/picked/aa/slide.ome.tif", size: picked.size },
    ],
};
let finished = null;
routes = {
    "POST /projects/export/plan": async () => [200, plan],
    "POST /projects/export/manifest": async (body) => {
        finished = JSON.parse(JSON.stringify(body.manifest));
        const gone = new Set(body.left_out.map((item) => item.path));
        finished.projects[0].files = finished.projects[0].files.filter((record) => !gone.has(record.path));
        for (const record of [finished.projects[0].entry, ...finished.projects[0].files, ...finished.media]) {
            record.sha256 = body.sums[record.path].sha256;
        }
        finished.projects[0].revision = "r".repeat(64);
        finished.left_out = body.left_out;
        return [200, finished];
    },
};

const saved = new FileHandle("p1.plexora");
let picker = 0;
frame.showSaveFilePicker = (options) => {
    picker += 1;
    frame.pickerOptions = options;
    return Promise.resolve(saved);
};

let exported = null;
await check("export: the Save dialog opens on the click, before anything is awaited", async () => {
    const asked = transport.chooseDestination({ name: "p1.plexora" });
    assert.equal(picker, 1);
    assert.equal(frame.pickerOptions.suggestedName, "p1.plexora");
    const destination = await asked;
    assert.ok(destination && destination.dest);
    exported = destination.dest;
});

await check("export: the store is written back, then planned, then packed, the manifest first", async () => {
    const first = await transport.startExport({ ids: ["p1"], include: { tables: true }, dest: exported });
    assert.equal(first.kind, "export");
    assert.equal(first.file.name, "p1.plexora");
    const seen = [];
    const status = await until("export", first.job, seen);
    assert.equal(status.status, "ready", status.error);
    for (const stage of ["snapshot", "pack", "verify"]) assert.ok(seen.includes(stage), seen.join(","));
    const order = calls.map((call) => call[0] === "POST" ? call[1] : call[0]);
    assert.ok(order.indexOf("flush") < order.indexOf("/projects/export/plan"));
    assert.ok(order.indexOf("/projects/export/plan") < order.indexOf("/projects/export/manifest"));
    const entries = await codec.readCentral(new Blob([saved.bytes]));
    assert.equal(entries[0].name, codec.MANIFEST);
    assert.equal(entries[0].method, codec.STORED);
    assert.equal(status.file.size, saved.bytes.length);
    assert.equal(status.result.projects[0].revision, "r".repeat(64));
    exported = { status, blob: new Blob([saved.bytes]), entries };
});

await check("export: each member from where it is -- the folder, a pick, Python's work folder", async () => {
    const { blob, entries } = exported;
    const byName = new Map(entries.map((entry) => [entry.name, entry]));
    const read = async (name) => codec.readMember(blob, byName.get(name), 1 << 30);
    assert.deepEqual(await read("projects/p1/state/cells.csv"), csv);
    assert.deepEqual(await read("projects/p1/entry.json"), work["/tmp/plexora-bundle-1/00000.bin"]);
    assert.deepEqual(await read("media/k1/slide.ome.tif"), new Uint8Array(70000).fill(9));
    assert.equal(byName.get("projects/p1/state/cells.csv").method, codec.DEFLATED);
    const manifest = JSON.parse(new TextDecoder().decode(await read(codec.MANIFEST)));
    assert.equal(manifest.projects[0].files.find((r) => r.path.endsWith("cells.csv")).sha256, sha(csv));
    assert.ok(!calls.some((call) => call[0] === "readFile" && call[1].startsWith("/data/")), "a store file read through Python");
    assert.ok(!byName.has("projects/p1/state/gone.json"));
    assert.deepEqual(exported.status.result.left_out, [{ path: "projects/p1/state/gone.json", why: "gone before it was packed" }]);
    assert.equal(calls.find((call) => call[1] === "/projects/export/manifest")[2].work_dir, "/tmp/plexora-bundle-1");
});

await check("export: delivered where it was saved", async () => {
    assert.deepEqual(await transport.deliver(exported.status), { saved: true, name: "p1.plexora" });
    assert.equal(clicks.length, 0);
});

await check("export: without a Save dialog, written into this site's storage and downloaded from there", async () => {
    delete frame.showSaveFilePicker;
    const destination = await transport.chooseDestination({ name: "p1" });
    assert.deepEqual(destination, { dest: null });
    const first = await transport.startExport({ ids: ["p1"], include: {}, dest: null });
    const status = await until("export", first.job);
    assert.equal(status.status, "ready", status.error);
    assert.equal(status.file.name, "p1.plexora");
    const kept = opfs.at(`plexora/bundles/${first.job}/p1.plexora`);
    assert.ok(kept && kept.bytes.length === status.file.size);
    assert.deepEqual(await transport.deliver(status), { downloaded: true });
    assert.equal(clicks.at(-1).download, "p1.plexora");
    assert.equal(frame.lastBlob.size, status.file.size);
    // The next export clears the one before.
    const next = await transport.startExport({ ids: ["p1"], include: {}, dest: null });
    await until("export", next.job);
    assert.ok(!opfs.at(`plexora/bundles/${first.job}`) && opfs.at(`plexora/bundles/${next.job}/p1.plexora`));
});

await check("export: a cancelled one stops and takes its file away", async () => {
    store.put("p1/big.csv", new Uint8Array(24 << 20).fill(65));
    plan.members.push({ bundle_path: "projects/p1/state/big.csv", source: "/data/p1/big.csv", size: 24 << 20 });
    const handle = new FileHandle("stop.plexora");
    frame.showSaveFilePicker = () => Promise.resolve(handle);
    const { dest } = await transport.chooseDestination({ name: "stop" });
    const first = await transport.startExport({ ids: ["p1"], include: {}, dest });
    for (let i = 0; i < 200 && (await transport.status("export", first.job)).stage !== "pack"; i += 1) await tick();
    await transport.cancel("export", first.job);
    const status = await until("export", first.job);
    assert.equal(status.status, "cancelled");
    assert.ok(handle.aborted && handle.removed);
    plan.members.pop();
});

await check("export: a manifest Python refuses takes the file away", async () => {
    const keep = routes["POST /projects/export/manifest"];
    routes["POST /projects/export/manifest"] = async () => [400, { error: "Send the manifest and the sums." }];
    const handle = new FileHandle("refused.plexora");
    frame.showSaveFilePicker = () => Promise.resolve(handle);
    const { dest } = await transport.chooseDestination({ name: "refused" });
    const first = await transport.startExport({ ids: ["p1"], include: {}, dest });
    const status = await until("export", first.job);
    assert.equal(status.status, "failed");
    assert.equal(status.error, "Send the manifest and the sums.");
    assert.ok(handle.aborted && handle.removed);
    routes["POST /projects/export/manifest"] = keep;
});

// -- import ---------------------------------------------------------------------------

const bundleFile = new File([exported.blob], "p1.plexora");
let mounted = null;
await check("import: the picked file is mounted for Python, which previews it from the file", async () => {
    routes["POST /projects/import/preview"] = async (body) => [200, { format: 2, projects: [{ id: "p1", status: "new" }],
                                                                  placements: body.place_from !== undefined ? placements() : undefined }];
    const preview = await transport.preview({ file: bundleFile }, null);
    mounted = preview.path;
    assert.match(mounted, /^\/picked\/[a-z0-9]+\/p1\.plexora$/i);
    assert.equal(preview.upload, null);
    const asked = calls.filter((call) => call[1] === "/projects/import/preview").at(-1)[2];
    assert.deepEqual(asked, { path: mounted, location: null });
});

const csvEntry = exported.entries.find((entry) => entry.name === "projects/p1/state/cells.csv");
let wrongSha = false;
function placements() {
    return [{ path: "projects/p1/state/cells.csv", target: "/data/p1_2/cells.csv", size: csvEntry.usize,
              sha256: wrongSha ? "0".repeat(64) : sha(csv) },
            // One only Python can write: a store the worker cannot open.
            { path: "media/k1/slide.ome.tif", target: "/library/x/p1_2/media/k1/slide.ome.tif", size: 70000, sha256: null }];
}

let pythonStatus = "ready";
routes["POST /projects/import"] = async (body) => [202, { job: "py1", kind: "import", status: "pending", progress: 0,
                                                          stage: "verify", placed: body.placed }];
routes["GET /projects/import/*"] = async () => [200, { job: "py1", kind: "import", status: pythonStatus, progress: 100,
                                                        stage: pythonStatus, error: pythonStatus === "failed" ? "the disk went away" : null,
                                                        result: { projects: [{ target: "p1_2" }] } }];

await check("import: Python's large members unpacked into the store, checked, and adopted", async () => {
    const first = await transport.startImport({ source: { path: mounted }, choices: { p1: "import" }, location: null });
    const status = await until("import", first.job);
    assert.equal(status.status, "ready", status.error);
    assert.deepEqual(store.at("p1_2/cells.csv").bytes, csv);
    assert.deepEqual(adopted, ["/data/p1_2/cells.csv"]);
    const started = calls.filter((call) => call[1] === "/projects/import").at(-1)[2];
    assert.deepEqual(started.placed, { "projects/p1/state/cells.csv": "/data/p1_2/cells.csv" });
    assert.deepEqual(started.choices, { p1: "import" });
    assert.equal(calls.filter((call) => call[1] === "/projects/import/preview").at(-1)[2].place_from, 64 * 1024);
    assert.deepEqual(status.result, { projects: [{ target: "p1_2" }] });
});

await check("import: a failed import takes back what the worker wrote", async () => {
    pythonStatus = "failed";
    store.children.delete("p1_2");
    adopted.length = 0;
    const pullsBefore = pulls;
    const first = await transport.startImport({ source: { path: mounted }, choices: {}, location: null });
    const status = await until("import", first.job);
    assert.equal(status.status, "failed");
    assert.equal(status.error, "the disk went away");
    assert.deepEqual(adopted, ["/data/p1_2/cells.csv"]);
    assert.ok(!store.at("p1_2/cells.csv") && !store.at("p1_2"), "the file and the folder it made are gone");
    assert.ok(pulls > pullsBefore, "Python is told the folder changed");
    pythonStatus = "ready";
});

await check("import: a member that does not match is refused, nothing left, Python never asked", async () => {
    wrongSha = true;
    adopted.length = 0;
    const before = calls.filter((call) => call[1] === "/projects/import").length;
    const first = await transport.startImport({ source: { path: mounted }, choices: {}, location: null });
    const status = await until("import", first.job);
    assert.equal(status.status, "failed");
    assert.match(status.error, /does not match/);
    assert.ok(!store.at("p1_2/cells.csv") && !store.at("p1_2"));
    assert.equal(adopted.length, 0);
    assert.equal(calls.filter((call) => call[1] === "/projects/import").length, before);
    wrongSha = false;
});

await check("find missing: the browser's own dialog, then Python told what was found", async () => {
    routes["GET /project/p1_2/files_here"] = async () => [200, { missing: [{ recorded: "/x/slide.ome.tif", name: "slide.ome.tif" },
                                                                         { recorded: "/x/mask.tif", name: "mask.tif" }] }];
    routes["POST /project/p1_2/files_here"] = async (body) => [200, { ok: true, found: body.found }];
    found = { "/x/slide.ome.tif": "/picked/zz/slide.ome.tif" };
    assert.equal(transport.picksFolderItself, true);
    const answer = await transport.findMissing("p1_2");
    assert.equal(answer.found, 1);
    assert.deepEqual(answer.missing.map((item) => item.recorded), ["/x/mask.tif"]);
    assert.deepEqual(calls.filter((call) => call[1] === "/project/p1_2/files_here").at(-1)[2], { found });
});

await check("the rest is the server transport's", async () => {
    assert.deepEqual(await transport.estimate({ ids: ["p1"] }), { total: 1 });
    assert.equal(transport.available(), true);
    assert.equal(transport.canReveal(), false);
});

console.log(`${results.length} checks passed`);
process.exit(0);
