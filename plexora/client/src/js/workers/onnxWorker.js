// Magic select's model on the website: MobileSAM on onnxruntime-web, in a
// worker of its own beside the viewer, so neither a download nor a run ever
// holds the page. services/onnxExecutor.js starts it and hands it Python's
// "sam" questions (plexora/browser/sam_page.py asks them; vision/sam.py and
// vision/segment.py do everything else, as on the desktop).
//
// Messages ({id, op, args} -> {id, ok, result | error}):
//   configure {ort, models, device}  where the runtime (onnxruntime-web, in the
//                       Python bundle's ort/) and the model files are served,
//                       and a device the page's address forces (tests)
//   status {model}      what storage holds, what is downloading, the runtime;
//                       cheap (a page setting up polls it once a second)
//   install {model}     fetch what is missing; returns at once, status shows
//                       the progress, and the sessions are made after it
//   cancel              stop a download
//   load {model, device}  the two sessions, made once -> {device} | {missing}
//   embed {image}       -> {embedding, key, ms, device}
//   decode {key | embedding, coords, labels, mask_input, has_mask}
//                       -> {iou, low, key, ms} | {missing: true} (key unknown)
//   remove {model}      the files deleted and the sessions dropped
//   reset {device}      sessions and embeddings dropped; the next load on
//                       `device` (tests compare the two runtimes)
//   times               the latest runs: {op, ep, ms}
// `model` is vision/sam_weights.py's pins: {name, version, dir, files: [{name,
// role, sha256, bytes}]}. Arrays travel as {dtype, shape, data}.
//
// **Where the files are.** OPFS plexora/cache/models/<dir>/<name>, each with
// "<name>.sha256" written after it holding the pin it matched. Status counts a
// file there when it has the pinned size and that sidecar (a stat, never a
// hash, as the desktop's cheap check); `load` reads it whole and checks its
// sha256 again before a session is made of it, as the desktop does, and a
// file that fails is deleted and reported missing, so setup fetches it anew.
// A download is checked before anything is written: a file that arrives
// different is never stored.
//
// **Which runtime.** WebGPU when the browser has an adapter
// (ort.webgpu.min.mjs: the WebGPU execution provider and WebAssembly in one),
// WebAssembly otherwise (ort.wasm.min.mjs, smaller), threaded when the page
// is cross-origin isolated (the site always is). A WebGPU session that cannot
// be made, or whose first run fails, falls back to WebAssembly and says why
// (`device.fallback`), as the desktop's GPU providers fall back to CPU.

const RUNTIMES = { webgpu: "ort.webgpu.min.mjs", wasm: "ort.wasm.min.mjs" };
//: Every session's options: all graph optimisations, as the desktop, and only
//: errors logged (ORT's node-placement warnings would reach the console).
const SESSION = { graphOptimizationLevel: "all", logSeverityLevel: 3 };
//: Embeddings kept for decoder calls that name them (segment.py keeps 8 views).
const KEEP = 8;
const RECENT = 100;

let config = { ort: null, models: null, device: null };
//: The runtime's own record (ort/ort.json: version and files), once read.
let runtimeInfo;
//: The GPU adapter, once asked: undefined not asked, null none.
let adapter;
//: The last pins Python sent, for the sessions made after a download.
let lastModel = null;
let lastDevice = "auto";
const download = { running: false, done: 0, total: 0, file: null, error: null,
                   started_at: null, finished_at: null };
let aborter = null;
//: {ort, encoder, decoder, device}, once made; `loading` while being made.
let sessions = null;
let loading = null;
//: Why the sessions could not be made, until the next setup or reset.
let loadError = null;
const embeddings = new Map();
let nextKey = 1;
const runs = [];
//: What status found in storage, per model dir, until a write or delete.
const found = new Map();

const now = () => Date.now() / 1000;
const readable = (error) => String((error && error.message) || error || "failed").slice(0, 300);

// -- storage ----------------------------------------------------------------

async function folder(model, create) {
    let dir = await navigator.storage.getDirectory();
    for (const part of ["plexora", "cache", "models", ...String(model.dir || model.name).split("/")]) {
        if (part) dir = await dir.getDirectoryHandle(part, { create });
    }
    return dir;
}

async function textOf(dir, name) {
    return (await (await dir.getFileHandle(name)).getFile()).text();
}

//: Per file {present, ok, bytes}, as vision/model_files.py's file_status.
async function filesOf(model) {
    const key = model.dir || model.name;
    if (found.has(key)) return found.get(key);
    const rows = {};
    let dir = null;
    try { dir = await folder(model, false); } catch (error) { dir = null; }
    for (const file of model.files) {
        let present = false;
        let ok = false;
        if (dir) {
            try {
                const stored = await (await dir.getFileHandle(file.name)).getFile();
                present = true;
                const pin = (await textOf(dir, `${file.name}.sha256`)).trim();
                ok = stored.size === file.bytes && pin === file.sha256;
            } catch (error) { /* absent, or no sidecar: not there */ }
        }
        rows[file.name] = { present, ok, bytes: file.bytes };
    }
    found.set(key, rows);
    return rows;
}

async function write(dir, name, bytes) {
    const handle = await dir.getFileHandle(name, { create: true });
    if (typeof handle.createSyncAccessHandle === "function") {
        const access = await handle.createSyncAccessHandle();
        try {
            access.truncate(0);
            access.write(bytes, { at: 0 });
            access.flush();
        } finally {
            access.close();
        }
        return;
    }
    const writable = await handle.createWritable();
    await writable.write(bytes);
    await writable.close();
}

async function forgetFile(dir, name) {
    for (const entry of [name, `${name}.sha256`]) {
        try { await dir.removeEntry(entry); } catch (error) { /* not there */ }
    }
}

async function sha256(bytes) {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

// -- the one-time setup ---------------------------------------------------------

function base() {
    const models = config.models || "/models/";
    return models.endsWith("/") ? models : `${models}/`;
}

async function fetchFile(model, file, signal, onBytes) {
    const url = `${base()}${model.dir || ""}${file.name}`;
    const response = await fetch(url, { signal, cache: "no-store" });
    if (!response.ok) throw new Error(`${file.name}: the server answered HTTP ${response.status}`);
    // A site that answers every path with its page (Pages' fallback) has no
    // such file: said now, not as a checksum after 28 MB of HTML.
    const type = response.headers.get("content-type") || "";
    const length = Number(response.headers.get("content-length") || 0);
    if (type.includes("text/html") || (length && !response.headers.get("content-encoding") && length !== file.bytes)) {
        throw new Error(`${file.name} is not at ${url}`);
    }
    const bytes = new Uint8Array(file.bytes);
    let at = 0;
    const reader = response.body.getReader();
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (at + value.length > bytes.length) {
            throw new Error(`${file.name} is larger than it should be; nothing was installed.`);
        }
        bytes.set(value, at);
        at += value.length;
        onBytes(value.length);
    }
    if (at !== file.bytes || await sha256(bytes) !== file.sha256) {
        throw new Error(`${file.name} did not match its checksum; nothing was installed.`);
    }
    return bytes;
}

async function runInstall(model) {
    const { signal } = aborter;
    try {
        const rows = await filesOf(model);
        const todo = model.files.filter((file) => !rows[file.name].ok);
        download.file = todo[0] ? todo[0].name : null;
        download.done = download.total - todo.reduce((sum, file) => sum + file.bytes, 0);
        const dir = await folder(model, true);
        for (const file of todo) {
            download.file = file.name;
            const bytes = await fetchFile(model, file, signal, (n) => { download.done += n; });
            await write(dir, file.name, bytes);
            await write(dir, `${file.name}.sha256`, new TextEncoder().encode(file.sha256));
            found.delete(model.dir || model.name);
        }
        // Set up means ready to answer a click: the runtime fetched and both
        // sessions made (and run once) before the setup says it is done.
        download.file = "runtime";
        const made = await load({ model, device: lastDevice });
        if (made.error) download.error = made.error;
    } catch (error) {
        download.error = signal.aborted ? null : readable(error);
    } finally {
        found.delete(model.dir || model.name);
        download.running = false;
        download.finished_at = now();
        aborter = null;
    }
}

function install({ model }) {
    if (model) lastModel = model;
    if (!download.running && lastModel) {
        // Running from this moment: a status asked right after says so.
        Object.assign(download, { running: true, done: 0, file: null, error: null,
                                  total: lastModel.files.reduce((sum, file) => sum + file.bytes, 0),
                                  started_at: now(), finished_at: null });
        aborter = new AbortController();
        loadError = null;
        runInstall(lastModel);
    }
    return status({ model: lastModel });
}

function cancel() {
    if (aborter) aborter.abort();
    return true;
}

// -- status -----------------------------------------------------------------------

async function runtime() {
    if (runtimeInfo !== undefined) return runtimeInfo;
    if (!config.ort || typeof WebAssembly !== "object") {
        runtimeInfo = null;
        return runtimeInfo;
    }
    try {
        const response = await fetch(`${config.ort}ort.json`);
        runtimeInfo = response.ok ? await response.json() : null;
    } catch (error) {
        runtimeInfo = null;
    }
    return runtimeInfo;
}

//: Asked once; every caller waits for the same answer (a caller that came
//: while the first asked must not read "none": the background load a later
//: visit's status starts asks as status itself does).
let adapterAsked = null;
//: Why there is no adapter, for `device.gpu_note`.
let adapterWhy = null;
function gpuAdapter() {
    if (!adapterAsked) {
        adapterAsked = (async () => {
            try {
                if (!navigator.gpu) adapterWhy = "this browser has no WebGPU";
                else adapter = await navigator.gpu.requestAdapter();
                if (navigator.gpu && !adapter) adapterWhy = "WebGPU offered no adapter";
            } catch (error) {
                adapterWhy = `WebGPU: ${readable(error)}`;
            }
            adapter = adapter || null;
            return adapter;
        })();
    }
    return adapterAsked;
}

function adapterName(found) {
    const info = found && found.info;
    return info ? [info.vendor, info.architecture].filter(Boolean).join(" ") || "yes" : null;
}

//: Which runtime a load on `device` uses: "webgpu" or "wasm" (null: none
//: will do, and why).
async function planned(device) {
    const wanted = config.device || device || "auto";
    if (["cpu", "wasm"].includes(wanted)) return { ep: "wasm" };
    const gpu = await gpuAdapter();
    if (["gpu", "webgpu"].includes(wanted) && !gpu) {
        return { ep: null, why: "a GPU was asked for and this browser offers none (WebGPU)" };
    }
    return { ep: gpu ? "webgpu" : "wasm" };
}

async function status({ model } = {}) {
    if (model) lastModel = model;
    const info = await runtime();
    const out = {
        runtime: { installed: Boolean(info), version: info ? `onnxruntime-web ${info.version}` : null,
                   install: null },
        download: download.started_at === null ? null
            : { ...download, fraction: download.total ? Math.round((download.done / download.total) * 1e4) / 1e4 : 0 },
        device: sessions ? sessions.device : null,
        error: sessions ? null : loadError,
        hint: info ? null : "This site was built without the runtime magic select needs.",
    };
    if (lastModel) {
        const rows = await filesOf(lastModel);
        out.weights = { dir: `browser storage: plexora/cache/models/${lastModel.dir || lastModel.name}`,
                        source: "browser", files: rows,
                        missing: Object.keys(rows).filter((name) => !rows[name].ok) };
        // Armed with the model already stored (a later visit): the sessions
        // are made now, in the background, so the first click does not wait
        // for them -- the desktop warms its session at start the same way.
        // Once: after a failure it waits for the next setup (install) or reset.
        if (info && !out.weights.missing.length && !sessions && !loading && !download.running && !loadError) {
            load({ model: lastModel, device: lastDevice }).catch(() => null);
        }
    }
    if (!out.device && info) {
        const plan = await planned(lastDevice);
        out.device = { requested: config.device || lastDevice, encoder: plan.ep, decoder: plan.ep,
                       gpu: plan.ep === "webgpu", fallback: plan.why || null,
                       adapter: adapterName(adapter), gpu_note: adapterWhy, runtime: out.runtime.version };
    }
    return out;
}

// -- sessions ---------------------------------------------------------------------

async function readVerified(dir, file) {
    const bytes = new Uint8Array(await (await (await dir.getFileHandle(file.name)).getFile()).arrayBuffer());
    if (bytes.length !== file.bytes || await sha256(bytes) !== file.sha256) return null;
    return bytes;
}

async function makeSessions(model, device) {
    const info = await runtime();
    if (!info) return { error: "This site was built without the runtime magic select needs." };
    const dir = await folder(model, false).catch(() => null);
    const bytes = {};
    const missing = [];
    for (const file of model.files) {
        const read = dir ? await readVerified(dir, file).catch(() => null) : null;
        if (read) bytes[file.role] = read;
        else {
            missing.push(file.name);
            if (dir) await forgetFile(dir, file.name);
        }
    }
    found.delete(model.dir || model.name);
    if (missing.length) return { missing };
    const plan = await planned(device);
    if (!plan.ep) return { error: plan.why };
    const where = new URL(config.ort, self.location.href).href;
    const ort = await import(`${where}${RUNTIMES[plan.ep]}`);
    ort.env.wasm.wasmPaths = where;
    ort.env.logLevel = "error";
    const record = { requested: config.device || device || "auto", encoder: null, decoder: null,
                     gpu: false, fallback: null, adapter: adapterName(adapter), gpu_note: adapterWhy,
                     runtime: `onnxruntime-web ${info.version}` };
    const make = async (role) => {
        if (plan.ep === "webgpu") {
            try {
                const session = await ort.InferenceSession.create(bytes[role], {
                    executionProviders: ["webgpu"], ...SESSION });
                record[role] = "webgpu";
                return session;
            } catch (error) {
                record.fallback = `${role}: webgpu failed (${readable(error)})`.slice(0, 300);
            }
        }
        const session = await ort.InferenceSession.create(bytes[role], {
            executionProviders: ["wasm"], ...SESSION });
        record[role] = "wasm";
        return session;
    };
    const made = { ort, encoder: await make("encoder"), decoder: await make("decoder"), device: record,
                   model, files: Object.fromEntries(model.files.map((file) => [file.role, file])) };
    record.threads = ort.env.wasm.numThreads || null;
    record.gpu = record.encoder === "webgpu" || record.decoder === "webgpu";
    // One run of each now, as the desktop warms its session: a GPU compiles
    // its shaders on the first run (most of a second), not inside a click.
    const t = performance.now();
    const zeros = new ort.Tensor("float32", new Float32Array(3 * 1024 * 1024), [1, 3, 1024, 1024]);
    let warm;
    try {
        warm = await made.encoder.run({ image: zeros });
    } catch (error) {
        if (record.encoder !== "webgpu") throw error;
        made.encoder = await fallBack(made, "encoder", bytes.encoder, error);
        warm = await made.encoder.run({ image: zeros });
    }
    try {
        await made.decoder.run(decoderFeeds(ort, warm.image_embeddings.data,
            new Float32Array(4), new Float32Array([1, -1]), 1, 2, new Float32Array(256 * 256), 0));
    } catch (error) {
        if (record.decoder !== "webgpu") throw error;
        made.decoder = await fallBack(made, "decoder", bytes.decoder, error);
    }
    record.warm_ms = Math.round(performance.now() - t);
    return made;
}

//: A WebGPU session that was made but failed to run: WebAssembly from now on
//: (`bytes` the model file, read again from storage when not at hand).
async function fallBack(made, role, bytes, error) {
    made.device.fallback = `${role}: webgpu failed at run (${readable(error)})`.slice(0, 300);
    made.device[role] = "wasm";
    made.device.gpu = made.device.encoder === "webgpu" || made.device.decoder === "webgpu";
    const model = bytes || await readVerified(await folder(made.model, false), made.files[role]);
    if (!model) throw new Error(`${made.files[role].name} is no longer in storage`);
    return made.ort.InferenceSession.create(model, {
        executionProviders: ["wasm"], ...SESSION });
}

async function load({ model, device } = {}) {
    if (model) lastModel = model;
    if (device) lastDevice = device;
    if (sessions) return { device: sessions.device };
    if (!lastModel) return { error: "no model was named" };
    if (!loading) {
        loading = makeSessions(lastModel, lastDevice).then((made) => {
            if (made && made.encoder) sessions = made;
            return made;
        }, (error) => ({ error: readable(error) })).finally(() => { loading = null; });
    }
    const made = await loading;
    loadError = made.error || null;
    if (made.missing) return { missing: made.missing };
    if (made.error) return { error: made.error };
    return { device: made.device };
}

// -- runs ---------------------------------------------------------------------------

function floats(entry) {
    const data = entry.data;
    if (data instanceof Float32Array) return data;
    if (data.byteOffset % 4 === 0) return new Float32Array(data.buffer, data.byteOffset, data.byteLength / 4);
    return new Float32Array(data.slice().buffer);
}

//: An answer's array: its own buffer, so it can be moved, not copied.
function wire(tensor, keep = false) {
    let data = tensor.data;
    if (keep || data.byteLength !== data.buffer.byteLength || !(data.buffer instanceof ArrayBuffer)) {
        data = data.slice();
    }
    return { dtype: "float32", shape: Array.from(tensor.dims), data };
}

function decoderFeeds(ort, embedding, coords, labels, batch, points, mask, has) {
    return {
        image_embeddings: new ort.Tensor("float32", embedding, [1, 256, 64, 64]),
        point_coords: new ort.Tensor("float32", coords, [batch, points, 2]),
        point_labels: new ort.Tensor("float32", labels, [batch, points]),
        mask_input: new ort.Tensor("float32", mask, [batch, 1, 256, 256]),
        has_mask_input: new ort.Tensor("float32", new Float32Array([has]), [1]),
    };
}

function remember(embedding) {
    const key = `e${nextKey++}`;
    embeddings.set(key, embedding);
    while (embeddings.size > KEEP) embeddings.delete(embeddings.keys().next().value);
    return key;
}

function timed(op, ep, ms) {
    runs.push({ op, ep, ms: Math.round(ms * 10) / 10, at: Math.round(performance.now()) });
    if (runs.length > RECENT) runs.shift();
}

async function ready() {
    if (!sessions) {
        const made = await load({});
        if (!sessions) throw new Error(made.error || (made.missing ? "the model files are missing" : "no session"));
    }
    return sessions;
}

async function embed({ image }) {
    const made = await ready();
    const input = new made.ort.Tensor("float32", floats(image), image.shape);
    const t = performance.now();
    let out;
    try {
        out = await made.encoder.run({ image: input });
    } catch (error) {
        if (made.device.encoder !== "webgpu") throw error;
        made.encoder = await fallBack(made, "encoder", null, error);
        out = await made.encoder.run({ image: input });
    }
    const ms = performance.now() - t;
    timed("embed", made.device.encoder, ms);
    const embedding = out.image_embeddings;
    const key = remember(embedding.data.slice());
    return { embedding: wire(embedding), key, ms, device: made.device };
}

async function decode({ key, embedding, coords, labels, mask_input: mask, has_mask: has }) {
    const made = await ready();
    let held = key ? embeddings.get(key) : null;
    if (key && !held && !embedding) return { missing: true };
    if (!held) {
        held = floats(embedding).slice();
        key = remember(held);
    } else {
        embeddings.delete(key);
        embeddings.set(key, held);
    }
    const [batch, points] = coords.shape;
    const feeds = decoderFeeds(made.ort, held, floats(coords), floats(labels), batch, points,
                               floats(mask), floats(has)[0]);
    const t = performance.now();
    let out;
    try {
        out = await made.decoder.run(feeds);
    } catch (error) {
        if (made.device.decoder !== "webgpu") throw error;
        made.decoder = await fallBack(made, "decoder", null, error);
        out = await made.decoder.run(feeds);
    }
    const ms = performance.now() - t;
    timed("decode", made.device.decoder, ms);
    return { iou: wire(out.iou_predictions), low: wire(out.low_res_masks), key, ms, device: made.device };
}

async function remove({ model } = {}) {
    model = model || lastModel;
    await drop();
    const removed = [];
    if (!model) return { removed };
    const dir = await folder(model, false).catch(() => null);
    if (dir) {
        for (const file of model.files) {
            try {
                await dir.getFileHandle(file.name);
                removed.push(file.name);
            } catch (error) { continue; }
            await forgetFile(dir, file.name);
        }
    }
    found.delete(model.dir || model.name);
    return { removed };
}

async function drop() {
    if (loading) await loading.catch(() => null);
    const made = sessions;
    sessions = null;
    embeddings.clear();
    if (made) {
        for (const session of [made.encoder, made.decoder]) {
            try { await session.release(); } catch (error) { /* gone already */ }
        }
    }
}

const OPS = {
    configure: (args) => {
        config = { ...config, ...args };
        runtimeInfo = undefined;
        return true;
    },
    status,
    install,
    cancel,
    load,
    embed,
    decode,
    remove,
    reset: async ({ device } = {}) => {
        await drop();
        loadError = null;
        runs.length = 0;
        if (device !== undefined) config.device = device || null;
        return true;
    },
    times: () => runs.slice(),
};

//: The arrays of an answer, moved to the page rather than copied.
function transfersOf(result) {
    const out = [];
    for (const value of Object.values(result || {})) {
        const data = value && value.data;
        if (ArrayBuffer.isView(data) && data.buffer instanceof ArrayBuffer && !out.includes(data.buffer)) {
            out.push(data.buffer);
        }
    }
    return out;
}

self.onmessage = async ({ data: { id, op, args } }) => {
    try {
        if (!OPS[op]) throw new Error(`no op ${op}`);
        const result = await OPS[op](args || {});
        self.postMessage({ id, ok: true, result }, transfersOf(result));
    } catch (error) {
        self.postMessage({ id, ok: false, error: readable(error) });
    }
};
