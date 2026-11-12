// Plexora's Python, in the website's worker: Pyodide, the base stage, and the
// Flask app built inside it (plexora/browser/boot.py). The page's fetch facade
// (services/backend.js, through services/pythonClient.js) sends the requests
// it classes "python" here, and plexora/browser/bridge.py answers them.
//
// Messages from the page (each answered with {id, ok, result | error}):
//   warm {base}                    start loading Pyodide and the base stage (never
//                                  answered, never queued: boot waits on it)
//   boot {base, origin, cancels, scratch?}  load Pyodide, the base stage and the app
//                                  (`scratch`: data in memory, no project store or copies)
//   request {method, path, query, headers, body}  -> {status, headers, body, stream}
//   read {stream, size}            the rest of a long body -> {body, done}
//   close {stream}                 the page stopped reading it
//   mount {dir, files}             the user's picked files, read on demand (WORKERFS)
//   mountTree {dir, blobs}         a picked folder: [{name: "<folder>/<path>", data: File}]
//   flush                          the project store written back now
//   useStore {folder}              the project store moved to a granted folder
//   libraries                      the folders added beside it mounted again
//                                  -> [{id, label, path, files, error}]
//   bundleStores / adopt {paths} / readFile {path}
//                                  a `.plexora` file's bytes, moved by the page's
//                                  own worker (BUNDLE_OPS, services/bundleWeb.js)
//   answer {id, value, failed}     the page's answer to Python's question `id`
//                                  (handled at once, never queued: Python waits);
//                                  a `mount` {dir, files} in it is mounted first
//   nodeSpec {name}                what the browser node may serve for a project
//   licence {files}                the page's licence records written into /bioc
//                                  (plexora/browser/licence.py) -> the licence status
//   heap                           the wasm heap's bytes
//   telemetry {hidden}             telemetry's turn now (the page's tab is being
//                                  hidden): counts into the queue, an upload if
//                                  due, the files kept (plexora/browser/telemetry.py)
//   run {code}                     tests only (boot with debug: true): Python's
//                                  last expression, as text
//   cancelJob {id}                 a job the page stopped, without shared memory
//                                  (never queued: Python reads it at its next yield)
//   quit                           the store written back, and this worker closed
//                                  (Help › Restart Python)
// Events posted to the page, unasked: {event: {kind: "job", job}} as a job's
// record is written, {event: {kind: "chat", conversation, event}} as an AI
// conversation records an event, {event: {kind: "wakeup"}} when a held poll
// should ask again, {event: {kind: "stage", stage, ms}} as a stage arrives,
// {event: {kind: "ask", ask, id, args}} when Python needs what only the page
// knows (a picked table's columns, a picked image's maximum, magic select's
// model run; the page answers with `answer`), {mark, at}.
//
// Background work (jobs, builds) is queued by Python and run here, a slice at
// a time (cooperative.step), only while no request is waiting: a request is
// never held behind a job. While Python sleeps inside a job waiting on the
// user (`idle`, through JSPI), or waits on the network (`_wait_on`: the AI
// gateway's answer, read as it arrives), requests are answered in the gap.

import { createStore } from "./storeSync.js";
import { idbFolder } from "./idbFolder.js";
import { createTelemetryStore, indexedKv } from "./telemetryStore.js";
import { openCacheFiles } from "./cacheFiles.js";
import { jpegxrDecode } from "./jpegxr.js";

// -- the page: this worker's own, or one tab's ---------------------------------
//
// Where the browser keeps a SharedWorker a while after its last page
// (services/pythonClient.js `keepsAcrossReload`), this worker is one tab's,
// named for it, and every page the tab shows connects: a reload connects
// again to the Python the last page left, its projects loaded. The latest
// page is the one answered. What the page before asked and was not answered
// is dropped (its answers would carry ids the new page uses for other
// things), and what Python had asked it is answered "nothing", the asks' own
// fallback. Once no page of the tab has come back for PAGE_GONE_MS (the tab
// closed), the worker closes itself (`watchPage`) rather than hold its heap
// for the browser's minutes.
const tabWorker = typeof SharedWorkerGlobalScope === "function" && self instanceof SharedWorkerGlobalScope;
//: The page answered now (a tab worker's), its number, and what was posted
//: before the first one connected.
let page = null;
let pageNumber = 0;
const unsent = [];
const PAGE_GONE_MS = 30000;
let watching = false;
let goneTimer = null;
if (tabWorker) {
    self.postMessage = (message, transfer) => {
        if (page) page.postMessage(message, transfer || []);
        else if (unsent.length < 200) unsent.push([message, transfer || []]);
    };
    self.onconnect = (event) => { attach(event.ports[0]); };
}

function attach(port) {
    pageNumber += 1;
    const number = pageNumber;
    if (page) leftBehind(number);
    clearTimeout(goneTimer);
    goneTimer = null;
    page = port;
    port.onmessage = (event) => {
        if (event.data && typeof event.data === "object") event.data.page = number;
        self.onmessage(event);
    };
    port.onmessageerror = () => self.postMessage({ log: "a message from the page could not be read" });
    port.start();
    for (const [message, transfer] of unsent.splice(0)) port.postMessage(message, transfer);
    // Whether the Python this page found was already running (a reload).
    port.postMessage({ attached: { booted: Boolean(booted), page: number, fatal: fatalTold } });
    watchPage();
}

//: The page before `number` is gone: its queued and retried requests are
//: dropped, and Python's questions to it answered with nothing.
function leftBehind(number) {
    for (let i = queue.length - 1; i >= 0; i -= 1) {
        if (queue[i].page !== undefined && queue[i].page < number) queue.splice(i, 1);
    }
    // Every retry waiting is an earlier page's: this one has asked nothing yet.
    for (const timer of retrying.values()) clearTimeout(timer);
    retrying.clear();
    for (const [id, resolve] of [...asks]) {
        asks.delete(id);
        resolve(null);
    }
}

//: Whether the page that sent `message` is still the one answered.
const pageNow = (message) => message.page === undefined || message.page === pageNumber;

// The tab's page holds a lock named as this worker for as long as it lives
// (services/pythonClient.js `holdTab`); the lock coming to this worker means
// the page let it go -- reloading, or closed. A page of the tab connecting
// within PAGE_GONE_MS keeps this worker; none, and it closes.
function watchPage() {
    if (!tabWorker || watching || !self.navigator || !self.navigator.locks || !self.name) return;
    watching = true;
    self.navigator.locks.request(self.name, () => {
        watching = false;
        const left = pageNumber;
        clearTimeout(goneTimer);
        goneTimer = setTimeout(() => {
            if (pageNumber === left) quit();
        }, PAGE_GONE_MS);
        // Released at once: the page a reload brings takes it next.
    }).catch(() => { watching = false; });
}

async function quit() {
    try {
        await syncing;
        if (store) await store.push();
        await pushLibraries();
    } catch (error) {
        // Closing regardless.
    }
    self.close();
}

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
//: id -> timer of a request a route asked to be asked again later (`drain`).
const retrying = new Map();
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
//: Whether the page has been told Python failed for good (`fatal` event).
let fatalTold = false;
//: Past this heap a fatal failure is taken for running out of memory: the
//: worker's WebAssembly memory stops at 4 GiB, and an allocation that fails
//: there (polars' Rust aborts) ends Pyodide.
const FATAL_MEMORY_BYTES = 3.5 * 2 ** 30;
//: A cause that is running out of memory whatever the heap says: the page's
//: own buffers (the files under /data are typed arrays outside the heap) can
//: fail to grow while the heap is small.
const OUT_OF_MEMORY = /out of memory|allocation failed|cannot enlarge memory|\boom\b|MemoryError/i;
//: What ended Python here, once it has: {cause, heap, memory}.
let fatalWith = null;

//: Python failed for good (Pyodide's `on_fatal`, or the first call after
//: that says so): the page is told once, with the cause, and every later
//: request is answered at once instead of by a Python that cannot.
function fatal(error, module = py && py._module) {
    if (fatalTold) return;
    fatalTold = true;
    const heap = module && module.HEAP8 ? module.HEAP8.length : 0;
    const cause = String((error && (error.message || error.name)) || error || "").slice(0, 400);
    const memory = heap >= FATAL_MEMORY_BYTES || OUT_OF_MEMORY.test(cause)
        || OUT_OF_MEMORY.test(String(error && error.name || ""));
    fatalWith = { cause, heap, memory };
    self.postMessage({ log: `Python stopped: ${cause} (heap ${(heap / 2 ** 30).toFixed(2)} GiB)` });
    self.postMessage({ event: { kind: "fatal", memory, heap, cause } });
}
const at = () => performance.timeOrigin + performance.now();
const mark = (name) => self.postMessage({ mark: name, at: at() });

// WORKERFS reads a picked file with one FileReaderSync slice per read() call,
// about 0.4 ms each however small, and tifffile reads a header in thousands of
// small reads. Picked files are read in 1 MiB blocks instead, the last 64 kept,
// and reads over 4 MiB go straight through (P9). A read that misses is one
// round trip to wherever the file lies, after the last, and from a share it is
// the bytes that cost (Chrome, the lab share, a block not read lately: 64 KiB
// ~20 ms, 1 MiB ~220 ms). What a TIFF's open will read is read ahead instead,
// in 64 KiB pieces, every piece a step of its walk names at once
// (plexora/browser/tiff_ahead.py, through `plexoraReadAhead`).
const BLOCK = 1 << 20;
const KEEP = 64;
const PIECE = 64 << 10;
//: Pieces held at most (64 MiB), and read by one read ahead: the read
//: ahead's, the small reads' (`SMALL`) and the page's prefetch.
const PIECES = 1024;
//: A read this small that misses, and does not go on from where the last
//: read of the file ended, is answered from the 64 KiB piece around it,
//: kept, not from a 1 MiB block. Python's buffered reader asks for its
//: whole buffer (128 KiB in Pyodide's 3.14) after every seek, and what a
//: TIFF's open seeks to is directories
//: and their tile tables, a few hundred bytes to a few KB each, and where
//: they lie across a whole file -- a 40-channel, 7-level OME-TIFF of 106 GB
//: has 280 of them, each beside its own plane -- every one cost a 1 MiB read
//: (~0.25 s from a file share), and 280 blocks were more than the 64 kept,
//: so each of an import's opens read them all again: 642 s of /import/inspect.
//: A piece costs about what a 4 KB read does (~20 ms there). A read that
//: goes on from the last (a table read start to end) keeps its blocks.
const SMALL = 128 << 10;

// Python lets go of its lock around a file read, as around any blocking call.
// WebKit runs FinalizationRegistry callbacks inside FileReaderSync's read
// (a nested run loop; Chromium and Firefox run nothing there), and Pyodide's
// callback, finding the lock let go, aborts the worker ("NoGilError"). So the
// read takes the lock back while it reads, as a C extension's callback would.
function withGil(module, read) {
    if (!module._PyGILState_Check || module._PyGILState_Check()) return read();
    const state = module._PyGILState_Ensure();
    try {
        return read();
    } finally {
        module._PyGILState_Release(state);
    }
}

//: -> readAhead(node, indexes): those pieces of the picked file `node` not
//: held, read at once -- each run of neighbours as one slice -- and held;
//: resolves how many were read.
//: Reads of picked files, timed per file for the page's "slow drive" notice
//: (services/handoffNotice.js `pace`): what a read waited is reported as
//: intervals, closed after PACE_GAP_MS quiet or PACE_EVERY_MS busy, as
//: plexora/server/utils/read_pace.py reports the network's.
const PACE_EVERY_MS = 1000;
const PACE_GAP_MS = 250;
const drivePaces = new Map();
function drivePace(name, began, ended, bytes) {
    const source = String(name || "");
    let state = drivePaces.get(source);
    if (state && began - state.end > PACE_GAP_MS) {
        reportPace(source, state);
        state = null;
    }
    if (!state) {
        state = { start: began, end: ended, busy: 0, bytes: 0 };
        drivePaces.set(source, state);
    }
    state.end = Math.max(state.end, ended);
    // Reads here are synchronous, one after another: their times add up.
    state.busy += Math.max(0, ended - began);
    state.bytes += bytes;
    if (state.end - state.start >= PACE_EVERY_MS) {
        reportPace(source, state);
        drivePaces.delete(source);
    }
}
function reportPace(source, state) {
    self.postMessage({ event: { kind: "pace", cause: "drive", source, start: state.start,
                                end: state.end, busy: state.busy, bytes: state.bytes } });
}

function blockReads(workerfs, module) {
    const reader = new FileReaderSync();
    const blocks = new Map();
    const pieces = new Map();
    //: Where the last read of each file ended: a read from there is sequential.
    const lastEnd = new Map();
    const slice = (node, from, to) => withGil(module, () => {
        const began = Date.now();
        const bytes = new Uint8Array(reader.readAsArrayBuffer(node.contents.slice(from, to)));
        drivePace(node.name, began, Date.now(), bytes.length);
        return bytes;
    });
    const block = (node, index) => {
        const key = `${node.id}:${index}`;
        let data = blocks.get(key);
        if (data) blocks.delete(key);
        else data = slice(node, index * BLOCK, (index + 1) * BLOCK);
        blocks.set(key, data);
        if (blocks.size > KEEP) blocks.delete(blocks.keys().next().value);
        return data;
    };
    //: A piece held, the oldest let go past PIECES.
    const keep = (node, index, data) => {
        pieces.set(`${node.id}:${index}`, data);
        if (pieces.size > PIECES) pieces.delete(pieces.keys().next().value);
    };
    // What pieces hold from `position` on, up to `end`, copied into `buffer`:
    // how many bytes (none when `position` is in no piece).
    const held = (node, buffer, offset, position, end) => {
        let p = position;
        while (p < end) {
            const index = Math.floor(p / PIECE);
            const data = pieces.get(`${node.id}:${index}`);
            const from = p - index * PIECE;
            if (!data || from >= data.length) break;
            const n = Math.min(data.length - from, end - p);
            buffer.set(data.subarray(from, from + n), offset + (p - position));
            p += n;
        }
        return p - position;
    };
    workerfs.stream_ops.read = (stream, buffer, offset, length, position) => {
        const node = stream.node;
        const end = Math.min(node.size, position + length);
        if (position >= end) return 0;
        // A read that starts in what was read ahead is answered from it alone,
        // short when it runs on past: a buffered reader asks for more than it
        // was asked, and asks again for what it wanted, as of any file that
        // answers short -- no block is read for what nobody wanted.
        const sequential = lastEnd.get(node.id) === position;
        const ahead = pieces.size ? held(node, buffer, offset, position, end) : 0;
        if (ahead) {
            lastEnd.set(node.id, position + ahead);
            return ahead;
        }
        lastEnd.set(node.id, end);
        if (end - position <= SMALL && !sequential) {
            const index = Math.floor(position / PIECE);
            keep(node, index, slice(node, index * PIECE, Math.min((index + 1) * PIECE, node.size)));
            const answered = held(node, buffer, offset, position, end);
            lastEnd.set(node.id, position + answered);
            return answered;
        }
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
    return async (node, indexes) => {
        const wanted = [...new Set(Array.from(indexes, Number))]
            .filter((index) => Number.isInteger(index) && index >= 0 && index * PIECE < node.size
                    && !pieces.has(`${node.id}:${index}`))
            .sort((a, b) => a - b)
            .slice(0, PIECES);
        const runs = [];
        for (const index of wanted) {
            const last = runs[runs.length - 1];
            if (last && last.end === index) last.end += 1;
            else runs.push({ start: index, end: index + 1 });
        }
        const read = await Promise.all(runs.map(({ start, end }) =>
            node.contents.slice(start * PIECE, Math.min(end * PIECE, node.size)).arrayBuffer()));
        runs.forEach(({ start, end }, i) => {
            const bytes = new Uint8Array(read[i]);
            for (let index = start; index < end; index += 1) {
                keep(node, index, bytes.subarray((index - start) * PIECE, (index - start + 1) * PIECE));
            }
        });
        return wanted.length;
    };
}

// WORKERFS dates a mounted file by `file.lastModifiedDate`, which Firefox no
// longer has, and a folder's files (`blobs`: a picked folder, an MRXS slide's)
// by nothing at all: both took the time of the mount, a new one at every
// visit. Python takes a file's time for its version (a mask's
// `segmentationSourceKey`, the key of the pyramid the page builds of it), so
// each reload took the mask for a changed one and the page built its pyramid
// again, under a new key, beside the old. A file is dated by its own
// `lastModified` instead, as the desktop reads a file's time. Once per
// WORKERFS (the mounts all share it); a Blob with no time keeps the mount's.
function datedFiles(workerfs) {
    if (workerfs.plexoraDated) return workerfs;
    const createNode = workerfs.createNode;
    workerfs.createNode = function (parent, name, mode, dev, contents, mtime) {
        const own = contents && typeof contents.lastModified === "number" ? new Date(contents.lastModified) : mtime;
        return createNode.call(this, parent, name, mode, dev, contents, own);
    };
    workerfs.plexoraDated = true;
    return workerfs;
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
//: Jobs the page stopped by message (`cancelJob`: a tab worker, which shares
//: no memory with its page), read at Python's next yield.
const cancelledJobs = new Set();
function cancelled(jobId) {
    if (cancelledJobs.has(jobId)) return true;
    if (!cancels) return false;
    // The page writes the ids and leaves the rest of the buffer zero: the
    // last id ends at the first zero, not at the buffer's end.
    const bytes = new Uint8Array(cancels).slice();
    const end = bytes.indexOf(0);
    const text = new TextDecoder().decode(end < 0 ? bytes : bytes.subarray(0, end));
    return text.split("\n").includes(jobId);
}

//: This site's own storage for the projects: OPFS plexora/state where this
//: browser can write there, else a folder kept in IndexedDB
//: (workers/idbFolder.js: WebKit on Windows opens OPFS and fails every
//: write). ["opfs" | "indexeddb", handle]; throws where neither works.
async function browserState() {
    let why = null;
    // Three tries: a fresh Firefox profile once refused the first write.
    for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
            const root = await navigator.storage.getDirectory();
            const plexora = await root.getDirectoryHandle("plexora", { create: true });
            // A write, as the store will make one: opening is not enough.
            const probe = await plexora.getFileHandle(".write-check", { create: true });
            const writable = await probe.createWritable();
            await writable.write(new Uint8Array([1]));
            await writable.close();
            await plexora.removeEntry(".write-check").catch(() => {});
            const state = await plexora.getDirectoryHandle("state", { create: true });
            await takeBack(state);
            return ["opfs", state];
        } catch (error) {
            why = error;
            await new Promise((resolve) => setTimeout(resolve, 250));
        }
    }
    try {
        const folder = await idbFolder("plexora-state");
        self.postMessage({ log: `store: this site's files cannot be written here (${why?.name || ""} ${why?.message || why}); `
                                + "projects are kept in IndexedDB" });
        return ["indexeddb", folder];
    } catch (error) {
        throw new Error(`${why?.message || why}; ${error?.message || error}`);
    }
}

//: Projects an earlier visit had to keep in IndexedDB, now that OPFS can be
//: written: copied into `state` where it has no file of that name (its
//: registry merged, entries already there kept), then the database dropped.
async function takeBack(state) {
    if (typeof indexedDB === "undefined" || typeof indexedDB.databases !== "function") return;
    const names = (await indexedDB.databases().catch(() => [])).map((db) => db.name);
    if (!names.includes("plexora-state")) return;
    const held = await idbFolder("plexora-state");
    const copy = async (from, to) => {
        for await (const [name, handle] of from.entries()) {
            if (handle.kind === "directory") {
                await copy(handle, await to.getDirectoryHandle(name, { create: true }));
                continue;
            }
            const there = await to.getFileHandle(name).catch(() => null);
            let body = await handle.getFile();
            if (there && name === "config.json" && to === state) {
                const ours = JSON.parse(await (await there.getFile()).text() || "{}");
                body = JSON.stringify({ ...JSON.parse(await body.text() || "{}"), ...ours }, null, 4);
            } else if (there) {
                continue;
            }
            const writable = await (await to.getFileHandle(name, { create: true })).createWritable();
            await writable.write(body);
            await writable.close();
        }
    };
    await copy(held, state);
    held.close();
    await new Promise((resolve) => {
        const request = indexedDB.deleteDatabase("plexora-state");
        request.onsuccess = request.onerror = request.onblocked = () => resolve();
    });
    self.postMessage({ log: "store: projects kept in IndexedDB on an earlier visit are back with the others" });
}

async function mountState(folder) {
    // The project store: a folder the user granted (services/browserStore.js),
    // or this site's own storage, read into /data where the app keeps its data
    // root, and kept in step with it (workers/storeSync.js) -- never mirrored:
    // the desktop app may write the same folder.
    store = createStore(py.FS, "/data", (line) => self.postMessage({ log: line }));
    try {
        let handle = folder;
        let kind = "folder";
        if (!handle) [kind, handle] = await browserState();
        // Plexora's folder on the computer is the desktop app's too: what is
        // only one machine's, or too big to hold, stays there (storeSync.js).
        await store.open(handle, { shared: kind === "folder" });
        storeHandle = handle;
        lastPull = performance.now();
        return kind;
    } catch (error) {
        store = null;
        py.FS.mkdirTree("/data");
        return `memory (${error?.message || error})`;
    }
}

// One write back at a time, and at most one more queued behind it, a moment
// after it was asked for: a burst of requests is one more write-back, never
// several at once. Asked after every request and every background step. A
// request that may have written (the bridge's `wrote`: anything but a read)
// is written back SYNC_DELAY_MS after it; a read only later, after
// READ_SYNC_DELAY_MS -- a GET can write too (a project's first load makes its
// database), but most write nothing, and each write-back walks all of /data.
// An earlier write-back asked for replaces a later one.
const SYNC_DELAY_MS = 250;
const READ_SYNC_DELAY_MS = 2000;
let syncing = Promise.resolve();
let syncTimer = null;
let syncAt = 0;
function scheduleSync(delay = SYNC_DELAY_MS) {
    scheduleKeep();
    if (!store) return;
    const at = performance.now() + delay;
    if (syncTimer !== null) {
        if (at >= syncAt) return;
        clearTimeout(syncTimer);
    }
    syncAt = at;
    syncTimer = setTimeout(() => {
        syncTimer = null;
        if (syncPending) return;
        syncPending = true;
        syncing = syncing.then(async () => {
            syncPending = false;
            try { await store.push(); } catch (error) { self.postMessage({ log: `store: ${error}` }); }
            await pushLibraries();
        });
    }, delay);
}

// The chunk cache's index (plexora/browser/cachefiles.py) written to the
// site's storage once Python has been quiet for a moment after a request or
// a step: the snapshot a reload starts from. Only when it changed.
const KEEP_MS = 2000;
let keepTimer = null;
function scheduleKeep() {
    if (!self.plexoraCacheFiles || keepTimer) return;
    keepTimer = setTimeout(() => {
        keepTimer = null;
        if (!py || busy || stepping || queue.length) {
            scheduleKeep();
            return;
        }
        let module = null;
        try {
            module = py.pyimport("plexora.browser.cachefiles");
            module.keep(true);
        } catch (error) {
            self.postMessage({ log: `cache: ${error?.message || error}` });
        } finally {
            module?.destroy();
        }
    }, KEEP_MS);
}


// -- the folders added beside the store (Settings › Data) -----------------------
//
// The workspaces the user added beside the one their projects are kept in
// (services/browserStore.js keeps their handles in its IndexedDB record,
// `locations`): each one's projects/ read into /library/<id> by a store of
// its own -- the same kind as /data's, shared with the desktop app, written
// back and read in with it -- and named to Python (`boot.set_libraries`),
// whose /projects then lists their projects where they are. Read again at
// boot, when the store moves (`useStore`) and when the page says the list
// changed (`libraries`). The record is read here rather than sent, so a
// reload, or a store moved by the onboarding, finds the same list. A folder
// this browser does not let the page open today is left out, not forgotten:
// the page's Reconnect asks for it, then says so.
const LIBRARY_ROOT = "/library";
//: id -> {store, handle (its projects/), path, label}.
const libraries = new Map();
let libraryMounts = 0;
//: The folder /data is read from (`mountState`, `useStore`): one added
//: beside it too is not mounted twice.
let storeHandle = null;

//: The record's `locations` ([{id, handle, label}]), or none.
function keptFolders() {
    return new Promise((resolve) => {
        if (typeof indexedDB === "undefined") { resolve([]); return; }
        let request;
        try {
            request = indexedDB.open("plexora-store", 1);
        } catch (error) {
            resolve([]);
            return;
        }
        // Made as browserStore.js makes it, should this be the first to ask.
        request.onupgradeneeded = () => request.result.createObjectStore("store");
        request.onerror = () => resolve([]);
        request.onblocked = () => resolve([]);
        request.onsuccess = () => {
            const db = request.result;
            const done = (value) => { db.close(); resolve(value); };
            try {
                const get = db.transaction("store", "readonly").objectStore("store").get("data");
                get.onsuccess = () => {
                    const kept = get.result && Array.isArray(get.result.locations) ? get.result.locations : [];
                    // The id names a folder in Python's files: nothing else.
                    done(kept.filter((loc) => loc && loc.handle && typeof loc.handle.getDirectoryHandle === "function"
                                              && /^[A-Za-z0-9_-]{1,64}$/.test(String(loc.id || ""))));
                };
                get.onerror = () => done([]);
            } catch (error) {
                done([]);
            }
        };
    });
}

async function sameFolder(a, b) {
    try {
        return Boolean(a && b && typeof a.isSameEntry === "function" && await a.isSameEntry(b));
    } catch (error) {
        return false;
    }
}

//: A library's files gone from memory. Links are unlinked, never followed.
function removeTree(path) {
    let names;
    try {
        names = py.FS.readdir(path);
    } catch (error) {
        return;
    }
    for (const name of names) {
        if (name === "." || name === "..") continue;
        const child = `${path}/${name}`;
        try {
            if (py.FS.isDir(py.FS.lstat(child).mode)) {
                removeTree(child);
                py.FS.rmdir(child);
            } else {
                py.FS.unlink(child);
            }
        } catch (error) {
            // Open in Python: left, and gone with the worker.
        }
    }
}

async function pushLibraries() {
    for (const [id, lib] of [...libraries]) {
        try { await lib.store.push(); } catch (error) { self.postMessage({ log: `library ${id}: ${error}` }); }
    }
}

//: Whether any library read anything in or dropped anything.
async function pullLibraries() {
    let changed = false;
    for (const [id, lib] of [...libraries]) {
        try {
            const done = await lib.store.pull();
            changed = changed || Boolean(done && (done.read || done.dropped));
        } catch (error) {
            self.postMessage({ log: `library ${id}: ${error}` });
        }
    }
    return changed;
}

//: The record's folders mounted, those it no longer has (or this browser no
//: longer opens) let go, and Python told which are there.
async function mountLibraries() {
    if (!py) return [];
    const wanted = [];
    for (const loc of await keptFolders()) {
        const id = String(loc.id);
        const label = String(loc.label || loc.handle.name || id);
        let projects = null;
        let error = null;
        try {
            // Refused without the browser's permission this visit.
            projects = await loc.handle.getDirectoryHandle("projects", { create: true });
        } catch (failure) {
            error = failure && (failure.name === "NotAllowedError" || failure.name === "SecurityError")
                ? "permission" : String(failure?.message || failure);
        }
        if (projects && await sameFolder(projects, storeHandle)) {
            projects = null;
            error = "default";
        }
        wanted.push({ id, label, projects, error });
    }
    // Let go: out of the map first, so no write-back or read-in is started
    // on it again -- a store whose memory is gone would delete the folder's
    // files on its next write-back -- then written back once, then cleared.
    await syncing;
    for (const [id, lib] of [...libraries]) {
        const next = wanted.find((want) => want.id === id && want.projects);
        if (next && await sameFolder(next.projects, lib.handle)) continue;
        libraries.delete(id);
        try { await lib.store.push(); } catch (error) { self.postMessage({ log: `library ${id}: ${error}` }); }
        removeTree(lib.path);
        try { py.FS.rmdir(lib.path); } catch (error) { /* still holds what Python has open */ }
    }
    for (const want of wanted) {
        if (!want.projects || libraries.has(want.id)) continue;
        const path = `${LIBRARY_ROOT}/${want.id}`;
        libraryMounts += 1;
        const opened = createStore(py.FS, path, (line) => self.postMessage({ log: `library ${want.id}: ${line}` }),
                                   { heldRoot: `/held/library-${libraryMounts}` });
        try {
            await opened.open(want.projects, { shared: true });
            libraries.set(want.id, { store: opened, handle: want.projects, path, label: want.label });
        } catch (failure) {
            want.error = String(failure?.message || failure);
            removeTree(path);
        }
    }
    let module = null;
    let named = null;
    try {
        module = py.pyimport("plexora.browser.boot");
        named = py.toPy([...libraries.values()].map((lib) => lib.path));
        module.set_libraries(named)?.destroy?.();
    } catch (failure) {
        self.postMessage({ log: `libraries: ${failure?.message || failure}` });
    } finally {
        named?.destroy?.();
        module?.destroy?.();
    }
    return wanted.map((want) => {
        const lib = libraries.get(want.id);
        return { id: want.id, label: want.label, path: lib ? lib.path : null,
                 files: lib ? lib.store.files() : 0, error: lib ? null : want.error };
    });
}


// Pyodide, the base stage's packages and Plexora's wheels, loaded and
// unpacked: everything of the boot that needs nothing from the page but the
// bundle's address. Started by `warm` as soon as the page has the address, so
// the downloads run while the page still sets itself up (services/shell.js),
// and awaited by `boot`. Nothing is imported: Plexora builds its app as its
// package is imported, and the environment it reads is boot's. The instance
// stays out of `py` until boot takes it, so no step runs before then.
let warming = null;
//: `blockReads`' read ahead, once Pyodide is loaded.
let readAhead = null;

function warm(args) {
    if (warming) return warming;
    base = args.base.endsWith("/") ? args.base : `${args.base}/`;
    warming = (async () => {
        const phases = { start: at() };
        stagesInfo = await (await fetch(`${base}stages.json`)).json();
        const wheels = Promise.all(stagesInfo.base.wheels.map(fetchWheel));
        const { loadPyodide } = await import(`${base}pyodide/pyodide.mjs`);
        const pyodide = await loadPyodide({ indexURL: `${base}pyodide/` });
        // The error that ends Pyodide, as it happens: every later call only
        // says "already fatally failed", which names no cause.
        try {
            pyodide._api.on_fatal = (error) => fatal(error, pyodide._module);
        } catch (error) { /* told by the next call instead */ }
        readAhead = blockReads(pyodide.FS.filesystems.WORKERFS, pyodide._module);
        phases.core = at(); mark("core");
        await pyodide.loadPackage(stagesInfo.base.pyodide, { messageCallback: () => {} });
        phases.packages = at(); mark("packages");
        const site = pyodide.runPython("import site; site.getsitepackages()[0]");
        for (const buffer of await wheels) pyodide.unpackArchive(buffer, "wheel", { extractDir: site });
        phases.wheels = at(); mark("wheels");
        return { pyodide, phases };
    })();
    // A failure is boot's to report, with boot's answer.
    warming.catch(() => null);
    return warming;
}

//: The first boot's answer, and the boot itself: a page of this tab that
//: connects again (a reload) is given the Python the last one left.
let booted = null;
let booting = null;

async function boot(args) {
    if (!booting) {
        booting = firstBoot(args).then((answer) => {
            booted = answer;
            return answer;
        });
        return booting;
    }
    const answer = await booting;
    if (args.debug) debug = true;
    if (fatalTold) {
        // Python failed for good: the tab needs a new one, which the page
        // starts (services/pythonClient.js `restart`) once this one is gone.
        setTimeout(() => self.close(), 1000);
        return { ...answer, dead: true };
    }
    return { ...answer, reattached: true, page: pageNumber };
}

async function firstBoot(args) {
    origin = args.origin;
    siteBase = String(args.siteBase || "");
    if (args.cancels) cancels = args.cancels;
    debug = Boolean(args.debug);
    inFlight = args.inFlight || null;
    // A warm worker was given the same address; one booted cold warms now.
    const warmed = await warm(args);
    py = warmed.pyodide;
    const phases = { ...warmed.phases, boot: at() };
    // A scratch boot (the self-check page, services/selfcheck.js) keeps its
    // data in memory and mounts none of the site's copies: the projects the
    // app's tab keeps are never opened by a second Python.
    if (args.scratch) py.FS.mkdirTree("/data");
    const store = args.scratch ? "memory (scratch)" : await mountState(args.store || null);
    py.FS.mkdirTree("/bioc");
    py.FS.mkdirTree("/picked");
    py.FS.mkdirTree("/derived");
    const derived = args.scratch ? 0 : await mountDerived();
    // The chunk cache's files (plexora/browser/cachefiles.py), opened now:
    // Python cannot wait for them later in every browser.
    const cache = args.scratch ? { error: "a scratch boot" } : await openCacheFiles();
    self.plexoraCacheFiles = cache.files || null;
    self.plexoraCacheQuota = cache.quota || 0;
    self.plexoraCacheWhy = cache.error || "";
    // Without JSPI Python cannot wait on a promise: every stage is loaded
    // now (none can arrive mid-import), and the page's answers come through
    // shared memory (`syncAsk`).
    const loaded = ["base"];
    if (!jspi) {
        for (const name of Object.keys(stagesInfo)) {
            if (name === "base") continue;
            try {
                await loadStage(name, "boot (no JSPI)");
                loaded.push(name);
            } catch (error) {
                self.postMessage({ log: `stage ${name}: ${error?.message || error}` });
            }
        }
        // The JPEG 2000 decoder too: Python cannot wait for it later.
        await j2kReady().catch((error) => self.postMessage({ log: `JPEG 2000: ${error?.message || error}` }));
        phases.stages = at(); mark("stages");
        openSyncChannel();
    }
    self.plexoraPost = (text) => self.postMessage({ event: JSON.parse(text) });
    self.plexoraCancelled = cancelled;
    self.plexoraLoadStage = loadStage;
    self.plexoraAsk = ask;
    self.plexoraJ2KReady = j2kReady;
    self.plexoraJ2K = j2kDecode;
    self.plexoraJXR = jpegxrDecode;
    self.plexoraAskSync = syncAsk;
    self.plexoraSourceWrite = sourceWrite;
    self.plexoraSleep = sleep;
    self.plexoraJspi = jspi;
    self.plexoraStagesLoaded = loaded;
    self.plexoraWaiting = () => queue.length > 0 || (inFlight !== null && Atomics.load(inFlight, 0) > 0);
    // A picked file's pieces read at once (plexora/browser/tiff_ahead.py): a
    // promise of how many were read, or -1 for a path that is not a picked
    // file -- and while a job waits with requests let in (`idling`), when a
    // request could start in the read's wait, inside the one reading.
    self.plexoraReadAhead = (path, indexes) => {
        if (!readAhead || idling > 0) return -1;
        let node = null;
        try {
            node = py.FS.lookupPath(String(path), { follow: true }).node;
        } catch (error) {
            return -1;
        }
        return node && node.contents instanceof Blob ? readAhead(node, indexes) : -1;
    };
    self.plexoraReadPiece = PIECE;
    py.globals.set("ORIGIN", origin);
    py.globals.set("SITE_BASE", siteBase);
    py.globals.set("TELEMETRY", JSON.stringify(args.telemetry || null));
    const info = JSON.parse(await py.runPythonAsync(`
import json
import js
from pyodide.ffi import run_sync
from plexora.browser import boot, stages

JSPI = bool(js.plexoraJspi)

def _post(message):
    js.plexoraPost(json.dumps(message))

def _held():
    # Inside a C library's call back into Python (h5py reading a remote .ims:
    # plexora/server/utils/no_suspend.py), where suspending ends Python.
    from plexora.server.utils import no_suspend
    return no_suspend.active()

def _idle(seconds):
    import asyncio
    if not JSPI or _held():
        # Nothing can be served meanwhile: the time passes, blocked.
        if seconds > 0:
            js.self.plexoraSleep(seconds)
        return
    js.self.plexoraIdle(1)
    try:
        # No time (a long task's checkpoint): yield to whatever is queued --
        # the messages carrying requests -- rather than to a timer.
        run_sync(js.self.plexoraYield() if seconds <= 0 else asyncio.sleep(seconds))
    finally:
        js.self.plexoraIdle(-1)

def _wait_on(promise):
    # The network (plexora/browser/fetch.py). In background work (a chat
    # turn, a job) requests are answered while Python waits for the answer,
    # as they are while a job sleeps. A request's own call (the launcher's
    # balance) just waits: requests run inside a request's wait were seen to
    # wreck the worker (a polars read nested in /ai/v1/balance's wait never
    # let the balance resume, and the page crashed).
    if not js.self.plexoraInStep():
        return run_sync(promise)
    js.self.plexoraIdle(1)
    try:
        return run_sync(promise)
    finally:
        js.self.plexoraIdle(-1)

def _absent(value):
    from pyodide.ffi import jsnull
    return value is None or value is jsnull

def _ask(kind, args):
    # A question only the page can answer; None when it cannot. numpy arrays
    # at the top level of args cross as {dtype, shape, data}, their bytes in a
    # typed array (magic select's image and embedding: plexora/browser/sam_page.py).
    import numpy as np
    from pyodide.ffi import to_js
    arrays = {key: value for key, value in args.items() if isinstance(value, np.ndarray)}
    message = js.JSON.parse(json.dumps({key: value for key, value in args.items() if key not in arrays}))
    for key, value in arrays.items():
        value = np.ascontiguousarray(value)
        entry = js.JSON.parse(json.dumps({"dtype": str(value.dtype), "shape": list(value.shape)}))
        entry.data = to_js(memoryview(value).cast("B"))
        setattr(message, key, entry)
    value = run_sync(js.plexoraAsk(kind, message)) if JSPI and not _held() else js.plexoraAskSync(kind, message)
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
    if JSPI:  # without it, loaded at boot with the stages
        run_sync(js.self.plexoraJ2KReady())
    found = js.self.plexoraJ2K(to_js(memoryview(bytes(data))))
    bits, signed, components = int(found.bits), bool(found.signed), int(found.components)
    dtype = np.dtype(("i" if signed else "u") + ("1" if bits <= 8 else "2"))
    pixels = np.frombuffer(found.data.to_py(), dtype=dtype)
    shape = (int(found.height), int(found.width)) + ((components,) if components > 1 else ())
    return pixels.reshape(shape)

def _jxr(data):
    # JPEG XR for czifile and tifffile (imagecodecs_shim's set_jpegxr): jxrlib
    # in WebAssembly in this worker (workers/jpegxr.js), fetched and compiled
    # synchronously the first time a tile needs it, with or without JSPI.
    from pyodide.ffi import to_js
    found = js.self.plexoraJXR(to_js(memoryview(bytes(data))))
    return {"width": int(found.width), "height": int(found.height), "channels": int(found.channels),
            "colorformat": str(found.colorFormat), "bitdepth": str(found.bitDepth),
            "bitsperpixel": int(found.bitsPerPixel), "alpha": bool(found.alpha),
            "bgr": bool(found.bgr), "data": found.data.to_py()}

from plexora.browser import imagecodecs_shim as _codecs
_codecs.set_jpeg2k(_j2k)
_codecs.set_jpegxr(_jxr)

def _load_stage(name):
    if not JSPI:
        raise ImportError(f"the {name} stage could not be loaded in this browser")
    if _held():
        raise ImportError(f"the {name} stage cannot be loaded inside a library's read")
    run_sync(js.plexoraLoadStage(name, _finder.importers.get(name, "?")))

def _read_ahead(path, pieces):
    # A picked TIFF's pieces, read at once (plexora/browser/tiff_ahead.py):
    # only where Python can wait on the reads; elsewhere tifffile reads them
    # one after another, as before.
    from pyodide.ffi import can_run_sync, to_js
    if not JSPI or not can_run_sync() or _held():
        return -1
    answer = js.self.plexoraReadAhead(path, to_js(list(pieces)))
    return int(answer if isinstance(answer, (int, float)) else run_sync(answer))

_finder = stages.install(_load_stage, loaded=tuple(js.plexoraStagesLoaded.to_py()))
json.dumps(boot.start(ORIGIN, base_url=SITE_BASE, post=_post, cancelled=lambda job: bool(js.plexoraCancelled(job)),
                      idle=_idle, waiting=lambda: bool(js.plexoraWaiting()), columns=_columns,
                      ask=_ask, wait_on=_wait_on, jspi=JSPI, sleep=lambda seconds: js.self.plexoraSleep(seconds),
                      deliver=lambda source, staged: bool(js.self.plexoraSourceWrite(source, staged)),
                      read_ahead=_read_ahead, read_piece=int(js.self.plexoraReadPiece),
                      telemetry=json.loads(TELEMETRY)))
`));
    phases.ready = at(); mark("ready");
    // The folders added beside the store, once Python can be told of them
    // (boot.start sets the environment set_libraries changes).
    const libraryList = args.scratch ? [] : await mountLibraries();
    // The quiet background work waits for counts from here: the page sends
    // what it held for Python a moment after this (a reloaded project's
    // first screen), and a stage preload started in that moment held all of
    // it (/config waited 0.8 s behind the scipy stage).
    lastAnswer = performance.now();
    if (args.telemetry && args.telemetry.endpoint) info.telemetry = await startTelemetry(args.telemetry);
    return { phases, info, store, derived, libraries: libraryList, stages: Object.fromEntries(
        Object.entries(stagesInfo).map(([name, stage]) => [name, stage.bytes])),
             transport: jspi ? "jspi" : syncChannel ? "shared memory" : "none", loaded };
}

// -- Python waiting on the page without JSPI -----------------------------------
//
// Firefox and Safari releases before JavaScript Promise Integration cannot
// suspend Python on a promise. There the worker asks the page and blocks in
// Atomics.wait on memory it shares with the page (the site is cross-origin
// isolated), and the page writes the answer there (services/pythonClient.js
// `answerSync`), in chunks of the shared buffer's size: header (JSON, its
// arrays and files by reference) then the arrays' bytes. A File the answer
// names (a pyramid the page built) comes as a blob: URL the worker reads with
// a synchronous request. Nothing else can run in the worker meanwhile -- the
// page's answer never needs it.

//: Shared with the page: [state, bytes in this chunk, bytes in all], and the chunk.
let syncChannel = null;
//: Atomics.wait's cell for a blocking sleep.
let sleepCell = null;
const SYNC_BYTES = 8 << 20;
const S = { IDLE: 0, WAITING: 1, CHUNK: 2, LAST: 3, FAILED: 4 };

function openSyncChannel() {
    if (typeof SharedArrayBuffer !== "function" || !self.crossOriginIsolated) return;
    syncChannel = { ctl: new Int32Array(new SharedArrayBuffer(16)),
                    data: new Uint8Array(new SharedArrayBuffer(SYNC_BYTES)) };
    sleepCell = new Int32Array(new SharedArrayBuffer(4));
    self.postMessage({ syncChannel });
}

function sleep(seconds) {
    if (sleepCell) Atomics.wait(sleepCell, 0, 0, Math.max(0, seconds * 1000));
}

//: The page's answer to `kind`, waited for here; null when it has none (or
//: there is no shared memory: Python then does the work itself).
function syncAsk(kind, args) {
    if (!syncChannel) return null;
    const { ctl, data } = syncChannel;
    const id = nextAsk++;
    Atomics.store(ctl, 0, S.WAITING);
    self.postMessage({ event: { kind: "ask", ask: kind, id, args, sync: true } });
    let all = null;
    let at = 0;
    for (;;) {
        Atomics.wait(ctl, 0, S.WAITING);
        const state = Atomics.load(ctl, 0);
        const n = Atomics.load(ctl, 1);
        if (state === S.FAILED) {
            const why = new TextDecoder().decode(data.slice(0, n));
            Atomics.store(ctl, 0, S.IDLE);
            self.postMessage({ log: `the page could not answer: ${why}` });
            return null;
        }
        if (!all) all = new Uint8Array(Atomics.load(ctl, 2));
        all.set(data.subarray(0, n), at);
        at += n;
        if (state === S.LAST) {
            Atomics.store(ctl, 0, S.IDLE);
            Atomics.notify(ctl, 0);
            break;
        }
        Atomics.store(ctl, 0, S.WAITING);
        Atomics.notify(ctl, 0);
    }
    const value = decodeAnswer(all);
    if (value && value.mount) {
        try { mount(value.mount); } catch (error) { self.postMessage({ log: `mount: ${error}` }); }
        delete value.mount;
    }
    return value;
}

//: services/pythonClient.js `encodeAnswer`, undone: arrays become views of
//: the bytes, files are read through their blob: URLs.
function decodeAnswer(bytes) {
    const length = new DataView(bytes.buffer).getUint32(0, true);
    const head = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length)));
    const start = (4 + length + 7) & ~7;
    const revive = (value) => {
        if (Array.isArray(value)) return value.map(revive);
        if (!value || typeof value !== "object") return value;
        if ("\u0000bin" in value) {
            const { type, offset, length: count } = head.bins[value["\u0000bin"]];
            return new self[type](bytes.buffer, start + offset, count);
        }
        if ("\u0000blob" in value) {
            const request = new XMLHttpRequest();
            request.open("GET", value["\u0000blob"], false);
            request.responseType = "blob";
            request.send();
            return new File([request.response], value.name || "file");
        }
        return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, revive(item)]));
    };
    return revive(head.value);
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
    // An array's bytes (`{dtype, shape, data}`, made by `_ask`) are a copy
    // Python no longer needs: moved to the page, not copied again -- never a
    // view of Python's own memory, which moving would take from it.
    const heap = py && py._module && py._module.HEAP8 ? py._module.HEAP8.buffer : null;
    const transfer = [];
    for (const value of Object.values(args || {})) {
        const data = value && value.data;
        if (ArrayBuffer.isView(data) && data.buffer instanceof ArrayBuffer && data.buffer !== heap
            && !transfer.includes(data.buffer)) transfer.push(data.buffer);
    }
    return new Promise((resolve) => {
        const id = nextAsk++;
        asks.set(id, resolve);
        self.postMessage({ event: { kind: "ask", ask: kind, id, args } }, transfer);
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
    // `retryable`: this worker puts a request a route asks to be asked again
    // later back in its queue (`drain`), so the route may say so (bridge.py).
    const message = py.toPy({ ...args, retryable: true, body: args.body ? new Uint8Array(args.body) : null });
    const answer = await call(handle, app, message, origin);
    const retry = answer.get("retry");
    if (retry !== undefined && retry !== null) {
        // The route asked to be asked again later (a channel the page is
        // still scanning, server/providers/browser.py `WindowPending`): `drain`
        // puts it back in the queue; nothing to answer or write back.
        answer.destroy(); message.destroy(); handle.destroy(); app.destroy();
        return { retry: Math.max(20, Number(retry) || 200), pyMs: Math.round(performance.now() - started) };
    }
    const result = {
        status: answer.get("status"),
        headers: answer.get("headers").toJs(),
        body: toBytes(answer.get("body")),
        stream: answer.get("stream") ?? null,
        pyMs: Math.round(performance.now() - started),
    };
    scheduleSync(answer.get("wrote") === false ? READ_SYNC_DELAY_MS : SYNC_DELAY_MS);
    // Settings > Usage data (the mode, the notice, a new install id): kept now.
    if (telemetry && args.method === "POST" && /\/telemetry\/(mode|notice_seen|reset|send)$/.test(args.path || "")) {
        telemetry.kept.save().catch(() => null);
    }
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

//: Files read on demand under `dir`. A folder already mounted with the same
//: files is left as it is (a copy the page built is mounted at boot and may
//: be announced again); one mounted with other files -- a copy Python refused,
//: rebuilt by the page under the same key -- is mounted again with the new
//: ones, or Python would go on reading the refused file.
function mount({ dir, files }) {
    const mounted = py.FS.analyzePath(dir).exists && py.FS.isMountpoint(py.FS.lookupPath(dir).node);
    if (mounted && sameFiles(dir, files)) return files.map((file) => `${dir}/${file.name}`);
    if (mounted) py.FS.unmount(dir);
    py.FS.mkdirTree(dir);
    py.FS.mount(datedFiles(py.FS.filesystems.WORKERFS), { files }, dir);
    return files.map((file) => `${dir}/${file.name}`);
}

//: Whether `dir` holds `files` already: each by name, with the same size and
//: time (the same File object when it is the same announcement).
function sameFiles(dir, files) {
    for (const file of files) {
        let held = null;
        try {
            held = py.FS.lookupPath(`${dir}/${file.name}`).node.contents;
        } catch (error) {
            return false;
        }
        if (held === file) continue;
        if (!held || held.size !== file.size || held.lastModified !== file.lastModified) return false;
    }
    return true;
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
    py.FS.mount(datedFiles(py.FS.filesystems.WORKERFS), { blobs }, dir);
    return blobs.length;
}

// -- saving into a picked file (plexora/server/utils/source_writes.py) ---------
//
// Python wrote a picked file as a staged copy in its memory. The copy becomes
// the file Python reads at that path from now on -- the pick's mount made again
// with it in the original's place, as the desktop reads the file it wrote --
// and goes to the page, which saves it over the original through the
// browser's handle or offers it as a download (services/sourceWrites.js).
// Python removes the staged copy once this returns.
function sourceWrite(source, staged) {
    const bytes = py.FS.readFile(staged);
    const file = new File([bytes], source.split("/").pop(), { lastModified: Date.now() });
    swapPicked(source, file);
    self.postMessage({ event: { kind: "sourceWrite", source, file } });
    return true;
}

//: The pick mounted at /picked/<id> made again with `file` at `source`.
function swapPicked(source, file) {
    const parts = source.split("/");
    const dir = parts.slice(0, 3).join("/");
    const inside = parts.slice(3).join("/");
    const node = py.FS.lookupPath(dir).node;
    if (!node.mount || node.mount.mountpoint !== dir) return;
    const opts = node.mount.opts || {};
    const next = opts.blobs
        ? { blobs: opts.blobs.map((blob) => (blob.name === inside ? { name: blob.name, data: file } : blob)) }
        : { files: (opts.files || []).map((other) => (other.name === inside ? file : other)) };
    py.FS.unmount(dir);
    py.FS.mount(datedFiles(py.FS.filesystems.WORKERFS), next, dir);
}

// -- telemetry (plexora/browser/telemetry.py) -----------------------------------
//
// The desktop's anonymous usage counts, sent from here by the desktop's code:
// its files kept in IndexedDB (workers/telemetryStore.js), its turn taken a
// minute after the start, then every few minutes and when the tab is hidden
// (the `telemetry` op), queued behind the page's requests like any message.
const TELEMETRY_FIRST_MS = 60 * 1000;
const TELEMETRY_EVERY_MS = 5 * 60 * 1000;
let telemetry = null;

async function startTelemetry(options) {
    const kept = createTelemetryStore({ FS: py.FS, kv: indexedKv() });
    try {
        await kept.restore();
    } catch (error) {
        self.postMessage({ log: `telemetry: kept in memory only (${error?.message || error})` });
    }
    const module = py.pyimport("plexora.browser.telemetry");
    let answer;
    try {
        answer = JSON.parse(await call(module.start_json, JSON.stringify({
            home: "/telemetry", keep_queue: kept.kept, context: options.context || {} })));
    } finally {
        module.destroy();
    }
    telemetry = { kept, answer };
    await kept.save().catch(() => null);
    const tick = () => { queue.push({ op: "telemetry", args: { hidden: false }, arrived: performance.now() }); drain(); };
    setTimeout(() => { tick(); setInterval(tick, TELEMETRY_EVERY_MS); }, TELEMETRY_FIRST_MS);
    return answer;
}

async function telemetryTurn({ hidden = false } = {}) {
    if (!telemetry) return null;
    const module = py.pyimport("plexora.browser.telemetry");
    let answer;
    try {
        answer = JSON.parse(await call(module.tick_json, Boolean(hidden)));
    } finally {
        module.destroy();
    }
    answer.saved = await telemetry.kept.save().catch(() => []);
    return answer;
}

// A project's `.plexora` file on the website (services/bundleWeb.js): the
// bytes move in the page's own worker (workers/bundleWorker.js), which reads
// and writes the stores' folders itself, never through Python's heap. These
// three are what it needs from here: where each store's folder is, files it
// wrote there made known (storeSync `adopt`), and the small files an export
// makes in Python's own files (its database snapshots and entries).
const BUNDLE_OPS = {
    //: {"/data": {kind, handle?, opfs?}, "/library/<id>": ...}: a granted
    //: folder's handle, or this site's storage by its path (the worker opens
    //: it itself); `kind` alone where only Python reads the files (memory,
    //: IndexedDB).
    bundleStores: async () => {
        const describe = async (handle) => {
            if (!handle || typeof handle.getDirectoryHandle !== "function" || typeof handle.isSameEntry !== "function") {
                return { kind: handle ? "indexeddb" : "memory" };
            }
            try {
                const opfs = await (await navigator.storage.getDirectory()).resolve(handle);
                if (Array.isArray(opfs)) return { kind: "opfs", opfs };
            } catch (error) {
                // Not this site's storage.
            }
            return { kind: "folder", handle };
        };
        const out = { "/data": store ? await describe(storeHandle) : { kind: "memory" } };
        for (const lib of libraries.values()) out[lib.path] = await describe(lib.handle);
        return out;
    },
    //: Files the bundle worker wrote into a store, by their paths in
    //: Python's files: {placed, held, linked, missing} per store.
    adopt: async ({ paths }) => {
        await syncing;
        const roots = [["/data", store], ...[...libraries.values()].map((lib) => [lib.path, lib.store])];
        const out = {};
        for (const [root, owner] of roots) {
            const mine = (paths || []).filter((path) => String(path).startsWith(`${root}/`))
                .map((path) => String(path).slice(root.length + 1));
            if (owner && mine.length) out[root] = await owner.adopt(mine);
        }
        return out;
    },
    //: A file of Python's, its bytes (transferred, never copied twice).
    readFile: ({ path }) => ({ body: py.FS.readFile(String(path)) }),
    //: Where a picked file's directories are, as the page found them
    //: (services/firstView.js, which read them for the first view): their
    //: pieces read at once, side by side, so Python's open of the file finds
    //: every directory held instead of walking them one read after another.
    prefetch: async ({ path, offsets }) => {
        if (!readAhead || !py) return { read: 0 };
        let node = null;
        try {
            node = py.FS.lookupPath(String(path), { follow: true }).node;
        } catch (error) {
            return { read: 0 };
        }
        if (!node || !(node.contents instanceof Blob)) return { read: 0 };
        const indexes = Array.from(offsets || [], (at) => Math.floor(Number(at) / PIECE))
            .filter((index) => Number.isFinite(index));
        return { read: await readAhead(node, indexes) };
    },
};

//: What the worker still answers once Python has failed for good: boot
//: (which tells a reattaching page so), and what reads no Python. Every
//: other op would only ask a dead Pyodide.
const AFTER_FATAL = new Set(["boot", "heap", "trace", "state"]);

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
    telemetry: telemetryTurn,
    //: The project store written back now (before the page copies it).
    flush: async () => {
        await syncing;
        if (store) await store.push();
        await pushLibraries();
        return true;
    },
    ...BUNDLE_OPS,
    //: Keep the projects in `folder` from now on (services/browserStore.js
    //: has copied them there): the store written back, /data read from it.
    useStore: async ({ folder }) => {
        await syncing;
        if (store) await store.push();
        else store = createStore(py.FS, "/data", (line) => self.postMessage({ log: line }));
        // An added folder about to become the store has nothing left unwritten.
        await pushLibraries();
        await store.open(folder, { shared: true });
        storeHandle = folder;
        lastPull = performance.now();
        // The old store may be an added folder now, and the new one may have
        // been one (Settings' Make default).
        await mountLibraries();
        return "folder";
    },
    //: The page added or removed a folder, or the browser let it open one
    //: again (Settings › Data): the list read again and mounted.
    libraries: () => mountLibraries(),
    sync: async () => {
        if (!debug) throw new Error("sync is for tests: boot with debug: true");
        const started = performance.now();
        if (store) await store.push();
        return performance.now() - started;
    },
    //: Tests: what another app changed in the store, read in now.
    //: Also the page's, when the tab is shown again: what changed meanwhile.
    pull: async () => {
        if (!store) return null;
        lastPull = performance.now();
        pullEvery = PULL_MS;
        const done = await store.pull();
        await pullLibraries();
        return done;
    },
    trace: () => (debug ? traced.splice(0) : null),
    //: What the worker is doing, for the status chip and diagnostics.
    state: () => ({ busy, idling, retrying: retrying.size, queued: queue.map((m) => m.op === "request" ? m.args.path : m.op),
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
        // What another app (the desktop app sharing the projects folder, or
        // this site in another tab) changed is read in while nothing is
        // asked (`idlePull`) and when the tab is shown again (the page's
        // `pull`), not in front of a request: a pull asks the folder for
        // every one of its files. Only a page that has kept asking without a
        // pause for PULL_STALE_MS pays for one here.
        const now = performance.now();
        if (!busy && store && queue[0].op === "request"
            && now - lastPull > PULL_STALE_MS && now - lastQuiet > PULL_STALE_MS) {
            await pullNow();
            continue;
        }
        const queued = queue.shift();
        const { id, op, args, arrived } = queued;
        if (fatalTold && !AFTER_FATAL.has(op)) {
            // Python cannot answer any more: said now, not by a call into it.
            if (pageNow(queued)) {
                self.postMessage({ id, ok: false, stopped: true,
                                   error: `Python stopped in this tab: ${fatalWith ? fatalWith.cause : "it failed"}` });
            }
            continue;
        }
        const began = performance.now();
        const outer = current;
        current = { op, path: op === "request" ? args.path : null, since: began };
        const nested = busy;
        const outerStep = inStep;
        busy = true;
        inStep = false;
        try {
            const result = await OPS[op](args || {});
            if (op === "request" && result && result.retry) {
                // Back in the queue in `retry` ms, behind whatever arrives
                // meanwhile; a cancel finds it in `retrying`.
                // Each time with its count, so the route stops asking for
                // patience once it has had enough (data_routes `_patient`).
                queued.retries = (queued.retries || 0) + 1;
                queued.args = { ...args, headers: [...(args.headers || []).filter(([name]) => name.toLowerCase() !== "x-plexora-retried"),
                                                   ["X-Plexora-Retried", String(queued.retries)]] };
                retrying.set(id, setTimeout(() => {
                    retrying.delete(id);
                    if (!pageNow(queued)) return;  // its page is gone (a reload)
                    queue.push(queued);
                    drain();
                }, result.retry));
                continue;
            }
            const transfer = result && result.body instanceof Uint8Array ? [result.body.buffer] : [];
            // Not to a page that came after the one that asked: its ids are its own.
            if (pageNow(queued)) self.postMessage({ id, ok: true, result }, transfer);
            if (debug && op === "request") {
                traced.push({ path: args.path, arrived: Math.round(arrived), began: Math.round(began),
                              done: Math.round(performance.now()), nested, idling });
            }
        } catch (error) {
            const message = String(error?.message || error);
            // The error that ended Pyodide (marked `pyodide_fatal_error`), or
            // one of the calls after it, where `on_fatal` was not heard: the
            // page is told once, so it can say so instead of a 500 per request.
            if (error && error.pyodide_fatal_error) fatal(error);
            else if (/fatally failed and can no longer be used/.test(message)) fatal(error);
            if (pageNow(queued)) self.postMessage({ id, ok: false, error: message.slice(-6000),
                                                    stopped: fatalTold });
        } finally {
            busy = nested;
            inStep = outerStep;
            current = outer;
            // It may have queued work: the next step finds out.
            pendingWork = Math.max(pendingWork, 1);
            if (op === "request") lastAnswer = performance.now();
        }
    }
    if (!busy && !queue.length) {
        step();
        idlePull();
    }
}

//: The store's pull, holding the queue while it reads (Python must not see
//: half a folder).
//: Resolves whether anything was read in or dropped.
async function pullNow() {
    lastPull = performance.now();
    busy = true;
    let changed = false;
    try {
        const done = await store.pull();
        changed = Boolean(done && (done.read || done.dropped));
    } catch (error) {
        self.postMessage({ log: `store: ${error}` });
    }
    changed = (await pullLibraries()) || changed;
    busy = false;
    return changed;
}

//: A pull once requests have been quiet for IDLE_PULL_MS, again and again
//: while they stay quiet, each further apart while nothing comes in (PULL_MS,
//: doubled up to PULL_IDLE_MAX_MS): the folder read in during the pauses,
//: where no request waits for it.
const IDLE_PULL_MS = 800;
const PULL_STALE_MS = 10000;
const PULL_IDLE_MAX_MS = 30000;
let idleTimer = null;
let lastQuiet = 0;
let pullEvery = PULL_MS;
function idlePull() {
    if (!store || idleTimer !== null) return;
    idleTimer = setTimeout(async () => {
        idleTimer = null;
        if (busy || stepping || queue.length) return;  // the next drain asks again
        const now = performance.now();
        if (now - lastRequest < IDLE_PULL_MS) {
            idlePull();
            return;
        }
        lastQuiet = now;
        if (now - lastPull >= pullEvery) {
            const changed = await pullNow();
            pullEvery = changed ? PULL_MS : Math.min(pullEvery * 2, PULL_IDLE_MAX_MS);
            if (queue.length) {
                drain();
                return;
            }
        }
        idlePull();
    }, IDLE_PULL_MS);
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
// The quiet is counted from the last request's ANSWER as well as its arrival,
// and before every background task, not only the first (cooperative.py runs
// one a step): a page asks its next question once it has the last answer, so
// counted from arrival alone, a request that had waited out a long task left
// the next task free to start before the page could ask again -- and every
// request of a project opening on a 40-channel slide waited 7-9 s that way.
const QUIET_MS = 150;
const MAX_DEFER_MS = 500;
const BACKGROUND_QUIET_MS = 600;
let lastRequest = 0;
//: When the last request was answered (drain).
let lastAnswer = 0;
let lastStep = 0;
let stepping = false;
//: Whether the innermost Python running is background work (a step), not a
//: request run in its gap: only then may a network wait answer requests.
let inStep = false;
self.plexoraInStep = () => inStep;
//: What the last step left queued: work someone waits on, and background.
let pendingWork = 0;
let pendingBackground = 0;
//: How long the queued work still waits out a delay (`_work.spawn(after_s)`):
//: nothing could run in the last step, and nothing will before then.
let dueInMs = 0;
function step() {
    if (!py || fatalTold || stepping || busy || queue.length) return;
    stepping = true;
    const now = performance.now();
    const onlyBackground = pendingBackground > 0 && pendingWork === 0;
    const heard = Math.max(lastRequest, lastAnswer);
    const wait = Math.max(dueInMs, onlyBackground ? Math.max(0, BACKGROUND_QUIET_MS - (now - heard))
        : now - lastStep >= MAX_DEFER_MS ? 0 : Math.max(0, QUIET_MS - (now - lastRequest)));
    setTimeout(async () => {
        stepping = false;
        if (busy || queue.length) return;
        busy = true;
        lastStep = performance.now();
        try {
            const cooperative = py.pyimport("plexora.browser.cooperative");
            const began = performance.now();
            const quiet = began - Math.max(lastRequest, lastAnswer) >= BACKGROUND_QUIET_MS;
            inStep = true;
            const outcome = await call(cooperative.step, 50, quiet).finally(() => { inStep = false; });
            pendingWork = outcome.get("pending");
            pendingBackground = outcome.get("background");
            dueInMs = Number(outcome.get("wait_ms")) || 0;
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
            if (error && error.pyodide_fatal_error) fatal(error);
            else if (/fatally failed and can no longer be used/.test(String(error?.message || error))) fatal(error);
            // Once Python has failed, the step loop ends: it was asking a dead
            // Pyodide again every few hundred ms, one console line each time.
            if (!fatalTold) self.postMessage({ log: `step: ${error?.message || error}` });
        } finally {
            busy = false;
        }
        if (queue.length) drain();
        else if (pendingWork || pendingBackground) step();
    }, wait);
}

// What escaped every handler, with where it came from, said to the page
// before the worker is gone (worker.onerror has the message only).
self.addEventListener("error", (event) => {
    self.postMessage({ log: `uncaught: ${event.error?.stack || event.message}` });
});
self.addEventListener("unhandledrejection", (event) => {
    self.postMessage({ log: `unhandled: ${event.reason?.stack || event.reason}` });
});

self.onmessage = ({ data }) => {
    if (data.op === "warm") {
        // Not queued: it runs beside whatever comes next, and boot waits on it.
        warm(data.args || {});
        return;
    }
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
    if (data.op === "cancelJob") {
        cancelledJobs.add(String(data.args && data.args.id));
        return;
    }
    if (data.op === "quit") {
        quit();
        return;
    }
    if (data.op === "cancel") {
        // A request the page gave up on (its AbortSignal), dropped if Python
        // has not begun it; one already begun runs to its end.
        const at = queue.findIndex((queued) => queued.id === data.args.id);
        const later = retrying.get(data.args.id);
        if (at >= 0 || later !== undefined) {
            if (at >= 0) queue.splice(at, 1);
            if (later !== undefined) { clearTimeout(later); retrying.delete(data.args.id); }
            self.postMessage({ id: data.args.id, ok: false, error: "AbortError: the page gave up on this request", aborted: true });
        }
        return;
    }
    if (data.op === "request") {
        lastRequest = performance.now();
        // What it queues runs on the usual pace, not after another task's delay.
        dueInMs = 0;
    }
    queue.push(data);
    drain();
};
mark("loaded");
