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
//   mountTree {dir, blobs}         a picked folder: [{name: "<folder>/<path>", data: File}]
//   flush                          the project store written back now
//   useStore {folder}              the project store moved to a granted folder
//   answer {id, value, failed}     the page's answer to Python's question `id`
//                                  (handled at once, never queued: Python waits);
//                                  a `mount` {dir, files} in it is mounted first
//   nodeSpec {name}                what the browser node may serve for a project
//   licence {files}                the page's licence records written into /bioc
//                                  (plexora/browser/licence.py) -> the licence status
//   heap                           the wasm heap's bytes
//   run {code}                     tests only (boot with debug: true): Python's
//                                  last expression, as text
// Events posted to the page, unasked: {event: {kind: "job", job}} as a job's
// record is written, {event: {kind: "wakeup"}} when a held poll should ask
// again, {event: {kind: "stage", stage, ms}} as a stage arrives,
// {event: {kind: "ask", ask, id, args}} when Python needs what only the page
// knows (a picked table's columns, a picked image's maximum; the page answers
// with `answer`), {mark, at}.
//
// Background work (jobs, builds) is queued by Python and run here, a slice at
// a time (cooperative.step), only while no request is waiting: a request is
// never held behind a job. While Python sleeps inside a job waiting on the
// user (`idle`, through JSPI), requests are answered in the gap.

import { createStore } from "./storeSync.js";

let py = null;
let base = "";
let origin = "";
//: The site's base path, the app's `base_url` (services/shell.js `BASE`).
let siteBase = "";
let stagesInfo = null;
//: Python's questions to the page still waiting, by id (`ask`).
const asks = new Map();
let nextAsk = 1;
const queue = [];
let busy = false;
let idling = 0;
let syncPending = false;
//: The project store at /data (workers/storeSync.js), once mounted.
let store = null;
//: When the store last read in another app's changes (performance.now()).
let lastPull = 0;
const PULL_MS = 2000;
let debug = false;
//: The message being handled, for `state`.
let current = null;
//: Tests only (debug): when each message arrived, started and was answered.
const traced = [];
//: The page's count of messages posted and not yet received (shared memory).
let inFlight = null;
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

async function mountState(folder) {
    // The project store: a folder the user granted (services/browserStore.js),
    // or OPFS plexora/state, read into /data where the app keeps its data
    // root, and kept in step with it (workers/storeSync.js) -- never mirrored:
    // the desktop app may write the same folder.
    store = createStore(py.FS, "/data", (line) => self.postMessage({ log: line }));
    try {
        let handle = folder;
        if (!handle) {
            const root = await navigator.storage.getDirectory();
            const plexora = await root.getDirectoryHandle("plexora", { create: true });
            handle = await plexora.getDirectoryHandle("state", { create: true });
        }
        await store.open(handle);
        lastPull = performance.now();
        return folder ? "folder" : "opfs";
    } catch (error) {
        store = null;
        py.FS.mkdirTree("/data");
        return `memory (${error?.message || error})`;
    }
}

// One write back at a time, and at most one more queued behind it, a moment
// after it was asked for: a burst of requests is one more write-back, never
// several at once. Asked after every request and every background step: a
// GET writes too (a project's first load makes its database), and one with
// nothing new only looks.
const SYNC_DELAY_MS = 250;
let syncing = Promise.resolve();
function scheduleSync() {
    if (!store || syncPending) return;
    syncPending = true;
    syncing = syncing.then(async () => {
        await new Promise((resolve) => setTimeout(resolve, SYNC_DELAY_MS));
        syncPending = false;
        try { await store.push(); } catch (error) { self.postMessage({ log: `store: ${error}` }); }
    });
}

async function boot(args) {
    base = args.base.endsWith("/") ? args.base : `${args.base}/`;
    origin = args.origin;
    siteBase = String(args.siteBase || "");
    if (args.cancels) cancels = args.cancels;
    debug = Boolean(args.debug);
    inFlight = args.inFlight || null;
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
    const store = await mountState(args.store || null);
    py.FS.mkdirTree("/bioc");
    py.FS.mkdirTree("/picked");
    py.FS.mkdirTree("/derived");
    const derived = await mountDerived();
    self.plexoraPost = (text) => self.postMessage({ event: JSON.parse(text) });
    self.plexoraCancelled = cancelled;
    self.plexoraLoadStage = loadStage;
    self.plexoraAsk = ask;
    self.plexoraJ2KReady = j2kReady;
    self.plexoraJ2K = j2kDecode;
    self.plexoraWaiting = () => queue.length > 0 || (inFlight !== null && Atomics.load(inFlight, 0) > 0);
    py.globals.set("ORIGIN", origin);
    py.globals.set("SITE_BASE", siteBase);
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
        # No time (a long task's checkpoint): yield to whatever is queued --
        # the messages carrying requests -- rather than to a timer.
        run_sync(js.self.plexoraYield() if seconds <= 0 else asyncio.sleep(seconds))
    finally:
        js.self.plexoraIdle(-1)

def _absent(value):
    from pyodide.ffi import jsnull
    return value is None or value is jsnull

def _ask(kind, args):
    # A question only the page can answer; None when it cannot.
    value = run_sync(js.plexoraAsk(kind, js.JSON.parse(json.dumps(args))))
    return None if _absent(value) else value

def _ask_columns(src, kinds):
    # The page parses the picked file and hands its columns back
    # (services/browserTable.js); None when it cannot.
    import numpy as np
    absent = _absent
    held = _ask("columns", {"key": src, "kinds": kinds})
    if held is None:
        return None
    columns = []
    for column in held.columns:
        if absent(column):
            columns.append(None)
        elif column.kind == "num":
            missing = column.missing
            columns.append({"kind": "num", "values": np.frombuffer(column.values.to_py(), dtype="<f8"),
                            "missing": None if absent(missing) else np.frombuffer(missing.to_py(), dtype=np.uint8),
                            "bad": int(column.bad), "fractions": int(column.fractions)})
        else:
            columns.append({"kind": "str", "codes": np.frombuffer(column.codes.to_py(), dtype="<i4"),
                            "dictionary": column.dictionary.to_py()})
    return {"rows": int(held.rows), "columns": columns, "parts": int(held.parts), "ms": int(held.ms)}

def _columns(spec):
    from plexora.browser import columns as page_columns
    return page_columns.from_page(spec, _ask_columns)

def _j2k(data):
    # JPEG 2000 for tifffile (plexora/browser/imagecodecs_shim.py): OpenJPEG in
    # WebAssembly in this worker, loaded the first time a tile needs it.
    import numpy as np
    from pyodide.ffi import to_js
    run_sync(js.self.plexoraJ2KReady())
    found = js.self.plexoraJ2K(to_js(memoryview(bytes(data))))
    bits, signed, components = int(found.bits), bool(found.signed), int(found.components)
    dtype = np.dtype(("i" if signed else "u") + ("1" if bits <= 8 else "2"))
    pixels = np.frombuffer(found.data.to_py(), dtype=dtype)
    shape = (int(found.height), int(found.width)) + ((components,) if components > 1 else ())
    return pixels.reshape(shape)

from plexora.browser import imagecodecs_shim as _codecs
_codecs.set_jpeg2k(_j2k)

_finder = stages.install(lambda name: run_sync(js.plexoraLoadStage(name, _finder.importers.get(name, "?"))))
json.dumps(boot.start(ORIGIN, base_url=SITE_BASE, post=_post, cancelled=lambda job: bool(js.plexoraCancelled(job)),
                      idle=_idle, waiting=lambda: bool(js.plexoraWaiting()), columns=_columns,
                      ask=_ask))
`));
    phases.ready = at(); mark("ready");
    return { phases, info, store, derived, stages: Object.fromEntries(
        Object.entries(stagesInfo).map(([name, stage]) => [name, stage.bytes])) };
}

//: A long task's yield (its checkpoint): turns of the event loop until every
//: message the page has posted has been received -- and, the task idling, its
//: request answered -- or, without the page's count, one turn.
self.plexoraYield = async () => {
    const turn = () => new Promise((resolve) => setTimeout(resolve, 0));
    if (inFlight === null) return turn();
    for (let i = 0; i < 1000 && (Atomics.load(inFlight, 0) > 0 || queue.length); i += 1) await turn();
    return undefined;
};

self.plexoraIdle = (delta) => {
    idling += delta;
    if (delta > 0) drain();
};

// JPEG 2000 for tifffile in Python (imagecodecs_shim's `set_jpeg2k`):
// OpenJPEG in WebAssembly (client/external/openjpeg-1.3.6), loaded the first
// time a tile needs it and called synchronously after that.
const OPENJPEG = "/client/external/openjpeg-1.3.6/";
let openjpeg = null;

async function j2kReady() {
    if (!openjpeg) {
        const { default: factory } = await import(`${OPENJPEG}openjpegwasm_decode.mjs`);
        openjpeg = await factory({ locateFile: (name) => `${OPENJPEG}${name}` });
    }
    return true;
}

function j2kDecode(bytes) {
    const decoder = new openjpeg.J2KDecoder();
    try {
        decoder.getEncodedBuffer(bytes.length).set(bytes);
        decoder.decode();
        const frame = decoder.getFrameInfo();
        return { width: frame.width, height: frame.height, components: frame.componentCount,
                 bits: frame.bitsPerSample, signed: frame.isSigned,
                 data: new Uint8Array(decoder.getDecodedBuffer()) };
    } finally {
        decoder.delete();
    }
}

// What only the page can answer -- a picked table's columns, a picked
// image's full-resolution maximum -- asked from Python, which waits on the
// promise (run_sync). The page answers with `answer`, which is never queued:
// Python is suspended until it arrives.
function ask(kind, args) {
    return new Promise((resolve) => {
        const id = nextAsk++;
        asks.set(id, resolve);
        self.postMessage({ event: { kind: "ask", ask: kind, id, args } });
    });
}

function answered({ id, value, failed }) {
    if (failed) self.postMessage({ log: `the page could not answer: ${failed}` });
    if (value && value.mount) {
        // A file the page made for Python (a pyramid it built): mounted before
        // Python hears the answer that names it.
        try { mount(value.mount); } catch (error) { self.postMessage({ log: `mount: ${error}` }); }
        delete value.mount;
    }
    const resolve = asks.get(id);
    asks.delete(id);
    if (resolve) resolve(value === undefined ? null : value);
    return Boolean(resolve);
}

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
    const started = performance.now();
    const handle = py.pyimport("plexora.browser.bridge").handle;
    const app = py.pyimport("plexora").app;
    const message = py.toPy({ ...args, body: args.body ? new Uint8Array(args.body) : null });
    const answer = await call(handle, app, message, origin);
    const result = {
        status: answer.get("status"),
        headers: answer.get("headers").toJs(),
        body: toBytes(answer.get("body")),
        stream: answer.get("stream") ?? null,
        pyMs: Math.round(performance.now() - started),
    };
    scheduleSync();
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

//: Files read on demand under `dir`; a folder already mounted is left as it
//: is (a copy the page built is mounted at boot and may be announced again).
function mount({ dir, files }) {
    if (!(py.FS.analyzePath(dir).exists && py.FS.isMountpoint(py.FS.lookupPath(dir).node))) {
        py.FS.mkdirTree(dir);
        py.FS.mount(py.FS.filesystems.WORKERFS, { files }, dir);
    }
    return files.map((file) => `${dir}/${file.name}`);
}

// The pyramids the page built in earlier visits (services/browserPyramid.js,
// OPFS plexora/derived/<key>/, finished when its manifest is written), where
// the projects that use them recorded them: /derived/<key>/<name>.
async function mountDerived() {
    let count = 0;
    try {
        const root = await navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        const derived = await plexora.getDirectoryHandle("derived", { create: true });
        for await (const [key, handle] of derived.entries()) {
            if (handle.kind !== "directory") continue;
            try {
                const manifest = JSON.parse(await (await (await handle.getFileHandle("manifest.json")).getFile()).text());
                const file = await (await handle.getFileHandle(manifest.name)).getFile();
                if (file.size !== manifest.bytes) continue;
                mount({ dir: `/derived/${key}`, files: [file] });
                count += 1;
            } catch (error) {
                // Unfinished: built again when an import asks for it.
            }
        }
    } catch (error) {
        self.postMessage({ log: `derived: ${error?.message || error}` });
    }
    return count;
}

function mountTree({ dir, blobs }) {
    py.FS.mkdirTree(dir);
    py.FS.mount(py.FS.filesystems.WORKERFS, { blobs }, dir);
    return blobs.length;
}

const OPS = {
    boot,
    request,
    read,
    close: ({ stream }) => { py.pyimport("plexora.browser.bridge").close(stream); return true; },
    mount,
    mountTree,
    //: What the page's browser node may serve for a project (plexora/browser/node_spec.py).
    nodeSpec: async ({ name }) => {
        const module = py.pyimport("plexora.browser.node_spec");
        try {
            return JSON.parse(await call(module.describe_json, name));
        } finally {
            module.destroy();
        }
    },
    //: The licence the page holds (services/licenseClient.js), as the files
    //: `biocognia` reads, and Python's status after reading them.
    licence: async ({ files }) => {
        const module = py.pyimport("plexora.browser.licence");
        try {
            return JSON.parse(await call(module.adopt_json, JSON.stringify(files || {})));
        } finally {
            module.destroy();
        }
    },
    heap: () => (py ? py._module.HEAP8.length : null),
    //: The project store written back now (before the page copies it).
    flush: async () => {
        await syncing;
        if (store) await store.push();
        return true;
    },
    //: Keep the projects in `folder` from now on (services/browserStore.js
    //: has copied them there): the store written back, /data read from it.
    useStore: async ({ folder }) => {
        await syncing;
        if (store) await store.push();
        else store = createStore(py.FS, "/data", (line) => self.postMessage({ log: line }));
        await store.open(folder);
        lastPull = performance.now();
        return "folder";
    },
    sync: async () => {
        if (!debug) throw new Error("sync is for tests: boot with debug: true");
        const started = performance.now();
        if (store) await store.push();
        return performance.now() - started;
    },
    //: Tests: what another app changed in the store, read in now.
    pull: async () => (store ? store.pull() : null),
    trace: () => (debug ? traced.splice(0) : null),
    //: What the worker is doing, for the status chip and diagnostics.
    state: () => ({ busy, idling, queued: queue.map((m) => m.op === "request" ? m.args.path : m.op),
                    stepping, inFlight: inFlight === null ? null : Atomics.load(inFlight, 0),
                    current: current ? { ...current, forMs: Math.round(performance.now() - current.since) } : null }),
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
        // Between requests, now and then: what another app (the desktop app
        // sharing the projects folder) changed, read in before Python looks.
        if (!busy && store && queue[0].op === "request" && performance.now() - lastPull > PULL_MS) {
            lastPull = performance.now();
            busy = true;
            try { await store.pull(); } catch (error) { self.postMessage({ log: `store: ${error}` }); }
            busy = false;
            continue;
        }
        const { id, op, args, arrived } = queue.shift();
        const began = performance.now();
        const outer = current;
        current = { op, path: op === "request" ? args.path : null, since: began };
        const nested = busy;
        busy = true;
        try {
            const result = await OPS[op](args || {});
            const transfer = result && result.body instanceof Uint8Array ? [result.body.buffer] : [];
            self.postMessage({ id, ok: true, result }, transfer);
            if (debug && op === "request") {
                traced.push({ path: args.path, arrived: Math.round(arrived), began: Math.round(began),
                              done: Math.round(performance.now()), nested, idling });
            }
        } catch (error) {
            self.postMessage({ id, ok: false, error: String(error?.message || error).slice(-6000) });
        } finally {
            busy = nested;
            current = outer;
            // It may have queued work: the next step finds out.
            pendingWork = Math.max(pendingWork, 1);
        }
    }
    if (!busy && !queue.length) step();
}

// Background work, a slice at a time, while nothing is waiting -- and once
// requests have been quiet for a moment (a page opening asks for many things
// in bursts, and a job started between two of them holds the next), but never
// later than MAX_DEFER_MS after the last slice: a page that polls steadily
// must not starve its jobs.
// Work nobody is waiting for (`_work.spawn(background=True)`: a cache warmed
// ahead of being asked) waits for BACKGROUND_QUIET_MS without a request, and
// MAX_DEFER_MS never hurries it: a page opening asks one thing after another,
// and a GMM fit started in a gap holds the next for a second.
const QUIET_MS = 150;
const MAX_DEFER_MS = 500;
const BACKGROUND_QUIET_MS = 600;
let lastRequest = 0;
let lastStep = 0;
let stepping = false;
//: What the last step left queued: work someone waits on, and background.
let pendingWork = 0;
let pendingBackground = 0;
function step() {
    if (!py || stepping || busy || queue.length) return;
    stepping = true;
    const now = performance.now();
    const onlyBackground = pendingBackground > 0 && pendingWork === 0;
    const wait = onlyBackground ? Math.max(0, BACKGROUND_QUIET_MS - (now - lastRequest))
        : now - lastStep >= MAX_DEFER_MS ? 0 : Math.max(0, QUIET_MS - (now - lastRequest));
    setTimeout(async () => {
        stepping = false;
        if (busy || queue.length) return;
        busy = true;
        lastStep = performance.now();
        try {
            const cooperative = py.pyimport("plexora.browser.cooperative");
            const began = performance.now();
            const quiet = began - lastRequest >= BACKGROUND_QUIET_MS;
            const outcome = await call(cooperative.step, 50, quiet);
            pendingWork = outcome.get("pending");
            pendingBackground = outcome.get("background");
            const ms = Math.round(performance.now() - began);
            if (outcome.get("ran")) {
                self.postMessage({ event: { kind: "step", ms, names: outcome.get("names").toJs() } });
                // Background work writes too (an import's project, a job's
                // record): written back as a request's writes are, else they
                // wait in memory for the next writing request.
                scheduleSync();
            }
            outcome.destroy(); cooperative.destroy();
        } catch (error) {
            self.postMessage({ log: `step: ${error?.message || error}` });
        } finally {
            busy = false;
        }
        if (queue.length) drain();
        else if (pendingWork || pendingBackground) step();
    }, wait);
}

self.onmessage = ({ data }) => {
    if (inFlight !== null) Atomics.sub(inFlight, 0, 1);
    data.arrived = performance.now();
    if (data.op === "answer") {
        // Handled at once: Python is suspended waiting for it, and a queued
        // message would wait for Python.
        self.postMessage({ id: data.id, ok: true, result: answered(data.args) });
        return;
    }
    if (data.op === "state") {
        // Answered at once, whatever Python is doing: it reads only JS.
        self.postMessage({ id: data.id, ok: true, result: OPS.state() });
        return;
    }
    if (data.op === "request") lastRequest = performance.now();
    queue.push(data);
    drain();
};
mark("loaded");
