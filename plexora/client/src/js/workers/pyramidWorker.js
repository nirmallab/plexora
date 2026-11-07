/**
 * A pyramid build on the website (services/browserPyramid.js starts one per
 * flat image Python asks for): node/pyramid.js over the browser node's tiff
 * workers as its hands, writing the copy image_pyramid.pyramidize_image would
 * write into the site's own storage (OPFS), where Python then reads it.
 *
 * This worker owns the file: a sync access handle exists only in a dedicated
 * worker. Output, under OPFS plexora/derived/<key>/: the copy under its own
 * name, then `manifest.json` {key, name, bytes, shape, dtype, factors, tile,
 * ms} -- written last, so a build that stopped part way is never taken for a
 * finished one (it is built again).
 *
 * Or, given `folder` (a directory the user granted, beside the original), the
 * copy goes there under its own name, through the folder's writable stream:
 * the browser writes a temporary file and puts it in place on close, so the
 * name never holds half a copy. A file already under that name is never
 * written over: it is handed back (`exists`) for Python to judge.
 *
 * {op: "build", file, key, name, shape: [C, H, W], dtype, factors, tile,
 *  description, hands: <tiff worker URL>, workers, folder?}
 *  -> {progress: {tiles, total, bands, bandsTotal}}..., then
 *     {done, file, bytes, timings}, {exists, file} or {error}
 */
import { build, dtypeOf } from "../node/pyramid.js";

/** The browser node's tiff workers, each opened on the source. */
function startHands(url, count, file) {
    const workers = [];
    const pending = new Map();
    const load = [];
    let nextId = 1;
    for (let i = 0; i < count; i += 1) {
        const worker = new Worker(url);
        worker.onmessage = ({ data }) => {
            const entry = pending.get(data.id);
            if (!entry) return;
            pending.delete(data.id);
            if (data.ok) entry.resolve(data.result);
            else entry.reject(new Error(data.error));
        };
        workers.push(worker);
        load.push(0);
    }
    const call = (i, op, args, transfer = []) => new Promise((resolve, reject) => {
        const id = nextId++;
        pending.set(id, { resolve, reject });
        load[i] += 1;
        workers[i].postMessage({ id, op, ...args }, transfer);
    }).finally(() => { load[i] -= 1; });
    return {
        count,
        opened: Promise.all(workers.map((_, i) => call(i, "open", { file, decoder: "native" }))),
        block: (i, job) => call(i, "block", job),
        deflate: (bytes) => {
            const i = load.indexOf(Math.min(...load));
            return call(i, "deflate", { buffer: bytes.buffer }, [bytes.buffer]).then((r) => r.bytes);
        },
        stop: () => workers.forEach((worker) => worker.terminate()),
    };
}

async function derivedDir(key) {
    const root = await navigator.storage.getDirectory();
    const plexora = await root.getDirectoryHandle("plexora", { create: true });
    const derived = await plexora.getDirectoryHandle("derived", { create: true });
    try { await derived.removeEntry(key, { recursive: true }); } catch (error) { /* none yet */ }
    return derived.getDirectoryHandle(key, { create: true });
}

async function writeJson(dir, name, value) {
    const handle = await (await dir.getFileHandle(name, { create: true })).createSyncAccessHandle();
    try {
        handle.truncate(0);
        handle.write(new TextEncoder().encode(JSON.stringify(value)), { at: 0 });
        handle.flush();
    } finally {
        handle.close();
    }
}

//: The parts written at their offsets, as one file's parts: the gaps (the
//: 8-byte alignment before the directories) are zeros.
function inOrder(parts, bytes) {
    const out = [];
    let at = 0;
    for (const offset of [...parts.keys()].sort((a, b) => a - b)) {
        const part = parts.get(offset);
        if (offset < at) throw new Error("the copy's parts overlap");
        if (offset > at) out.push(new Uint8Array(offset - at));
        out.push(part);
        at = offset + part.byteLength;
    }
    if (at < bytes) out.push(new Uint8Array(bytes - at));
    return out;
}

//: Why a build stopped, as the import says it. A full quota is a sentence
//: (Firefox lets a site keep about 10 GB), not a storage error's name.
async function sentence(error) {
    if (error && error.name === "QuotaExceededError") {
        let allows = "";
        try {
            const { quota } = await navigator.storage.estimate();
            if (quota) allows = `: it allows this site ${(quota / 1e9).toFixed(1)} GB`;
        } catch (ignored) { /* no estimate */ }
        return `this browser has no room left for it${allows}`;
    }
    return String(error && error.message || error).slice(0, 500);
}

self.onmessage = async ({ data }) => {
    if (data.op !== "build") return;
    const { file, key, name, shape, dtype, factors, tile, description, hands: url, workers, folder } = data;
    const t0 = performance.now();
    let hands = null;
    let access = null;
    let writable = null;
    try {
        hands = startHands(url, Math.max(1, workers), file);
        const [opened] = await hands.opened;
        const { geometry } = opened;
        // The stack Python asked about, read the way Python reads it.
        const [channels, height, width] = shape;
        const found = dtypeOf(geometry);
        if (geometry.numChannels !== channels || geometry.width !== width || geometry.height !== height
            || found !== dtype) {
            throw new Error(`the page reads ${geometry.numChannels} x ${geometry.height} x ${geometry.width} ${found}, `
                            + `Python ${channels} x ${height} x ${width} ${dtype}`);
        }
        if (folder) {
            const there = await folder.getFileHandle(name).catch(() => null);
            if (there) {
                self.postMessage({ exists: true, file: await there.getFile() });
                return;
            }
            const handle = await folder.getFileHandle(name, { create: true });
            writable = await handle.createWritable();
            // One write at a time, in the order asked: the stream takes no
            // two at once.
            let queue = Promise.resolve();
            const result = await build({
                geometry, dtype, channels, factors, tile, description, hands,
                sink: { write: (bytes, at) => (queue = queue.then(
                    () => writable.write({ type: "write", position: at, data: bytes }))) },
                progress: (progress) => self.postMessage({ progress }),
            });
            await queue;
            await writable.close();
            writable = null;
            const ms = Math.round(performance.now() - t0);
            self.postMessage({ done: true, file: await handle.getFile(), bytes: result.bytes,
                               timings: { ...result.timings, ms } });
            return;
        }
        // This site's storage, where the browser can write there; else the
        // copy is put together in memory (WebKit on Windows has no backend
        // for it; a private window may have no storage at all) and handed
        // back for the page to keep where it can (`kept: "memory"`).
        let dir = null;
        try {
            dir = await derivedDir(key);
            access = await (await dir.getFileHandle(name, { create: true })).createSyncAccessHandle();
        } catch (error) {
            access = null;
        }
        if (!access) {
            const parts = new Map();
            const result = await build({
                geometry, dtype, channels, factors, tile, description, hands,
                sink: { write: (bytes, at) => { parts.set(at, bytes); } },
                progress: (progress) => self.postMessage({ progress }),
            });
            const copy = new File(inOrder(parts, result.bytes), name);
            const ms = Math.round(performance.now() - t0);
            self.postMessage({ done: true, file: copy, bytes: result.bytes, kept: "memory",
                               timings: { ...result.timings, ms } });
            return;
        }
        access.truncate(0);
        const result = await build({
            geometry, dtype, channels, factors, tile, description, hands,
            sink: { write: (bytes, at) => { access.write(bytes, { at }); } },
            progress: (progress) => self.postMessage({ progress }),
        });
        access.flush();
        access.close();
        access = null;
        const ms = Math.round(performance.now() - t0);
        await writeJson(dir, "manifest.json", { key, name, bytes: result.bytes, shape, dtype, factors, tile, ms });
        const copy = await (await dir.getFileHandle(name)).getFile();
        self.postMessage({ done: true, file: copy, bytes: result.bytes, timings: { ...result.timings, ms } });
    } catch (error) {
        self.postMessage({ error: await sentence(error) });
    } finally {
        if (access) try { access.close(); } catch (error) { /* closed */ }
        // A copy that did not finish is dropped: abort() discards the
        // browser's temporary file and leaves the folder as it was.
        if (writable) try { await writable.abort(); } catch (error) { /* gone */ }
        if (hands) hands.stop();
    }
};
