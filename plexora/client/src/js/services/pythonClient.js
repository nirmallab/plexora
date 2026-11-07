/**
 * PlexoraPython -- the page's side of the website's Python worker.
 *
 * On the website (`PlexoraBackend.kind === "browser"`) Plexora's own Flask app
 * runs in Pyodide in workers/pythonWorker.js. This starts that worker, and
 * turns a request the fetch facade classed "python" into a message and the
 * answer back into a `Response`, so a caller cannot tell it from a server's.
 * It also keeps three things the worker cannot while Python is busy:
 *
 * - **Job records.** The worker posts every job's record as it is written; a
 *   `GET /agent/v1/jobs/<id>` is answered from the last one when the worker
 *   is running that very job (`job(id)`).
 * - **Held polls.** Python never holds a request (its worker has one thread),
 *   so a long-poll (`?wait=N`) that comes back empty is held here until the
 *   worker says something changed or N seconds pass, then asked once more.
 * - **Cancels.** A job's cancel is written into shared memory the worker reads
 *   at the job's next check (`cancel(id)`); the site is cross-origin isolated
 *   for exactly this. Without shared memory it is a request like any other.
 *
 *   start({base, origin, store})  -> promise of the boot's record ({phases, info, ...});
 *                           `store` a granted folder to keep the projects in
 *   ready                   a promise of the same, for whoever asks later
 *   fetch(url, init)        -> promise of a Response, as from the server
 *   mount(dir, files)       picked File objects under `dir` in Python's files
 *   mountTree(dir, blobs)   a picked folder: [{name: "<folder>/<path>", data: File}]
 *   answers(kind, fn)       who answers Python's questions of `kind` (the page's
 *                           table, window and pyramid readers)
 *   flush()                 the project store written back now
 *   useStore(folder)        the project store moved to a granted folder
 *   job(id) / cancel(id) / on(kind, fn)
 *
 * Classic script, `window.PlexoraPython`.
 */
(function (global) {
    "use strict";

    const WORKER = "client/src/js/workers/pythonWorker.js";
    const CANCEL_BYTES = 4096;
    let worker = null;
    let nextId = 1;
    const waiting = new Map();
    const listeners = new Map();
    const jobs = new Map();
    let wakeups = [];
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });
    const shared = typeof SharedArrayBuffer === "function" && global.crossOriginIsolated;
    const cancels = shared ? new SharedArrayBuffer(CANCEL_BYTES) : null;
    //: Requests posted and not yet received by the worker, counted where the
    //: worker can read them while Python runs: a long task lets them in at its
    //: next checkpoint (a message alone cannot say it is waiting until the
    //: worker's event loop delivers it, which a busy loop starves).
    const inFlight = shared ? new Int32Array(new SharedArrayBuffer(4)) : null;
    const cancelled = [];

    function call(op, args, transfer) {
        const id = nextId++;
        return new Promise((resolve, reject) => {
            waiting.set(id, { resolve, reject });
            if (inFlight) Atomics.add(inFlight, 0, 1);
            worker.postMessage({ id, op, args }, transfer || []);
        });
    }

    //: Python's questions to the page, answered by whoever registered for
    //: their kind (`answers`); null and why when nobody can.
    const answerers = new Map();
    async function respond({ id, ask, args }) {
        let value = null;
        let failed = null;
        let transfer = [];
        try {
            const fn = answerers.get(ask);
            if (!fn) failed = `nothing in this page answers "${ask}"`;
            else ({ value = null, transfer = [] } = (await fn(args)) || {});
        } catch (error) {
            failed = String(error && error.message || error).slice(0, 500);
        }
        await call("answer", { id, value, failed }, transfer);
    }

    function emit(kind, payload) {
        for (const fn of listeners.get(kind) || []) {
            try { fn(payload); } catch (error) { console.error(error); }
        }
    }

    function onMessage({ data }) {
        if (data.id !== undefined) {
            const pending = waiting.get(data.id);
            if (!pending) return;
            waiting.delete(data.id);
            if (data.ok) pending.resolve(data.result);
            else pending.reject(new Error(data.error));
            return;
        }
        if (data.event) {
            const event = data.event;
            if (event.kind === "job" && event.job) jobs.set(event.job.job_id, event);
            if (event.kind === "task") {
                tasks.push({ name: event.name, at: Math.round(performance.now()) });
                if (tasks.length > 50) tasks.shift();
            }
            if (event.kind === "step") {
                steps.push({ ms: event.ms, names: event.names, at: Math.round(performance.now()) });
                if (steps.length > RECENT) steps.shift();
            }
            if (event.kind === "ask") respond(event);
            if (event.kind === "wakeup") {
                const held = wakeups;
                wakeups = [];
                for (const wake of held) wake();
            }
            emit(event.kind, event);
            return;
        }
        if (data.mark) emit("mark", data);
        if (data.log) console.warn("[python]", data.log);
    }

    function start({ base, origin = global.location.origin, root = "/", debug = false, store = null } = {}) {
        if (worker) return ready;
        worker = new Worker(new URL(root + WORKER, global.location.href), { type: "module" });
        worker.onmessage = onMessage;
        worker.onerror = (event) => console.error("[python] worker failed:", event.message);
        call("boot", { base, origin, cancels, inFlight, debug, store }).then(resolveReady, (error) => {
            resolveReady({ error: String(error?.message || error) });
        });
        return ready;
    }

    function headersOf(init, request) {
        const headers = new Headers(request ? request.headers : undefined);
        new Headers(init && init.headers).forEach((value, name) => headers.set(name, value));
        return [...headers.entries()];
    }

    async function bodyOf(init, request) {
        const body = init && init.body !== undefined ? init.body : null;
        if (body === null || body === undefined) {
            return request && !["GET", "HEAD"].includes(request.method)
                ? new Uint8Array(await request.arrayBuffer()) : null;
        }
        if (typeof body === "string") return new TextEncoder().encode(body);
        if (body instanceof ArrayBuffer) return new Uint8Array(body);
        if (ArrayBuffer.isView(body)) return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
        // FormData, Blob, URLSearchParams: as the browser would send them.
        return new Uint8Array(await new Response(body).arrayBuffer());
    }

    function contentTypeOf(init) {
        const body = init && init.body;
        if (body instanceof URLSearchParams) return "application/x-www-form-urlencoded;charset=UTF-8";
        if (typeof FormData !== "undefined" && body instanceof FormData) {
            return null; // set from the encoded body below
        }
        return null;
    }

    //: The last requests Python answered: {path, method, status, ms, queued},
    //: for the performance record and the website's own diagnostics.
    const recent = [];
    const steps = [];
    const tasks = [];
    const RECENT = 400;

    async function send(url, init, request) {
        const sent = performance.now();
        const method = ((init && init.method) || (request && request.method) || "GET").toUpperCase();
        const headers = headersOf(init, request);
        let body = await bodyOf(init, request);
        if (typeof FormData !== "undefined" && init && init.body instanceof FormData) {
            const encoded = new Response(init.body);
            body = new Uint8Array(await encoded.arrayBuffer());
            headers.push(["Content-Type", encoded.headers.get("Content-Type")]);
        } else if (contentTypeOf(init)) {
            headers.push(["Content-Type", contentTypeOf(init)]);
        }
        const answer = await call("request", {
            method, path: url.pathname, query: url.search.replace(/^\?/, ""), headers,
            body: body ? body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) : null,
        });
        let payload = answer.body;
        if (answer.stream !== null && answer.stream !== undefined) {
            const parts = [answer.body];
            for (;;) {
                const part = await call("read", { stream: answer.stream });
                parts.push(part.body);
                if (part.done) break;
            }
            payload = new Uint8Array(await new Blob(parts).arrayBuffer());
        }
        // A body the app compressed (Content-Encoding) is one a browser would
        // have decoded on the way in; a Response made here is not decoded by
        // anyone, so it is inflated here and the header goes.
        let answered = answer.headers;
        const encoding = (answered.find(([name]) => name.toLowerCase() === "content-encoding") || [])[1];
        if (payload && encoding && /^(gzip|deflate)$/i.test(encoding.trim())) {
            payload = await inflate(payload, encoding.trim().toLowerCase());
            answered = answered.filter(([name]) => !/^content-(encoding|length)$/i.test(name));
        }
        const status = answer.status;
        recent.push({ path: url.pathname, method, status, ms: Math.round(performance.now() - sent),
                      pyMs: answer.pyMs ?? null, bytes: payload ? payload.byteLength : 0,
                      at: Math.round(sent) });
        if (recent.length > RECENT) recent.shift();
        const nullBody = status === 204 || status === 304 || method === "HEAD";
        return new Response(nullBody ? null : payload, { status, headers: answered });
    }

    //: Bytes through the browser's own decompressor, written straight in
    //: (a Blob in between costs a copy each way through the blob store).
    async function inflate(bytes, format) {
        const stream = new DecompressionStream(format);
        const writer = stream.writable.getWriter();
        writer.write(bytes).catch(() => {});
        writer.close().catch(() => {});
        return new Uint8Array(await new Response(stream.readable).arrayBuffer());
    }

    //: How long a held poll may be held, whatever it asked for.
    const MAX_HOLD_MS = 30000;

    //: A Request -- from this window or the app's frame, whose Request is
    //: another realm's and fails `instanceof`.
    const isRequest = (value) => value instanceof Request
        || (value !== null && typeof value === "object" && typeof value.url === "string"
            && typeof value.method === "string" && typeof value.arrayBuffer === "function");

    async function fetch(input, init) {
        await ready;
        const request = isRequest(input) ? input : null;
        const url = new URL(request ? request.url : String(input), global.location.href);
        const job = url.pathname.match(/\/agent\/v1\/jobs\/([^/]+)$/);
        if (job && busyWithJob(job[1])) {
            return jsonResponse({ success: true, job: jobs.get(job[1]).state });
        }
        const wait = Number(url.searchParams.get("wait") || 0);
        const response = await send(url, init, request);
        if (!(wait > 0) || !response.ok) return response;
        const answer = await response.clone().json().catch(() => null);
        if (!answer || answer.held !== false || (answer.commands || []).length
            || (answer.events || []).length) {
            return response;
        }
        // Held here: until the worker says something changed, or the wait ends.
        await new Promise((resolve) => {
            const timer = setTimeout(resolve, Math.min(wait * 1000, MAX_HOLD_MS));
            wakeups.push(() => { clearTimeout(timer); resolve(); });
        });
        url.searchParams.set("wait", "0");
        return send(url, init, request);
    }

    //: Whether the worker is (as far as the page knows) running job `id`.
    function busyWithJob(id) {
        const event = jobs.get(id);
        return Boolean(event && (event.job.status === "running" || event.job.status === "queued"));
    }

    function jsonResponse(value, status = 200) {
        return new Response(JSON.stringify(value), {
            status, headers: { "Content-Type": "application/json" },
        });
    }

    function cancel(id) {
        if (!cancels) return false;
        if (!cancelled.includes(id)) cancelled.push(id);
        const bytes = new TextEncoder().encode(cancelled.join("\n"));
        const view = new Uint8Array(cancels);
        view.fill(0);
        view.set(bytes.subarray(0, CANCEL_BYTES));
        return true;
    }

    function on(kind, fn) {
        if (!listeners.has(kind)) listeners.set(kind, new Set());
        listeners.get(kind).add(fn);
        return () => listeners.get(kind).delete(fn);
    }

    global.PlexoraPython = Object.freeze({
        start,
        ready,
        fetch,
        mount: (dir, files) => ready.then(() => call("mount", { dir, files })),
        mountTree: (dir, blobs) => ready.then(() => call("mountTree", { dir, blobs })),
        nodeSpec: (name) => ready.then(() => call("nodeSpec", { name })),
        //: Who answers Python's questions of kind `kind`: `fn(args)` -> a
        //: promise of {value, transfer} (services/browserTable.js "columns",
        //: services/browserNode.js "window").
        answers: (kind, fn) => { answerers.set(kind, fn); },
        heap: () => ready.then(() => call("heap", {})),
        run: (code) => ready.then(() => call("run", { code })),
        sync: () => ready.then(() => call("sync", {})),
        //: The project store written back now; moved to a granted folder
        //: (services/browserStore.js).
        flush: () => ready.then(() => call("flush", {})),
        useStore: (folder) => ready.then(() => call("useStore", { folder })),
        //: What another app changed in the store, read in now (tests; the
        //: worker also does it between requests).
        pull: () => ready.then(() => call("pull", {})),
        trace: () => ready.then(() => call("trace", {})),
        //: The worker's own account (busy, queued, current), and the calls
        //: this page is still waiting on; null if it does not answer in 2 s.
        state: () => Promise.race([
            call("state", {}).then((worker) => ({ worker, waitingOn: waiting.size })),
            new Promise((resolve) => setTimeout(() => resolve(null), 2000)),
        ]),
        job: (id) => (jobs.has(id) ? jobs.get(id).job : null),
        recent: () => recent.slice(),
        steps: () => steps.slice(),
        tasks: () => tasks.slice(),
        cancel,
        on,
    });
})(typeof window !== "undefined" ? window : globalThis);
