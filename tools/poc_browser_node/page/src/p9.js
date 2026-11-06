/**
 * P9's page side: Plexora's Python in a worker beside the viewer.
 *
 * - p9Boot()        starts workers/pyWorker.js and resolves when the gating
 *                   capabilities are registered ("ready": the AI tools could
 *                   be enabled); every phase is reported in ms since this
 *                   page's navigation began, and the worker's fetches as bytes.
 * - p9Session()     mounts the picked fixture files (WORKERFS: read on demand,
 *                   as a user's pick of a large image would be), writes the
 *                   project's config, and runs page/py/p9_run.py's oracle
 *                   session; returns its dump for bench/compare_p9.py.
 * - p9ViewerOpen()  the viewer's own open and pan, with or without the worker
 *                   booting beside it, and the main thread's long tasks.
 */
import { TiffPool } from "./tiffPool.js";
import { createViewer, waitFull } from "./viewer.js";
import { ensureColumnStore } from "./table.js";

const PY_SOURCES = ["p9_boot.py", "p9_run.py", "p9_oracle.py"];
const sinceNavigation = (epochMs) => epochMs - performance.timeOrigin;

export class PyWorker {
    constructor() {
        this.worker = new Worker(new URL("src/workers/pyWorker.js", location.href), { type: "module" });
        this.pending = new Map();
        this.marks = {};
        this.logs = [];
        this.next = 1;
        this.worker.onmessage = ({ data }) => {
            if (data.mark) this.marks[data.mark] = sinceNavigation(data.at);
            else if (data.log) this.logs.push(String(data.log).slice(0, 300));
            else {
                const { resolve, reject } = this.pending.get(data.id);
                this.pending.delete(data.id);
                if (data.ok) resolve(data.result); else reject(new Error(data.error));
            }
        };
        this.worker.onerror = (e) => this.logs.push(`worker error: ${e.message}`);
    }

    call(op, args = {}, transfer = []) {
        const id = this.next++;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            this.worker.postMessage({ id, op, args }, transfer);
        });
    }

    terminate() { this.worker.terminate(); }
}

async function bootLists() {
    const [manifest, packages] = await Promise.all([
        fetch("/wheels/manifest.json").then((r) => r.json()),
        fetch(new URL("py/p9_packages.json", location.href)).then((r) => r.json()),
    ]);
    return {
        packages: packages.pyodide,
        lazy: packages.lazy || [],
        wheels: manifest.wheels.filter((w) => !packages.skipWheels?.includes(w.file.split("-")[0]))
            .map((w) => `/wheels/${w.file}`),
        sources: Object.fromEntries(PY_SOURCES.map((n) => [n, new URL(`py/${n}`, location.href).href])),
    };
}

let current = null;

/**
 * Boot Python in a worker; resolves at "ready". `packages` replaces the
 * package list and `bare` stops after the core (profiling: what each package
 * and import costs on its own).
 */
export async function p9Boot({ packages = null, bare = false, lazy = false } = {}) {
    const t0 = performance.now();
    const lists = await bootLists();
    if (packages) lists.packages = packages;
    if (lazy) lists.packages = lists.packages.filter((p) => !lists.lazy.includes(p));
    if (bare) lists.bare = true;
    const py = new PyWorker();
    current = py;
    const out = await py.call("boot", lists);
    const resources = await py.call("resources");
    const sum = (f) => resources.filter(f).reduce((a, r) => a + (r.transfer || 0), 0);
    return {
        startedAt: t0,
        marks: py.marks,
        phases: Object.fromEntries(Object.entries(out.phases).map(([k, v]) => [k, sinceNavigation(v)])),
        shims: out.shims,
        ready: out.ready,
        heap: await py.call("heap"),
        bytes: {
            total: sum(() => true),
            pyodideCore: sum((r) => /\/pyodide\/(pyodide|python_stdlib)/.test(r.name)),
            packages: sum((r) => /\/pyodide\/.*\.(whl|zip)$/.test(r.name)),
            wheels: sum((r) => r.name.startsWith("/wheels/")),
            fetches: resources.length,
            cacheHits: resources.filter((r) => r.transfer === 0 && r.decoded > 0).length,
            files: resources.slice().sort((a, b) => b.encoded - a.encoded)
                .map((r) => ({ name: r.name.split("/").pop(), encoded: r.encoded, transfer: r.transfer,
                               start: Math.round(r.start), ms: Math.round(r.ms) })),
        },
        logs: py.logs,
    };
}

function pickedFiles(names) {
    const files = [...document.getElementById("picker").files];
    return names.map((name) => {
        const file = files.find((f) => f.name === name);
        if (!file) throw new Error(`no picked file named ${name}`);
        return file;
    });
}

/**
 * The picked files where the project's config names them: images and masks
 * stay on WORKERFS (read on demand, the user's file never copied); a table
 * is copied into MEMFS, because polars memory-maps what it reads and a
 * WORKERFS file cannot be mapped ("No such device").
 */
async function placeFiles(py, dir, files, copied) {
    await py.call("mount", { dir: "/picked", files });
    const placed = {};
    for (const file of files) {
        if (copied.test(file.name)) {
            const data = new Uint8Array(await file.arrayBuffer());
            await py.call("write", { path: `${dir}/${file.name}`, data }, [data.buffer]);
            placed[file.name] = "memfs";
        } else {
            await py.call("link", { target: `/picked/${file.name}`, path: `${dir}/${file.name}` });
            placed[file.name] = "workerfs";
        }
    }
    return placed;
}

/** The picked fixture placed and its config written: the project exists. */
export async function p9Prepare({ data = ["image.ome.tif", "mask.tif", "cells.csv"],
                                  config = "config.browser.json", info = "info.json" } = {}) {
    const py = current;
    if (!py) throw new Error("p9Boot first");
    const placed = await placeFiles(py, "/data/_gsynth_files", pickedFiles(data), /\.(csv|tsv|parquet)$/i);
    const [configFile, infoFile] = pickedFiles([config, info]);
    await py.call("write", { path: "/data/config.json", data: await configFile.text() });
    await py.call("write", { path: "/data/info.json", data: await infoFile.text() });
    return placed;
}

/** The oracle session on the picked fixture; returns p9_run's dump. */
export async function p9Session(options = {}) {
    const py = current;
    const placed = await p9Prepare(options);
    const t0 = performance.now();
    const text = await py.call("run", {
        code: "import p9_run, p9_boot, json\n"
            + (options.prototype === "metacache" ? "p9_boot.prototype_metadata_cache()\n" : "")
            + "_d = p9_run.session('/data', json.load(open('/data/info.json')))\n"
            + "_d['counters'] = p9_boot.counters()\n"
            + "json.dumps(_d)",
    });
    const wall = performance.now() - t0;
    const dump = JSON.parse(text);
    return { wall, heap: await py.call("heap"), reads: await py.call("reads"), dump, placed, logs: py.logs };
}

/**
 * L: a large cell table handed to Python. polars in Pyodide cannot parse a
 * large CSV (its reader spawns threads: "could not spawn threads"), so the
 * page's column store parses it (table.js, P3's JS workers into OPFS) and
 * the numeric columns go to the worker as Float64Arrays, which become a
 * polars frame without a parse; then one describe and one filter, and the heap.
 */
export async function p9TableProbe({ name, column }) {
    const py = current;
    const file = pickedFiles([name])[0];
    const t0 = performance.now();
    const { table, timings } = await ensureColumnStore(file, { force: true });
    const storeMs = performance.now() - t0;
    const t1 = performance.now();
    const names = table.numeric;
    const buffers = [];
    for (const n of names) buffers.push((await table.column(n)).buffer);
    table.forget();
    await py.call("columns", { names, buffers }, buffers);
    const handMs = performance.now() - t1;
    const out = await py.call("run", { code: `
import json, time
import js
import numpy as np
import polars as pl
_c = js.p9Columns
t0 = time.perf_counter()
frame = pl.DataFrame({n: np.frombuffer(_c.buffers[i].to_py(), dtype="<f8") for i, n in enumerate(_c.names)})
t1 = time.perf_counter()
frame.describe(); t2 = time.perf_counter()
n = frame.filter(pl.col("${column}") > pl.col("${column}").mean()).height; t3 = time.perf_counter()
js.p9Columns = None
json.dumps({"rows": frame.height, "columns": frame.width, "frameMs": (t1 - t0) * 1000,
            "describeMs": (t2 - t1) * 1000, "filterMs": (t3 - t2) * 1000, "filtered": n})` });
    return { storeMs, storeTimings: timings, handMs, ...JSON.parse(out), heap: await py.call("heap"),
             csvParse: "polars read_csv in Pyodide panics on this file (could not spawn threads)" };
}

/** Python's own ask: anything, by code (S1's checks, profiling). */
export async function p9Run(code) {
    return current.call("run", { code });
}

/**
 * The viewer opened on the picked image, with the worker booting beside it
 * (`worker: true`) or not; long tasks on this main thread throughout, and a
 * pan while the worker is (still) booting.
 */
export async function p9ViewerOpen({ worker = false, image = "image.ome.tif", channels, panFrames, lazy = false }) {
    const longTasks = [];
    const observer = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) longTasks.push({ start: e.startTime, ms: e.duration });
    });
    observer.observe({ type: "longtask", buffered: true });
    const booting = worker ? p9Boot({ lazy }) : null;
    const t0 = performance.now();
    const pool = new TiffPool(new URL("dist/tiffWorker.js", location.href).href, 4);
    const { geometry } = await pool.open({ file: pickedFiles([image])[0], decoder: "native" });
    const container = document.getElementById("paneA");
    const viewer = await createViewer({
        container: "paneA",
        geometry: { ...geometry, tileWidth: 1024, tileHeight: 1024 },
        channels, feed: "browser", browserTile: (c, level, x, y) => pool.tile(c, level, x, y),
    });
    const full = await waitFull(viewer.viewer, { timeoutMs: 60000 });
    const openMs = performance.now() - t0;
    const pan = await panFrames(viewer.viewer, { frames: 240 });
    const panEndedBeforeReady = worker ? !(current?.marks?.ready) : null;
    const boot = booting ? await booting : null;
    observer.disconnect();
    viewer.destroy();
    pool.terminate();
    container.replaceChildren();
    return { worker, openMs, full, pan, panEndedBeforeReady, longTasks,
             boot: boot ? { phases: boot.phases, bytes: boot.bytes } : null };
}

export function p9Close() {
    current?.terminate();
    current = null;
}
