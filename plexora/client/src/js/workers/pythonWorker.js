// Plexora's Python, in the website's worker: Pyodide, the base stage, and the
// Flask app built inside it (plexora/browser/boot.py). The page's fetch facade
// (services/backend.js, through services/pythonClient.js) sends the requests
// it classes "python" here, and plexora/browser/bridge.py answers them.
//
// Messages from the page (each answered with {id, ok, result | error}):
//   boot {base, origin, cancels}   load Pyodide, the base stage and the app
//   request {method, path, query, headers, body}  -> {status, headers, body, stream}
//   read {stream, size}            the rest of a long body -> {body, done}
//   close {stream}                 the page stopped reading it
//   mount {dir, files}             the user's picked files, read on demand (WORKERFS)
//   columns {key, names, buffers}  a table's columns from the page's column store
//   heap                           the wasm heap's bytes
//   run {code}                     tests only (boot with debug: true): Python's
//                                  last expression, as text
// Events posted to the page, unasked: {event: {kind: "job", job}} as a job's
// record is written, {event: {kind: "wakeup"}} when a held poll should ask
// again, {event: {kind: "stage", stage, ms}} as a stage arrives, {mark, at}.
//
// Background work (jobs, builds) is queued by Python and run here, a slice at
// a time (cooperative.step), only while no request is waiting: a request is
// never held behind a job. While Python sleeps inside a job waiting on the
// user (`idle`, through JSPI), requests are answered in the gap.

let py = null;
let base = "";
let origin = "";
let stagesInfo = null;
const columnStore = new Map();
const queue = [];
let busy = false;
let idling = 0;
let syncPending = false;
let nativeFs = null;
let debug = false;
const at = () => performance.timeOrigin + performance.now();
const mark = (name) => self.postMessage({ mark: name, at: at() });

// WORKERFS reads a picked file with one FileReaderSync slice per read() call,
// about 0.4 ms each however small, and tifffile reads a header in thousands of
// small reads. Picked files are read in 1 MiB blocks instead, the last 64 kept,
// and reads over 4 MiB go straight through (P9).
const BLOCK = 1 << 20;
const KEEP = 64;

function blockReads(workerfs) {
    const reader = new FileReaderSync();
    const blocks = new Map();
    const slice = (node, from, to) => new Uint8Array(reader.readAsArrayBuffer(node.contents.slice(from, to)));
    const block = (node, index) => {
        const key = `${node.id}:${index}`;
        let data = blocks.get(key);
        if (data) blocks.delete(key);
        else data = slice(node, index * BLOCK, (index + 1) * BLOCK);
        blocks.set(key, data);
        if (blocks.size > KEEP) blocks.delete(blocks.keys().next().value);
        return data;
    };
    workerfs.stream_ops.read = (stream, buffer, offset, length, position) => {
        const node = stream.node;
        const end = Math.min(node.size, position + length);
        if (position >= end) return 0;
        if (end - position > 4 * BLOCK) {
            buffer.set(slice(node, position, end), offset);
            return end - position;
        }
        for (let p = position; p < end;) {
            const index = Math.floor(p / BLOCK);
            const data = block(node, index);
            const from = p - index * BLOCK;
            const n = Math.min(data.length - from, end - p);
            buffer.set(data.subarray(from, from + n), offset + (p - position));
            p += n;
        }
        return end - position;
    };
}

async function fetchWheel(name) {
    const response = await fetch(`${base}wheels/${name}`);
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
    return response.arrayBuffer();
}

async function installWheels(names) {
    const site = py.runPython("import site; site.getsitepackages()[0]");
    for (const buffer of await Promise.all(names.map(fetchWheel))) {
        py.unpackArchive(buffer, "wheel", { extractDir: site });
    }
}

// A stage, loaded when Python first imports one of its modules
// (plexora/browser/stages.py); Python waits through JSPI.
async function loadStage(name, importer = "?") {
    const stage = stagesInfo[name];
    if (!stage) throw new Error(`no stage ${name}`);
    const started = performance.now();
    const wheels = installWheels(stage.wheels);
    if (stage.pyodide.length) await py.loadPackage(stage.pyodide, { messageCallback: () => {} });
    await wheels;
    self.postMessage({ event: { kind: "stage", stage: name, importer,
                                ms: Math.round(performance.now() - started) } });
}

// The page's cancel flags: job ids it wrote into shared memory while this
// worker was busy (services/pythonClient.js writes them).
let cancels = null;
function cancelled(jobId) {
    if (!cancels) return false;
    const text = new TextDecoder().decode(new Uint8Array(cancels).slice());
    return text.split("\n").includes(jobId);
}

async function mountState() {
    // The project store: OPFS plexora/state, mounted where the app keeps its
    // data root. Written back after every request that may have changed it.
    try {
        const root = await navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        const state = await plexora.getDirectoryHandle("state", { create: true });
        nativeFs = await py.mountNativeFS("/data", state);
        return "opfs";
    } catch (error) {
        py.FS.mkdirTree("/data");
        return `memory (${error?.message || error})`;
    }
}

function scheduleSync() {
    if (!nativeFs || syncPending) return;
    syncPending = true;
    setTimeout(async () => {
        syncPending = false;
        try { await nativeFs.syncfs(); } catch (error) { self.postMessage({ log: `syncfs: ${error}` }); }
    }, 0);
}

async function boot(args) {
    base = args.base.endsWith("/") ? args.base : `${args.base}/`;
    origin = args.origin;
    if (args.cancels) cancels = args.cancels;
    debug = Boolean(args.debug);
    const phases = { start: at() };
    stagesInfo = await (await fetch(`${base}stages.json`)).json();
    const wheels = Promise.all(stagesInfo.base.wheels.map(fetchWheel));
    const { loadPyodide } = await import(`${base}pyodide/pyodide.mjs`);
    py = await loadPyodide({ indexURL: `${base}pyodide/` });
    blockReads(py.FS.filesystems.WORKERFS);
    phases.core = at(); mark("core");
    await py.loadPackage(stagesInfo.base.pyodide, { messageCallback: () => {} });
    phases.packages = at(); mark("packages");
    const site = py.runPython("import site; site.getsitepackages()[0]");
    for (const buffer of await wheels) py.unpackArchive(buffer, "wheel", { extractDir: site });
    phases.wheels = at(); mark("wheels");
    const store = await mountState();
    py.FS.mkdirTree("/bioc");
    py.FS.mkdirTree("/picked");
    self.plexoraPost = (text) => self.postMessage({ event: JSON.parse(text) });
    self.plexoraCancelled = cancelled;
    self.plexoraLoadStage = loadStage;
    self.plexoraColumns = (src) => columnStore.get(src) || null;
    py.globals.set("ORIGIN", origin);
    const info = JSON.parse(await py.runPythonAsync(`
import json
import js
from pyodide.ffi import run_sync
from plexora.browser import boot, stages

def _post(message):
    js.plexoraPost(json.dumps(message))

def _idle(seconds):
    import asyncio
    js.self.plexoraIdle(1)
    try:
        run_sync(asyncio.sleep(seconds))
    finally:
        js.self.plexoraIdle(-1)

def _columns(spec):
    held = js.plexoraColumns(str(spec.src))
    if held is None:
        return None
    import numpy as np
    import polars as pl
    names = held.names.to_py()
    return pl.DataFrame({name: np.frombuffer(held.buffers[i].to_py(), dtype="<f8")
                         for i, name in enumerate(names)})

_finder = stages.install(lambda name: run_sync(js.plexoraLoadStage(name, _finder.importers.get(name, "?"))))
json.dumps(boot.start(ORIGIN, post=_post, cancelled=lambda job: bool(js.plexoraCancelled(job)),
                      idle=_idle, columns=_columns))
`));
    phases.ready = at(); mark("ready");
    return { phases, info, store, stages: Object.fromEntries(
        Object.entries(stagesInfo).map(([name, stage]) => [name, stage.bytes])) };
}

self.plexoraIdle = (delta) => {
    idling += delta;
    if (delta > 0) drain();
};

function toBytes(proxy) {
    const bytes = proxy.toJs();
    proxy.destroy?.();
    return bytes;
}

// Calls into Python that may wait inside it -- a stage loading on first
// import, a job's poll sleeping -- go through JSPI's promising entry, which is
// what lets `run_sync` suspend them. Where there is no JSPI (Firefox, Safari)
// the boot loads every stage first and nothing suspends.
function call(fn, ...args) {
    return typeof fn.callPromising === "function" && jspi ? fn.callPromising(...args)
        : Promise.resolve(fn(...args));
}
const jspi = typeof WebAssembly.Suspending === "function";

async function request(args) {
    const handle = py.pyimport("plexora.browser.bridge").handle;
    const app = py.pyimport("plexora").app;
    const message = py.toPy({ ...args, body: args.body ? new Uint8Array(args.body) : null });
    const answer = await call(handle, app, message, origin);
    const result = {
        status: answer.get("status"),
        headers: answer.get("headers").toJs(),
        body: toBytes(answer.get("body")),
        stream: answer.get("stream") ?? null,
    };
    if (answer.get("wrote")) scheduleSync();
    answer.destroy(); message.destroy(); handle.destroy(); app.destroy();
    return result;
}

function read({ stream, size }) {
    const bridge = py.pyimport("plexora.browser.bridge");
    const part = bridge.read(stream, size || (8 << 20));
    const result = { body: toBytes(part.get("body")), done: part.get("done") };
    part.destroy(); bridge.destroy();
    return result;
}

function mount({ dir, files }) {
    py.FS.mkdirTree(dir);
    py.FS.mount(py.FS.filesystems.WORKERFS, { files }, dir);
    return files.map((file) => `${dir}/${file.name}`);
}

const OPS = {
    boot,
    request,
    read,
    close: ({ stream }) => { py.pyimport("plexora.browser.bridge").close(stream); return true; },
    mount,
    columns: ({ key, names, buffers }) => { columnStore.set(key, { names, buffers }); return names.length; },
    forget: ({ key }) => columnStore.delete(key),
    heap: () => (py ? py._module.HEAP8.length : null),
    sync: async () => {
        if (!debug) throw new Error("sync is for tests: boot with debug: true");
        const started = performance.now();
        if (nativeFs) await nativeFs.syncfs();
        return performance.now() - started;
    },
    run: async ({ code }) => {
        if (!debug) throw new Error("run is for tests: boot with debug: true");
        const value = await py.runPythonAsync(code);
        return value === undefined || value === null ? null : String(value);
    },
};

// Requests go in order; one at a time unless Python is asleep inside a job
// waiting on the user, when the next request is answered in the gap.
async function drain() {
    while (queue.length && (!busy || idling > 0)) {
        const { id, op, args } = queue.shift();
        const nested = busy;
        busy = true;
        try {
            const result = await OPS[op](args || {});
            const transfer = result && result.body instanceof Uint8Array ? [result.body.buffer] : [];
            self.postMessage({ id, ok: true, result }, transfer);
        } catch (error) {
            self.postMessage({ id, ok: false, error: String(error?.message || error).slice(-6000) });
        } finally {
            busy = nested;
        }
    }
    if (!busy && !queue.length) step();
}

// Background work, a slice at a time, while nothing is waiting.
let stepping = false;
function step() {
    if (!py || stepping || busy || queue.length) return;
    stepping = true;
    setTimeout(async () => {
        stepping = false;
        if (busy || queue.length) return;
        busy = true;
        let pending = 0;
        try {
            const cooperative = py.pyimport("plexora.browser.cooperative");
            const outcome = await call(cooperative.step, 50);
            pending = outcome.get("pending");
            outcome.destroy(); cooperative.destroy();
        } catch (error) {
            self.postMessage({ log: `step: ${error?.message || error}` });
        } finally {
            busy = false;
        }
        if (queue.length) drain();
        else if (pending) step();
    }, 0);
}

self.onmessage = ({ data }) => {
    queue.push(data);
    drain();
};
mark("loaded");
