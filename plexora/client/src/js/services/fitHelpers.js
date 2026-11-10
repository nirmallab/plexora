/**
 * The website's helper Pythons for contrast fits (workers/fitWorker.js).
 *
 * The website's Python is one thread: while it fits a channel's mixture (0.2
 * to 2.7 s on LSP11385's channels) every request waits, a click's calibration
 * among them. So the fit route hands the fit here instead
 * (plexora/server/providers/browser.py `page_fit`, asked as "fit"): a helper
 * makes it with mixture_fit as the main Python runs it (its compiled file,
 * sent along) and the same bundle, and the route is
 * asked again until the nine floats are back -- bit for bit what the main
 * Python would have made. No helper ready, a helper failed, a job this page
 * does not know (a reload): Python fits it itself, as it always did.
 *
 * How many: two on a machine with eight cores or more, one with four, none
 * below that or where the browser says it has under 4 GB (each is a Pyodide
 * with numpy, scipy and scikit-learn: about a hundred and fifty megabytes).
 * They load once Python is ready, the second after the first.
 *
 * Python's question, `{op, ...}`:
 *   ready                    -> {ready: whether a helper is ready}
 *   start {values, components, code, code_kind, code_id}
 *                            values {dtype, shape, data}: the float64 values
 *                            to fit; code {data}: mixture_fit, "pyc" or
 *                            "source" (`code_kind`), named by `code_id`;
 *                            -> {id}, or null when no helper is ready
 *   peek {id}                -> {done}: whether `result` would answer now
 *   result {id}              -> {pending} | {params: hex} | {failed} | {gone}
 */
(function (global) {
    "use strict";

    const WORKER = "client/src/js/workers/fitWorker.js";
    //: A result nobody came back for (the viewer moved on) is dropped after
    //: this, and so is a fit still queued.
    const KEPT_MS = 60000;

    //: Helpers this machine runs (see above). `nav`: the navigator, for tests.
    function poolSize(nav = global.navigator || {}) {
        const cores = Number(nav.hardwareConcurrency) || 2;
        const memory = Number(nav.deviceMemory) || 0;     // Chrome's; 0: not said
        if (memory && memory < 4) return 0;
        return cores >= 8 ? 2 : cores >= 4 ? 1 : 0;
    }

    const helpers = [];   // {worker, ready, dead, busy: job id | null}
    const jobs = new Map();   // id -> {state: queued|running|done, values?, source?, params?, error?, at}
    const queue = [];
    let nextId = 1;
    let wanted = 0;
    let url = null;
    const now = () => (global.performance ? global.performance.now() : Date.now());

    function start(bundle, size = poolSize()) {
        if (url || !size || !bundle || !bundle.base) return false;
        const base = new URL(bundle.base, global.location.href).href;
        url = new URL(`${String(bundle.root || "/").replace(/\/?$/, "/")}${WORKER}?base=${encodeURIComponent(base)}`,
                      global.location.href);
        wanted = size;
        spawn();
        return true;
    }

    // One at a time: the next once this one is ready (two loading at once
    // take two cores from whatever the page is doing meanwhile).
    function spawn() {
        if (!url || helpers.length >= wanted) return;
        const helper = { worker: null, ready: false, dead: false, busy: null };
        try {
            helper.worker = new global.Worker(url, { type: "module" });
        } catch (error) {
            wanted = helpers.length;
            return;
        }
        helpers.push(helper);
        helper.worker.onmessage = (event) => onMessage(helper, event.data || {});
        helper.worker.onerror = () => fail(helper, "the helper failed");
        helper.worker.postMessage({ op: "ready" });
    }

    function onMessage(helper, data) {
        if (data.op === "ready") {
            helper.ready = true;
            pump();
            spawn();
        } else if (data.op === "failed") {
            fail(helper, data.error || "the helper did not load");
            spawn();
        } else if (data.op === "done") {
            helper.busy = null;
            const job = jobs.get(data.id);
            if (job) {
                job.state = "done";
                job.params = typeof data.params === "string" ? data.params : null;
                job.error = job.params ? null : (data.error || "no fit");
                job.at = now();
            }
            pump();
        }
    }

    function fail(helper, why) {
        if (helper.dead) return;
        helper.dead = true;
        helper.ready = false;
        try { helper.worker.terminate(); } catch (error) { /* gone already */ }
        const job = helper.busy && jobs.get(helper.busy);
        if (job) Object.assign(job, { state: "done", error: why, at: now() });
        helper.busy = null;
        // A helper that failed is not replaced: one that failed to load will
        // fail again, and Python fits for itself without one.
        wanted = Math.max(0, wanted - 1);
    }

    function expire() {
        const old = now() - KEPT_MS;
        for (const [id, job] of jobs) {
            if (job.state !== "running" && job.at < old) jobs.delete(id);
        }
    }

    function pump() {
        expire();
        for (const helper of helpers) {
            if (!helper.ready || helper.dead || helper.busy) continue;
            let id;
            let job;
            while (queue.length && !job) {
                id = queue.shift();
                job = jobs.get(id);
                if (job && job.state !== "queued") job = null;
            }
            if (!job) return;
            job.state = "running";
            helper.busy = id;
            const { values, components, code, codeKind, codeId } = job;
            delete job.values;
            delete job.code;
            helper.worker.postMessage({ op: "fit", id, values, components, code, codeKind, codeId },
                                      [values.buffer, code.buffer]);
        }
    }

    const ready = () => helpers.some((helper) => helper.ready && !helper.dead);

    function answer(args = {}) {
        const op = args && args.op;
        if (op === "ready") return { value: { ready: ready() } };
        if (op === "start") {
            if (!ready()) return { value: null };
            let values = args.values && args.values.data;
            let code = args.code && args.code.data;
            if (!(values instanceof Uint8Array) || !(code instanceof Uint8Array)
                    || (args.values.dtype && args.values.dtype !== "float64")
                    || !["pyc", "source"].includes(args.code_kind) || typeof args.code_id !== "string") {
                return { value: null };
            }
            // Each its own buffer, moved to the helper.
            const own = (bytes) => (bytes.byteOffset || bytes.byteLength !== bytes.buffer.byteLength
                ? bytes.slice() : bytes);
            values = own(values);
            code = own(code);
            if (code.buffer === values.buffer) code = code.slice();
            const id = `fit-${nextId++}`;
            const components = Math.max(1, Math.min(16, Number(args.components) || 3));
            jobs.set(id, { state: "queued", values, components, code, codeKind: args.code_kind,
                           codeId: args.code_id, at: now() });
            queue.push(id);
            pump();
            return { value: { id } };
        }
        if (op === "peek") {
            const job = jobs.get(args.id);
            return { value: { done: !job || job.state === "done" } };
        }
        if (op === "result") {
            const job = jobs.get(args.id);
            if (!job) return { value: { gone: true } };
            if (job.state !== "done") return { value: { pending: true } };
            jobs.delete(args.id);
            return { value: job.error ? { failed: job.error } : { params: job.params } };
        }
        return { value: null };
    }

    const python = global.PlexoraPython;
    if (python && python.answers) {
        python.answers("fit", answer);
        Promise.resolve(python.ready).then((info) => {
            if (info && !info.error && python.bundle) start(python.bundle());
        }).catch(() => null);
    }

    global.PlexoraFitHelpers = Object.freeze({
        poolSize,
        start,
        answer,
        //: What the pool holds now (tests, the console).
        state: () => ({
            helpers: helpers.map((h) => ({ ready: h.ready, dead: h.dead, busy: h.busy })),
            jobs: jobs.size,
            queued: queue.length,
        }),
    });
})(typeof window !== "undefined" ? window : globalThis);
