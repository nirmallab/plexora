/**
 * PlexoraSelfcheck -- the website's self-check page (/selfcheck, written by
 * static_build.py): how Plexora runs in this browser on this computer,
 * measured in about a minute, with what each number means for the website's
 * limits (client/src/envelope.json, the same numbers its notices use,
 * services/handoffNotice.js).
 *
 * It measures, one after the other:
 *
 * - **browser**: the engine, cores, memory, cross-origin isolation, JSPI,
 *   folder access, storage room and whether the site's storage is kept;
 * - **download**: one file of the Python bundle fetched past the cache, its
 *   bytes as sent (the brotli copy's size, manifest.json) over the time;
 * - **python**: Plexora's Python started in a scratch worker (nothing of the
 *   site's projects or copies mounted: another tab's app is untouched), each
 *   phase of its boot;
 * - **pyramid**: a flat 2-channel uint16 image made here (stripped,
 *   uncompressed) and its pyramid built by the website's own builder
 *   (workers/pyramidWorker.js) into the site's storage, then deleted;
 * - **memory**: how far a WebAssembly memory grows in a worker (Python's
 *   heap cannot outgrow it);
 * - **gpu**: whether WebGPU has an adapter (magic select's speed).
 *
 * Each result carries the website's estimate for this computer made before
 * the measurement (from its cores and memory) beside what was measured, so
 * one file says how good the estimate was. Nothing is sent anywhere: the
 * results are on the page and in the file "Download results" saves.
 *
 *   run()        -> promise of the results ({selfcheck, date, version, steps})
 *   results      the last run's, or null
 *
 * Classic script, `window.PlexoraSelfcheck`; needs PlexoraPython
 * (services/pythonClient.js) on the page.
 */
(function (global) {
    "use strict";

    const BASE = (() => {
        try {
            return new URL(global.document.baseURI).pathname.replace(/\/+$/, "");
        } catch (error) {
            return "";
        }
    })();
    const at = (path) => `${BASE}${path}`;
    const PYRAMID_WORKER = at("/client/src/js/workers/pyramidWorker.js");
    const HANDS = at("/client/dist/tiffWorker_bundle.js");
    const MEMORY_WORKER = at("/client/src/js/workers/selfcheckMemory.js");
    const WORKERS = Math.max(1, Math.min(12, (global.navigator.hardwareConcurrency || 4) - 2));
    const GB = 1e9;
    //: The run so far, kept as each step starts and ends: a browser that
    //: closes the page mid-step (out of memory: Chrome's "Error code: 5")
    //: leaves the steps before it and the one it died in.
    const PROGRESS = "plexora-selfcheck";
    //: A pyramid build that has not finished a tile for this long has stopped.
    const STALLED_MS = 60000;
    const LABELS = { browser: "Browser", storage: "Storage", gpu: "GPU", memory: "Memory",
                     download: "Download", python: "Python", pyramid: "Pyramid" };

    let results = null;

    function keep(out) {
        try {
            global.localStorage.setItem(PROGRESS, JSON.stringify(out));
        } catch (error) { /* not kept: a crash leaves no trace */ }
    }

    function kept() {
        try {
            return JSON.parse(global.localStorage.getItem(PROGRESS) || "null");
        } catch (error) {
            return null;
        }
    }

    const round = (value, digits = 1) => Math.round(value * 10 ** digits) / 10 ** digits;
    const gb = (bytes) => (bytes >= 10 * GB ? `${Math.round(bytes / GB)} GB` : `${round(bytes / GB)} GB`);
    const seconds = (ms) => (ms >= 10000 ? `${Math.round(ms / 1000)} s` : `${round(ms / 1000)} s`);

    async function json(url, init) {
        const answer = await global.fetch(url, init);
        if (!answer.ok) throw new Error(`${url}: ${answer.status}`);
        return answer.json();
    }

    // -- browser ------------------------------------------------------------------

    function engine() {
        const ua = global.navigator.userAgent || "";
        const brands = (global.navigator.userAgentData && global.navigator.userAgentData.brands) || [];
        const named = brands.find((b) => !/Not.?A.?Brand|Chromium/i.test(b.brand)) || brands[0];
        if (named) return `${named.brand} ${named.version}`;
        const firefox = ua.match(/Firefox\/(\d+)/);
        if (firefox) return `Firefox ${firefox[1]}`;
        const safari = ua.match(/Version\/(\d+(?:\.\d+)?).*Safari/);
        if (safari) return `Safari ${safari[1]}`;
        return ua.slice(0, 80);
    }

    async function browser() {
        const nav = global.navigator;
        const detail = {
            engine: engine(),
            platform: (nav.userAgentData && nav.userAgentData.platform) || nav.platform || null,
            user_agent: nav.userAgent,
            cores: nav.hardwareConcurrency || null,
            memory_gb: nav.deviceMemory || null,
            isolated: Boolean(global.crossOriginIsolated),
            shared_memory: typeof SharedArrayBuffer === "function",
            jspi: typeof WebAssembly !== "undefined" && "Suspending" in WebAssembly,
            folders: typeof global.showDirectoryPicker === "function",
            opfs: Boolean(nav.storage && nav.storage.getDirectory),
            webcodecs: typeof global.VideoEncoder === "function",
        };
        try {
            const { quota, usage } = await nav.storage.estimate();
            Object.assign(detail, { quota_bytes: quota || null, usage_bytes: usage || 0 });
        } catch (error) { /* no estimate here */ }
        try {
            detail.persisted = await nav.storage.persisted();
        } catch (error) {
            detail.persisted = null;
        }
        const parts = [detail.engine, detail.cores ? `${detail.cores} cores` : null,
                       detail.memory_gb ? `${detail.memory_gb >= 8 ? "8+" : detail.memory_gb} GB memory` : null];
        const missing = [["isolated", "isolation"], ["jspi", "JSPI"], ["folders", "folder saving"], ["opfs", "site storage"]]
            .filter(([key]) => !detail[key]).map(([, name]) => name);
        return {
            label: "Browser", value: parts.filter(Boolean).join(" · "), detail,
            here: missing.length ? `Without ${missing.join(", ")}` : "Everything the website uses",
        };
    }

    function storage(found) {
        const d = found.detail;
        if (!d.quota_bytes) return { label: "Storage", value: "Not said by this browser", detail: {}, here: "" };
        const room = d.quota_bytes - d.usage_bytes;
        return {
            label: "Storage", value: `${gb(room)} free for the site`, detail: { room_bytes: room },
            here: d.persisted ? "Kept by the browser" : "The browser may clear it when space runs low",
        };
    }

    // -- download -----------------------------------------------------------------

    async function download(shell, manifest) {
        const py = shell.py.endsWith("/") ? shell.py : `${shell.py}/`;
        const stages = await json(`${py}stages.json`, { cache: "no-store" });
        const name = `wheels/${stages.base.wheels[0]}`;
        const files = manifest.files || {};
        const sent = (files[`${name}.br`] || files[name] || {}).bytes;
        const t0 = performance.now();
        const answer = await global.fetch(`${py}${name}`, { cache: "no-store" });
        const body = await answer.arrayBuffer();
        const ms = performance.now() - t0;
        const bytes = sent || body.byteLength;
        const rate = bytes / (ms / 1000);
        const first = manifest.first_visit_bytes / rate * 1000;
        return {
            label: "Download", value: `${round(rate / 1e6)} MB/s`,
            detail: { file: name, bytes, ms: Math.round(ms), bytes_per_second: Math.round(rate),
                      first_visit_bytes: manifest.first_visit_bytes, first_visit_download_ms: Math.round(first) },
            here: `A first visit downloads ${Math.round(manifest.first_visit_bytes / 1e6)} MB: about ${seconds(first)}`,
        };
    }

    // -- python ---------------------------------------------------------------------

    async function python(shell) {
        if (!global.PlexoraPython) throw new Error("the Python client is not on this page");
        const t0 = Date.now();
        const boot = await global.PlexoraPython.start({ base: shell.py, root: `${BASE}/`, siteBase: BASE, scratch: true });
        if (!boot || boot.error) throw new Error((boot && boot.error) || "Python did not start");
        const p = boot.phases || {};
        const since = (key) => (p[key] && p.start ? Math.round(p[key] - p.start) : null);
        const detail = { core_ms: since("core"), packages_ms: since("packages"), wheels_ms: since("wheels"),
                         ready_ms: since("ready"), page_ms: Date.now() - t0, transport: boot.transport };
        return {
            label: "Python", value: `Ready in ${seconds(detail.page_ms)}`, detail,
            here: boot.transport === "jspi" ? "Tools load as they are first used"
                : "Every tool loads at start (no JSPI here)",
        };
    }

    // -- pyramid --------------------------------------------------------------------

    //: A classic little-endian TIFF of `channels` stripped, uncompressed uint16
    //: pages, as a File made here: a gradient with a little noise, so it
    //: deflates about as a real image does.
    async function flatImage(channels, height, width, rows = 256) {
        const strips = Math.ceil(height / rows);
        const stripBytes = rows * width * 2;
        const ENTRIES = 11;
        const ifdBytes = 2 + ENTRIES * 12 + 4;
        const pageMeta = ifdBytes + 8 * strips;
        const metaBytes = 8 + channels * pageMeta;
        const pageBytes = height * width * 2;
        const meta = new DataView(new ArrayBuffer(metaBytes));
        meta.setUint16(0, 0x4949, true);
        meta.setUint16(2, 42, true);
        meta.setUint32(4, 8, true);
        for (let c = 0; c < channels; c += 1) {
            const ifd = 8 + c * pageMeta;
            const offsets = ifd + ifdBytes;
            const counts = offsets + 4 * strips;
            const data = metaBytes + c * pageBytes;
            let at = ifd;
            meta.setUint16(at, ENTRIES, true);
            at += 2;
            const entry = (tag, type, count, value) => {
                meta.setUint16(at, tag, true);
                meta.setUint16(at + 2, type, true);
                meta.setUint32(at + 4, count, true);
                if (type === 3 && count === 1) meta.setUint16(at + 8, value, true);
                else meta.setUint32(at + 8, value, true);
                at += 12;
            };
            entry(256, 4, 1, width);
            entry(257, 4, 1, height);
            entry(258, 3, 1, 16);
            entry(259, 3, 1, 1);
            entry(262, 3, 1, 1);
            entry(273, 4, strips, offsets);
            entry(277, 3, 1, 1);
            entry(278, 4, 1, rows);
            entry(279, 4, strips, counts);
            entry(284, 3, 1, 1);
            entry(339, 3, 1, 1);
            meta.setUint32(at, c + 1 < channels ? 8 + (c + 1) * pageMeta : 0, true);
            for (let s = 0; s < strips; s += 1) {
                const these = Math.min(rows, height - s * rows);
                meta.setUint32(offsets + 4 * s, data + s * stripBytes, true);
                meta.setUint32(counts + 4 * s, these * width * 2, true);
            }
        }
        const parts = [meta.buffer];
        let seed = 0x9e3779b9;
        for (let c = 0; c < channels; c += 1) {
            for (let s = 0; s < strips; s += 1) {
                const these = Math.min(rows, height - s * rows);
                const strip = new Uint16Array(these * width);
                for (let y = 0; y < these; y += 1) {
                    const row = s * rows + y;
                    const base = y * width;
                    for (let x = 0; x < width; x += 1) {
                        seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
                        strip[base + x] = (((x >> 2) + (row >> 1) + c * 977) & 4095) * 12 + ((seed >>> 0) & 15);
                    }
                }
                parts.push(strip.buffer);
                // Keep the page responsive while it makes the image.
                if (s % 8 === 7) await new Promise((resolve) => global.setTimeout(resolve, 0));
            }
        }
        return new File(parts, "selfcheck.tif", { type: "image/tiff" });
    }

    function factorsFor(height, width, tile) {
        const factors = [1];
        const last = () => factors[factors.length - 1];
        while (Math.max(Math.ceil(height / last()), Math.ceil(width / last())) > tile) factors.push(last() * 2);
        return factors;
    }

    async function removeCopy(key) {
        try {
            const root = await global.navigator.storage.getDirectory();
            const derived = await (await root.getDirectoryHandle("plexora")).getDirectoryHandle("derived");
            await derived.removeEntry(key, { recursive: true });
        } catch (error) { /* never made, or gone */ }
    }

    async function pyramid(limits, found) {
        const [channels, height, width] = [2, 8192, 8192];
        const raw = channels * height * width * 2;
        const image = limits.image || {};
        const perWorker = Number(image.build_bytes_per_second_per_worker) || 29e6;
        const limitS = Number(image.build_limit_s) || 120;
        const estimate = { workers: WORKERS, bytes_per_second: perWorker * WORKERS,
                           flat_limit_bytes: perWorker * WORKERS * limitS };
        const file = await flatImage(channels, height, width);
        const key = `selfcheck-${Date.now().toString(36)}`;
        const tile = 1024;
        const t0 = performance.now();
        const done = await new Promise((resolve, reject) => {
            const worker = new Worker(PYRAMID_WORKER, { type: "module" });
            let watchdog = null;
            const stop = (fn, value) => {
                global.clearTimeout(watchdog);
                worker.terminate();
                fn(value);
            };
            const watch = () => {
                global.clearTimeout(watchdog);
                watchdog = global.setTimeout(() => stop(reject, new Error(
                    `the build stopped: no progress for ${STALLED_MS / 1000} s`)), STALLED_MS);
            };
            watch();
            worker.onmessage = ({ data }) => {
                if (data.progress) return watch();
                if (data.error) stop(reject, new Error(data.error));
                else stop(resolve, data);
            };
            worker.onerror = (event) => stop(reject, new Error(event.message || "the pyramid worker failed"));
            worker.postMessage({ op: "build", file, key, name: "selfcheck.ome.tif", kind: "image",
                                 shape: [channels, height, width], dtype: "uint16",
                                 factors: factorsFor(height, width, tile), tile,
                                 description: "Plexora self-check", hands: HANDS, workers: WORKERS, folder: null });
        }).finally(() => removeCopy(key));
        const ms = performance.now() - t0;
        const rate = raw / (ms / 1000);
        // The reference computer's own run of this check (envelope.json
        // `selfcheck`), against its measured build rate: this computer's
        // rate on a real image, by the same ratio.
        const reference = limits.selfcheck && limits.selfcheck.pyramid_bytes_per_second;
        const scaled = reference ? (perWorker * Number(image.build_workers_max || 12)) * (rate / reference) : null;
        const measuredLimit = (scaled || rate) * limitS;
        return {
            label: "Pyramid", value: `${gb(raw)} in ${seconds(ms)} (${Math.round(rate / 1e6)} MB/s, ${WORKERS} workers)`,
            detail: { shape: [channels, height, width], dtype: "uint16", bytes: raw, ms: Math.round(ms),
                      bytes_per_second: Math.round(rate), written_bytes: done.bytes || null,
                      timings: done.timings || null, estimate,
                      measured: { flat_limit_bytes: Math.round(measuredLimit), scaled_by_reference: Boolean(reference) } },
            here: `Flat images up to about ${gb(measuredLimit)} build within ${Math.round(limitS / 60)} min `
                + `(estimated ${gb(estimate.flat_limit_bytes)})`,
        };
    }

    // -- memory ---------------------------------------------------------------------

    function memory(limits, found, reached = () => {}) {
        return new Promise((resolve, reject) => {
            const worker = new Worker(MEMORY_WORKER);
            const t0 = performance.now();
            worker.onmessage = ({ data }) => {
                if (data.reached !== undefined) return reached(data.reached);
                worker.terminate();
                const table = limits.table || {};
                const values = Number(table.worker_values) || 40e6;
                const reference = Number(table.reference_device_memory_gb) || 8;
                const scale = found.detail.memory_gb ? Math.min(1, found.detail.memory_gb / reference) : 1;
                resolve({
                    label: "Memory", value: `WebAssembly grows to ${gb(data.bytes)}`,
                    detail: { wasm_bytes: data.bytes, stopped: data.error || null, ms: Math.round(performance.now() - t0),
                              table_values_here: Math.round(values * scale) },
                    here: `Tables up to about ${Math.round(values * scale / 1e6)} M values (cells × columns)`,
                });
            };
            worker.onerror = (event) => {
                worker.terminate();
                reject(new Error(event.message || "the memory worker failed"));
            };
            worker.postMessage({});
        });
    }

    // -- gpu ------------------------------------------------------------------------

    async function gpu(limits) {
        const nav = global.navigator;
        let adapter = null;
        try {
            adapter = nav.gpu ? await nav.gpu.requestAdapter() : null;
        } catch (error) {
            adapter = null;
        }
        const info = adapter ? (adapter.info || {}) : {};
        const click = Number((limits.model || {}).wasm_click_s) || 1;
        return {
            label: "GPU", value: adapter ? `WebGPU: ${[info.vendor, info.architecture].filter(Boolean).join(" ") || "yes"}`
                : "No WebGPU here",
            detail: { webgpu: Boolean(adapter), vendor: info.vendor || null, architecture: info.architecture || null,
                      description: info.description || null },
            here: adapter ? "Magic select runs on the GPU" : `Magic select: about ${click < 1.5 ? 1 : Math.round(click)} s a click`,
        };
    }

    // -- the page -------------------------------------------------------------------

    function row(table, step) {
        let tr = table.querySelector(`tr[data-step="${step.key}"]`);
        if (!tr) {
            tr = global.document.createElement("tr");
            tr.dataset.step = step.key;
            for (let i = 0; i < 3; i += 1) tr.appendChild(global.document.createElement("td"));
            table.appendChild(tr);
        }
        const [label, value, here] = tr.children;
        label.textContent = step.label;
        value.textContent = step.value;
        here.textContent = step.here || "";
        tr.className = step.state || "";
    }

    //: A run the browser ended mid-step, as the page shows it after a reload:
    //: the steps it measured, and the one it stopped in.
    function stopped(previous) {
        const key = previous.running;
        const reached = key === "memory" && previous.memory_reached ? ` after ${gb(previous.memory_reached)}` : "";
        return { ...previous, running: null, ok: false, crashed: key,
                 steps: { ...previous.steps, [key]: {
                     key, ok: false, label: LABELS[key] || key, value: "Stopped",
                     here: `The browser closed the page during this step${reached}`,
                     detail: { crashed: true, memory_reached: previous.memory_reached || null } } } };
    }

    function show(table, out) {
        if (!table) return;
        for (const step of Object.values(out.steps || {})) row(table, { ...step, state: step.ok ? "done" : "failed" });
    }

    /** Every step, or -- `resume`, a run the browser ended -- the steps it did
     *  not reach, the one it stopped in left as stopped. */
    async function run({ resume = null } = {}) {
        const table = global.document.getElementById("selfcheck-results");
        const shell = await json(at("/client/shell.json"), { cache: "no-store" });
        const limits = await json(at("/client/src/envelope.json"), { cache: "no-store" });
        const py = shell.py.endsWith("/") ? shell.py : `${shell.py}/`;
        const manifest = await json(`${py}manifest.json`, { cache: "no-store" });
        const out = resume ? { ...resume, resumed: (resume.resumed || 0) + 1 }
            : { selfcheck: 1, date: new Date().toISOString(), version: shell.version,
                python_bundle: manifest.plexora || null, envelope: limits.version || null, steps: {} };
        results = out;
        show(table, out);
        let found = out.steps.browser && out.steps.browser.ok ? out.steps.browser : null;
        // The cheap steps first, then memory before Python's heap is there,
        // then what a first visit does.
        const plan = [
            ["browser", () => browser()],
            ["storage", () => storage(found || { detail: {} })],
            ["gpu", () => gpu(limits)],
            ["memory", () => memory(limits, found || { detail: {} }, (bytes) => {
                out.memory_reached = bytes;
                keep(out);
            })],
            ["download", () => download(shell, manifest)],
            ["python", () => python(shell)],
            ["pyramid", () => pyramid(limits, found || { detail: {} })],
        ];
        for (const [key, measure] of plan) {
            if (out.steps[key]) continue;
            const label = LABELS[key];
            if (table) row(table, { key, label, value: "Measuring\u2026", state: "running" });
            out.running = key;
            keep(out);
            let step;
            try {
                step = { key, ok: true, ...(await measure()) };
            } catch (error) {
                step = { key, ok: false, label, value: "Failed", here: String(error && error.message || error).slice(0, 200),
                         detail: { error: String(error && error.message || error) } };
            }
            if (key === "browser") found = step.ok ? step : null;
            out.steps[key] = step;
            out.running = null;
            keep(out);
            if (table) row(table, { ...step, state: step.ok ? "done" : "failed" });
        }
        delete out.running;
        out.ok = Object.values(out.steps).every((step) => step.ok);
        keep(out);
        return out;
    }

    function save() {
        if (!results) return;
        const blob = new Blob([JSON.stringify(results, null, 1)], { type: "application/json" });
        const link = global.document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `plexora-selfcheck-${results.date.slice(0, 10)}.json`;
        global.document.body.appendChild(link);
        link.click();
        link.remove();
        global.setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    }

    function wire() {
        const start = global.document.getElementById("selfcheck-run");
        const download = global.document.getElementById("selfcheck-save");
        if (!start) return;
        const table = global.document.getElementById("selfcheck-results");
        let resume = null;
        const previous = kept();
        if (previous && previous.selfcheck === 1 && previous.steps) {
            // The last run, as it ended -- or as far as it got, when the
            // browser closed the page: then Run continues after that step.
            results = previous.running ? stopped(previous) : previous;
            show(table, results);
            download.disabled = false;
            global.document.body.dataset.state = results.crashed ? "stopped" : results.ok ? "done" : "failed";
            if (results.crashed) {
                resume = results;
                keep(results);
                start.textContent = "Continue the check";
            }
        }
        start.addEventListener("click", async () => {
            start.disabled = true;
            download.disabled = true;
            global.document.body.dataset.state = "running";
            if (!resume && table) table.textContent = "";
            try {
                await run({ resume });
            } finally {
                resume = null;
                start.textContent = "Run the check";
                start.disabled = false;
                global.document.body.dataset.state = results && results.ok ? "done" : "failed";
                download.disabled = !results;
            }
        });
        download.addEventListener("click", save);
    }

    if (global.document) {
        if (global.document.readyState === "loading") global.document.addEventListener("DOMContentLoaded", wire);
        else wire();
    }

    global.PlexoraSelfcheck = Object.freeze({ run, save, get results() { return results; } });
})(typeof window !== "undefined" ? window : globalThis);
