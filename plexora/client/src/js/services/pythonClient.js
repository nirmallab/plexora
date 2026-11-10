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
 *   `GET /agent/v1/jobs/<id>`, and a panel's `GET /plugins/<gating|qc>/jobs/<id>`,
 *   is answered from the last one while that job is queued or running
 *   (`job(id)`): the worker is busy running it, and the answer is the route's
 *   own (the worker sends the bridge's `state` beside the record).
 * - **Conversations.** The worker posts every event an AI conversation
 *   records; the chat panel's poll (`GET /ai/v1/conversations/<id>/events`)
 *   is answered from them while the turn runs -- the worker is busy with it --
 *   so a streamed answer and each tool call show as they happen.
 * - **Held polls.** Python never holds a request (its worker has one thread),
 *   so a long-poll (`?wait=N`) that comes back empty (`held: false`) is held
 *   here until the worker says something changed or N seconds pass, then
 *   asked once more. A wakeup may name what changed (`topic`): a
 *   conversation's poll wakes only for that conversation's events.
 * - **Long tasks' polls.** New Dataset's scan and create each run as one task
 *   (server/models/layer_jobs.py `mirror`), which posts its status route's
 *   answer as it goes; that route is answered from the last one while the
 *   task runs, and its cancel route is passed on as a cancel (below).
 * - **Cancels.** A job's cancel is written into shared memory the worker reads
 *   at the job's next check (`cancel(id)`); the site is cross-origin isolated
 *   for exactly this, and a panel's Cancel, or `job_cancel`, while the job
 *   runs is answered here. Without shared memory it is a request like any
 *   other, answered once the job has ended its turn.
 *
 *   warm({base, root})      the worker started and its downloads begun, before start()
 *                           is ready to be called (services/shell.js)
 *   start({base, origin, store, telemetry, scratch})  -> promise of the boot's record ({phases, info, ...});
 *                           `store` a granted folder to keep the projects in
 *                           (`scratch`: none, data in memory -- services/selfcheck.js);
 *                           `telemetry` {endpoint, dnt, context} when the site sends
 *                           the desktop's anonymous usage counts (plexora/browser/
 *                           telemetry.py), whose turn is also taken when the tab hides
 *   telemetry({hidden})     telemetry's turn now
 *   ready                   a promise of the same, for whoever asks later
 *   fetch(url, init)        -> promise of a Response, as from the server
 *   mount(dir, files)       picked File objects under `dir` in Python's files
 *   mountTree(dir, blobs)   a picked folder: [{name: "<folder>/<path>", data: File}]
 *   answers(kind, fn)       who answers Python's questions of `kind` (the page's
 *                           table, window and pyramid readers)
 *   licence(files)          the page's licence written into Python's /bioc
 *   flush()                 the project store written back now
 *   useStore(folder)        the project store moved to a granted folder
 *   job(id) / cancel(id) / on(kind, fn)
 *   unsupported(nav?)       {browser, version} when this browser cannot run the
 *                           website's Python, else null (start() is not to be called)
 *   saveWith(fn)            who saves a picked file Python wrote (services/
 *                           sourceWrites.js): `fn({source, file})` -> a promise;
 *                           a request's answer waits for the saves it started
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
    //: Long tasks' answers (layer_jobs `mirror`): key -> {running, get, answer, cancel, cancelled}.
    const mirrors = new Map();
    //: Held polls waiting for a wakeup: {topic, wake}.
    let wakeups = [];
    //: AI conversations' events, as the worker recorded them: id -> {base,
    //: events, running}. Every event recorded since this worker started is
    //: here, so the events after `base` are all of them.
    const chats = new Map();
    const CHAT_KEEP = 4000;
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });
    const shared = typeof SharedArrayBuffer === "function" && global.crossOriginIsolated;
    //: None with a tab's worker (`spawn`): a SharedWorker shares no memory.
    let cancels = shared ? new SharedArrayBuffer(CANCEL_BYTES) : null;
    //: Requests posted and not yet received by the worker, counted where the
    //: worker can read them while Python runs: a long task lets them in at its
    //: next checkpoint (a message alone cannot say it is waiting until the
    //: worker's event loop delivers it, which a busy loop starves).
    let inFlight = shared ? new Int32Array(new SharedArrayBuffer(4)) : null;
    const cancelled = [];
    //: Saves of picked files Python wrote (plexora/server/utils/source_writes.py),
    //: running: a request's answer waits for the ones its request started,
    //: whose events the worker posted before the answer.
    const saving = new Set();
    let saver = null;

    //: What a page waits on Python for: its requests (and their streamed
    //: bodies) and the browser node's spec of a project. Not the page's
    //: answers to Python's own questions, nor the bookkeeping ops.
    const FOREGROUND = new Set(["request", "read", "nodeSpec"]);
    //: Of those, when each still unanswered was sent (`busy`).
    const foreground = new Set();

    //: `signal`: an AbortSignal whose abort takes the message out of the
    //: worker's queue if Python has not begun it (one Python has begun runs
    //: to its end: nothing interrupts it), and rejects with an AbortError.
    function call(op, args, transfer, signal = null) {
        if (!FOREGROUND.has(op)) return post(op, args, transfer, signal);
        const sent = { at: performance.now() };
        foreground.add(sent);
        const ended = () => foreground.delete(sent);
        const answered = post(op, args, transfer, signal);
        answered.then(ended, ended);
        return answered;
    }

    function post(op, args, transfer, signal) {
        const id = nextId++;
        return new Promise((resolve, reject) => {
            if (signal && signal.aborted) {
                reject(new DOMException("The request was aborted", "AbortError"));
                return;
            }
            waiting.set(id, { resolve, reject });
            if (inFlight) Atomics.add(inFlight, 0, 1);
            worker.postMessage({ id, op, args }, transfer || []);
            if (signal) {
                signal.addEventListener("abort", () => {
                    if (!waiting.has(id)) return;
                    // Counted like every message the worker takes in.
                    if (inFlight) Atomics.add(inFlight, 0, 1);
                    worker.postMessage({ op: "cancel", args: { id } });
                }, { once: true });
            }
        });
    }

    //: Python's questions to the page, answered by whoever registered for
    //: their kind (`answers`); null and why when nobody can.
    const answerers = new Map();
    async function respond({ id, ask, args, sync }) {
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
        if (sync) await answerSync(value, failed);
        else await call("answer", { id, value, failed }, transfer);
    }

    // -- answers through shared memory (a browser without JSPI) ---------------
    //
    // There the worker blocks in Atomics.wait for the answer
    // (workers/pythonWorker.js `syncAsk`), and it is written into the memory
    // it shares with this page, a chunk at a time: a header (JSON, with each
    // typed array and file by reference) then the arrays' bytes, each at an
    // 8-byte boundary. A file is named by a blob: URL, read by the worker.
    let syncChannel = null;
    const S = { IDLE: 0, WAITING: 1, CHUNK: 2, LAST: 3, FAILED: 4 };
    const align8 = (n) => (n + 7) & ~7;

    function encodeAnswer(value) {
        const bins = [];
        let offset = 0;
        const arrays = [];
        const header = JSON.stringify({ value }, (key, item) => {
            if (ArrayBuffer.isView(item)) {
                bins.push({ type: item.constructor.name, offset, length: item.length });
                arrays.push({ at: offset, bytes: new Uint8Array(item.buffer, item.byteOffset, item.byteLength) });
                offset += align8(item.byteLength);
                return { "\u0000bin": bins.length - 1 };
            }
            if (typeof Blob !== "undefined" && item instanceof Blob) {
                // Let go a minute later: the worker reads it at once.
                const url = URL.createObjectURL(item);
                setTimeout(() => URL.revokeObjectURL(url), 60000);
                return { "\u0000blob": url, name: item.name || "", size: item.size };
            }
            return item;
        });
        const head = new TextEncoder().encode(JSON.stringify({ ...JSON.parse(header), bins }));
        const start = align8(4 + head.length);
        const out = new Uint8Array(start + offset);
        new DataView(out.buffer).setUint32(0, head.length, true);
        out.set(head, 4);
        for (const { at, bytes } of arrays) out.set(bytes, start + at);
        return out;
    }

    //: Until the worker has taken the chunk (state no longer `value`).
    async function taken(ctl, value) {
        while (Atomics.load(ctl, 0) === value) {
            if (typeof Atomics.waitAsync === "function") {
                const waited = Atomics.waitAsync(ctl, 0, value, 1000);
                if (waited.async) await waited.value;
            } else {
                await new Promise((resolve) => setTimeout(resolve, 1));
            }
        }
    }

    async function answerSync(value, failed) {
        const { ctl, data } = syncChannel;
        let bytes;
        try {
            bytes = failed ? null : encodeAnswer(value);
        } catch (error) {
            failed = `the answer could not be sent: ${error && error.message || error}`;
        }
        if (failed) {
            const why = new TextEncoder().encode(failed).subarray(0, data.length);
            data.set(why);
            Atomics.store(ctl, 1, why.length);
            Atomics.store(ctl, 0, S.FAILED);
            Atomics.notify(ctl, 0);
            return;
        }
        Atomics.store(ctl, 2, bytes.length);
        for (let at = 0; ;) {
            const n = Math.min(data.length, bytes.length - at);
            data.set(bytes.subarray(at, at + n));
            at += n;
            const last = at >= bytes.length;
            Atomics.store(ctl, 1, n);
            Atomics.store(ctl, 0, last ? S.LAST : S.CHUNK);
            Atomics.notify(ctl, 0);
            if (last) return;
            await taken(ctl, S.CHUNK);
        }
    }

    function save(event) {
        if (!saver) return;
        const done = Promise.resolve().then(() => saver(event))
            .catch((error) => console.error("[python] saving", event.source, error));
        saving.add(done);
        done.finally(() => saving.delete(done));
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
            else pending.reject(data.aborted ? new DOMException(data.error, "AbortError") : new Error(data.error));
            return;
        }
        if (data.event) {
            const event = data.event;
            if (event.kind === "job" && event.job) jobs.set(event.job.job_id, event);
            if (event.kind === "mirror" && event.key) {
                if (event.running) mirrors.set(event.key, event);
                else mirrors.delete(event.key);
            }
            if (event.kind === "task") {
                tasks.push({ name: event.name, at: Math.round(performance.now()) });
                if (tasks.length > 50) tasks.shift();
            }
            if (event.kind === "step") {
                steps.push({ ms: event.ms, names: event.names, at: Math.round(performance.now()) });
                if (steps.length > RECENT) steps.shift();
            }
            if (event.kind === "ask") respond(event);
            if (event.kind === "chat" && event.conversation && event.event) remember(event);
            if (event.kind === "wakeup") wake(event.topic || null);
            if (event.kind === "sourceWrite") save(event);
            emit(event.kind, event);
            return;
        }
        if (data.attached) attached(data.attached);
        if (data.syncChannel) syncChannel = data.syncChannel;
        if (data.mark) emit("mark", data);
        if (data.log) console.warn("[python]", data.log);
    }

    //: The site's base path ("" at a site's root; see services/shell.js):
    //: the app's routes are under it on the page's side, at "/" in Python.
    let siteBase = "";

    // -- one Python per tab, kept across a reload (Chrome, Edge) ------------------
    //
    // The worker used to live and die with the page, so a reload of the site
    // started Python again. Where the browser keeps a SharedWorker a while
    // after its last page (`extendedLifetime`), the tab's Python is one, named
    // by an id the tab keeps (sessionStorage): the page a reload brings
    // connects to the Python the last one left, its projects loaded, and the
    // worker closes itself once no page of the tab comes back
    // (workers/pythonWorker.js `watchPage`). Every tab has its own -- one
    // thread and one heap each, as before -- and holds its id's lock while it
    // lives, so a duplicated tab (which copies sessionStorage) finds it held
    // and takes a new id. A SharedWorker shares no memory with its page (it is
    // not cross-origin isolated): Python reaches the page by JSPI, which every
    // browser keeping one has, and a job is stopped by message.
    const TAB_KEY = "plexora-python-tab";
    //: How long a page waits for its tab's id to be free: the page before it
    //: (a reload) letting go, or else it is a copy of a live tab.
    const TAB_WAIT_MS = 1500;
    //: A reloaded page whose Python says nothing for this long (stuck in
    //: Python, or gone) starts the tab's Python again.
    const ATTACH_WAIT_MS = 15000;
    let mode = null;
    let attachInfo = null;
    let attachTimer = null;

    function keepsAcrossReload() {
        if (typeof SharedWorker !== "function" || !global.navigator || !global.navigator.locks
            || !global.sessionStorage || typeof WebAssembly !== "object"
            || typeof WebAssembly.Suspending !== "function") return false;
        // Whether the browser reads the option at all: a dictionary member it
        // does not know is never read. Asked of a worker that closes itself at
        // once (an empty one lived as long as its page).
        let kept = false;
        try {
            const url = URL.createObjectURL(new Blob(["self.close();"], { type: "text/javascript" }));
            const probe = new SharedWorker(url, {
                name: `plexora-probe-${Math.random().toString(36).slice(2)}`,
                get extendedLifetime() { kept = true; return false; },
            });
            probe.port.close();
            URL.revokeObjectURL(url);
        } catch (error) {
            return false;
        }
        return kept;
    }

    //: Hold the lock of tab id `id` while this page lives; false if it is
    //: not free within `ms`.
    function holdTab(id, ms) {
        return new Promise((resolve) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), ms);
            global.navigator.locks.request(`plexora-python:${id}`, { signal: controller.signal }, () => {
                clearTimeout(timer);
                resolve(true);
                return new Promise(() => {});
            }).catch(() => resolve(false));
        });
    }

    //: This tab's worker name, its lock held: {name, known} (`known`: an id
    //: an earlier page of the tab used, whose Python may be running), or null
    //: if no lock could be had.
    async function tabName() {
        let id = null;
        try { id = global.sessionStorage.getItem(TAB_KEY); } catch (error) { id = null; }
        if (id && await holdTab(id, TAB_WAIT_MS)) return { name: `plexora-python:${id}`, known: true };
        id = (global.crypto && typeof global.crypto.randomUUID === "function"
            ? global.crypto.randomUUID() : Math.random().toString(36).slice(2)).replace(/-/g, "").slice(0, 16);
        try { global.sessionStorage.setItem(TAB_KEY, id); } catch (error) { /* this page only */ }
        return await holdTab(id, TAB_WAIT_MS) ? { name: `plexora-python:${id}`, known: false } : null;
    }

    function spawn(root, { tab = false } = {}) {
        if (worker) return;
        const url = new URL(root + WORKER, global.location.href);
        if (!(tab && keepsAcrossReload())) {
            mode = "page";
            worker = new Worker(url, { type: "module" });
            worker.onmessage = onMessage;
            worker.onerror = (event) => console.error("[python] worker failed:", event.message);
            return;
        }
        mode = "tab";
        cancels = null;
        inFlight = null;
        // What is posted before the tab's worker is connected waits here.
        const outbox = [];
        let target = null;
        worker = { postMessage: (message, transfer) => (target ? target.postMessage(message, transfer || [])
            : outbox.push([message, transfer || []])) };
        tabName().then((tabbed) => {
            if (tabbed) {
                const tabWorker = new SharedWorker(url, { type: "module", name: tabbed.name, extendedLifetime: true });
                tabWorker.onerror = (event) => console.error("[python] worker failed:", event.message);
                target = tabWorker.port;
                target.onmessage = onMessage;
                target.onmessageerror = () => console.error("[python] a message from Python could not be read");
                target.start();
                // A Python an earlier page of the tab left that says nothing
                // (stuck, or gone): a new one, under a new id. Never for a new
                // id, whose worker may still be downloading on a slow link.
                if (tabbed.known) {
                    attachTimer = setTimeout(() => {
                        if (!attachInfo) restart("its Python did not answer this page");
                    }, ATTACH_WAIT_MS);
                }
            } else {
                mode = "page";
                target = new Worker(url, { type: "module" });
                target.onmessage = onMessage;
                target.onerror = (event) => console.error("[python] worker failed:", event.message);
            }
            for (const [message, transfer] of outbox.splice(0)) target.postMessage(message, transfer);
        });
    }

    //: The tab's worker connected this page: whether its Python was running.
    function attached(info) {
        attachInfo = info;
        clearTimeout(attachTimer);
        if (info.fatal) restart("Python had failed in this tab");
    }

    //: A new Python for this tab: the one running stops (its projects
    //: written back first) and the page loads again with an id of its own.
    //: Help › Restart Python, and a tab whose Python failed or is stuck.
    function restart(why = "") {
        if (why) console.warn(`[python] starting this tab's Python again: ${why}`);
        try { worker && worker.postMessage({ op: "quit" }); } catch (error) { /* gone */ }
        try { global.sessionStorage.removeItem(TAB_KEY); } catch (error) { /* this page only */ }
        // A moment for the store to be written back before this page goes.
        setTimeout(() => global.location.reload(), mode === "tab" ? 300 : 0);
    }

    //: The worker started, and Pyodide, the base stage and Plexora's wheels
    //: loading (pythonWorker.js `warm`), while the page gets on with what
    //: start() needs. start() with the same `base` then picks up from there.
    function warm({ base, root = "/", tab = false } = {}) {
        spawn(root, { tab });
        worker.postMessage({ op: "warm", args: { base } });
    }

    let booted = false;

    function start({ base, origin = global.location.origin, root = "/", siteBase: under = "",
                     debug = false, store = null, telemetry = null, scratch = false, tab = false } = {}) {
        if (booted) return ready;
        booted = true;
        siteBase = String(under || "").replace(/\/+$/, "");
        spawn(root, { tab: tab && !scratch });
        call("boot", { base, origin, siteBase, cancels, inFlight, debug, store, telemetry, scratch }).then((info) => {
            // The tab's Python had failed for good: a new one (the page reloads).
            if (info && info.dead) {
                restart("Python had failed in this tab");
                return;
            }
            resolveReady(info);
        }, (error) => {
            resolveReady({ error: String(error?.message || error) });
        });
        if (telemetry && telemetry.endpoint && global.document) {
            // Hidden may be closed: what was counted is queued (and kept) now.
            global.document.addEventListener("visibilitychange", () => {
                if (global.document.visibilityState === "hidden") {
                    ready.then(() => call("telemetry", { hidden: true })).catch(() => null);
                }
            });
        }
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
        // An aborted request (a Quick Edit read overtaken by the next, a page
        // navigated away from) is dropped from Python's queue if it is still
        // waiting there: on the website every request waits its turn on one
        // thread, and a stale one would hold up the fresh ones.
        const signal = (init && init.signal) || (request && request.signal) || null;
        const answer = await call("request", {
            method, path: route(url.pathname), query: url.search.replace(/^\?/, ""), headers,
            body: body ? body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) : null,
        }, undefined, signal);
        // The picked files this request wrote, saved before its answer is.
        if (saving.size) await Promise.all([...saving]);
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
        // Python's answer to a project's first screen, kept for the next
        // reload of the site to show before Python is up again
        // (services/keptAnswers.js); a write has what it changed asked again.
        if (global.PlexoraKept) {
            try {
                global.PlexoraKept.observe(url.href, method, status, answered, payload, headers);
            } catch (error) { /* nothing kept: the next reload waits for Python */ }
        }
        const nullBody = status === 204 || status === 304 || method === "HEAD";
        return new Response(nullBody ? null : payload, { status, headers: answered });
    }

    //: The route Python answers for a path of the site: the site's base
    //: path taken off (a request relayed from a page under it carries it).
    function route(path) {
        if (siteBase && (path === siteBase || path.startsWith(siteBase + "/"))) {
            return path.slice(siteBase.length) || "/";
        }
        return path;
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

    //: A held poll: resolves when a wakeup names `topic` (or names nothing,
    //: for a poll with no topic), or after `seconds`.
    function hold(topic, seconds) {
        return new Promise((resolve) => {
            const entry = { topic, wake: null };
            const timer = setTimeout(() => {
                wakeups = wakeups.filter((other) => other !== entry);
                resolve();
            }, Math.min(seconds * 1000, MAX_HOLD_MS));
            entry.wake = () => { clearTimeout(timer); resolve(); };
            wakeups.push(entry);
        });
    }

    function wake(topic) {
        const woken = wakeups.filter((entry) => entry.topic === topic);
        wakeups = wakeups.filter((entry) => entry.topic !== topic);
        for (const entry of woken) entry.wake();
    }

    //: What a poll of `url` waits for: a conversation's events, or (no topic)
    //: whatever the worker's untargeted wakeup announces.
    function topicOf(url) {
        const chat = url.pathname.match(/\/ai\/v1\/conversations\/([^/]+)\/events$/);
        return chat ? `chat:${decodeURIComponent(chat[1])}` : null;
    }

    function remember({ conversation, event }) {
        let chat = chats.get(conversation);
        if (!chat) {
            chat = { base: Math.max(0, Number(event.seq) - 1), events: [], running: false };
            chats.set(conversation, chat);
        }
        chat.events.push(event);
        if (chat.events.length > CHAT_KEEP) {
            const dropped = chat.events.splice(0, chat.events.length - CHAT_KEEP);
            chat.base = dropped[dropped.length - 1].seq;
        }
        if (event.event === "turn_started") chat.running = true;
        if (event.event === "turn_finished") chat.running = false;
        wake(`chat:${conversation}`);
    }

    //: A conversation's poll, answered from the events the worker sent while
    //: its turn runs (null: Python answers). Held here, as Python would hold
    //: it, until an event arrives or the wait ends.
    async function chatEvents(id, url) {
        const chat = chats.get(id);
        const after = Number(url.searchParams.get("after") || 0);
        if (!chat || !(after >= chat.base)) return null;
        let found = chat.events.filter((event) => event.seq > after);
        const wait = Number(url.searchParams.get("wait") || 0);
        if (!chat.running && !found.length) return null;
        if (!found.length && wait > 0) {
            await hold(`chat:${id}`, wait);
            if (!(after >= chat.base)) return null;
            found = chat.events.filter((event) => event.seq > after);
        }
        return jsonResponse({ success: true, ok: true, conversation_id: id, events: found,
                              after: found.length ? found[found.length - 1].seq : after,
                              running: chat.running, answered_by: "page" });
    }

    //: The panels' own job routes (server/routes.py of each): the fields they answer with.
    const PANEL_JOB = ["job_id", "status", "progress", "project", "capability", "error", "result"];

    async function jsonBody(init, request) {
        try {
            if (init && typeof init.body === "string") return JSON.parse(init.body);
            if (request) return await request.clone().json();
        } catch (error) { /* not JSON: Python says so */ }
        return null;
    }

    //: What the page answers itself while the worker runs a job, or null.
    async function whileBusy(url, method, init, request) {
        const path = url.pathname;
        const mirrored = mirrorFor(path, method);
        if (mirrored && method === "GET") {
            return jsonResponse({ ...mirrored.answer, answered_by: "page" });
        }
        if (mirrored && cancels && cancel(mirrored.key)) {
            return jsonResponse({ ...(mirrored.cancelled || { cancelled: true }), answered_by: "page" });
        }
        if (method === "GET") {
            const agentJob = path.match(/\/agent\/v1\/jobs\/([^/]+)$/);
            if (agentJob && busyWithJob(agentJob[1])) {
                return jsonResponse({ success: true, job: jobs.get(agentJob[1]).state, answered_by: "page" });
            }
            const panelJob = path.match(/\/plugins\/(gating|qc)\/jobs\/([^/]+)$/);
            if (panelJob && busyWithJob(panelJob[2], `${panelJob[1]}.`)) {
                const record = jobs.get(panelJob[2]).job;
                return jsonResponse({ ok: true, job: Object.fromEntries(PANEL_JOB.map((k) => [k, record[k] ?? null])),
                                      answered_by: "page" });
            }
            return null;
        }
        if (method !== "POST" || !cancels) return null;
        const panelCancel = path.match(/\/plugins\/(gating|qc)\/jobs\/([^/]+)\/cancel$/);
        let id = panelCancel && busyWithJob(panelCancel[2], `${panelCancel[1]}.`) ? panelCancel[2] : null;
        let shape = "panel";
        if (!id && /\/agent\/v1\/capabilities\/job_cancel$/.test(path)) {
            const body = await jsonBody(init, request);
            const asked = body && body.arguments && body.arguments.job_id;
            if (typeof asked === "string" && busyWithJob(asked)) {
                id = asked;
                shape = "agent";
            }
        }
        if (!id || !cancel(id)) return null;
        const answer = { job_id: id, status: "cancelling",
                         note: "the job stops at its next checkpoint; job_wait reports when it has" };
        return jsonResponse(shape === "panel" ? { ok: true, ...answer } : { success: true, result: answer });
    }

    //: Python's answers marked `Cache-Control: immutable` (a figure panel's
    //: preview at its render revision), kept for the tab's life. A response
    //: the site's service worker makes never enters the browser's HTTP cache,
    //: so without this every picture drawn again asked Python again, one at a
    //: time behind everything else. The least recently used go first past
    //: IMMUTABLE_BYTES.
    const IMMUTABLE_BYTES = 96 << 20;
    const immutable = new Map();
    let immutableBytes = 0;

    function keptImmutable(href) {
        const hit = immutable.get(href);
        if (!hit) return null;
        immutable.delete(href);
        immutable.set(href, hit);
        return new Response(hit.body, { status: hit.status, headers: hit.headers });
    }

    async function keepImmutable(href, response) {
        if (!response.ok || immutable.has(href)
            || !/\bimmutable\b/i.test(response.headers.get("Cache-Control") || "")) return;
        const body = await response.clone().arrayBuffer();
        if (body.byteLength > IMMUTABLE_BYTES / 8) return;
        immutable.set(href, { status: response.status, headers: [...response.headers.entries()], body });
        immutableBytes += body.byteLength;
        for (const [key, value] of immutable) {
            if (immutableBytes <= IMMUTABLE_BYTES) break;
            immutable.delete(key);
            immutableBytes -= value.body.byteLength;
        }
    }

    async function fetch(input, init) {
        await ready;
        const request = isRequest(input) ? input : null;
        const url = new URL(request ? request.url : String(input), global.location.href);
        const method = ((init && init.method) || (request && request.method) || "GET").toUpperCase();
        if (method === "GET") {
            const kept = keptImmutable(url.href);
            if (kept) return kept;
        }
        const mine = await whileBusy(url, method, init, request);
        if (mine) return mine;
        const chat = method === "GET" && url.pathname.match(/\/ai\/v1\/conversations\/([^/]+)\/events$/);
        if (chat) {
            const answered = await chatEvents(decodeURIComponent(chat[1]), url);
            if (answered) return answered;
        }
        const wait = Number(url.searchParams.get("wait") || 0);
        const response = await send(url, init, request);
        if (method === "GET") await keepImmutable(url.href, response).catch(() => null);
        if (!(wait > 0) || !response.ok) return response;
        const answer = await response.clone().json().catch(() => null);
        if (!answer || answer.held !== false || (answer.commands || []).length
            || (answer.events || []).length) {
            return response;
        }
        // Held here: until the worker says something changed, or the wait ends.
        await hold(topicOf(url), wait);
        if (chat) {
            const answered = await chatEvents(decodeURIComponent(chat[1]), url);
            if (answered) return answered;
        }
        url.searchParams.set("wait", "0");
        return send(url, init, request);
    }

    //: The running task whose status route (GET) or cancel route (POST) `path`
    //: is, or null. The page's paths may carry the site's base before the route.
    function mirrorFor(path, method) {
        for (const event of mirrors.values()) {
            const route = method === "GET" ? event.get : method === "POST" ? event.cancel : null;
            if (route && (path === route || path.endsWith(route))) return event;
        }
        return null;
    }

    //: Whether the worker is (as far as the page knows) running job `id`, one
    //: of capability `prefix`'s when given.
    function busyWithJob(id, prefix = "") {
        const event = jobs.get(id);
        return Boolean(event && (event.job.status === "running" || event.job.status === "queued")
                       && String(event.job.capability || "").startsWith(prefix));
    }

    function jsonResponse(value, status = 200) {
        return new Response(JSON.stringify(value), {
            status, headers: { "Content-Type": "application/json" },
        });
    }

    function cancel(id) {
        if (!cancels && mode === "tab") {
            // No shared memory with a tab's worker: a message, which Python
            // reads at its next yield.
            if (!cancelled.includes(id)) cancelled.push(id);
            worker.postMessage({ op: "cancelJob", args: { id } });
            return true;
        }
        if (!cancels) return false;
        if (!cancelled.includes(id)) cancelled.push(id);
        const bytes = new TextEncoder().encode(cancelled.join("\n"));
        const view = new Uint8Array(cancels);
        view.fill(0);
        view.set(bytes.subarray(0, CANCEL_BYTES));
        return true;
    }

    //: Chrome or Edge older than 137 has no JSPI, and the website's Python
    //: without it crashed Chrome 131 on macOS ("Error code: 5") and never
    //: started on Windows, where every compiled module validates: such a
    //: browser is asked to update instead. Firefox and Safari without JSPI
    //: take the other path (shared memory, every stage at boot), which runs.
    //: {browser, version} for a browser that cannot, else null.
    function unsupported(nav = global.navigator || {}) {
        if (typeof WebAssembly === "object" && typeof WebAssembly.Suspending === "function") return null;
        const brands = (nav.userAgentData && nav.userAgentData.brands) || [];
        const ua = String(nav.userAgent || "");
        const chromium = brands.find((brand) => /^Chromium$/i.test(brand.brand));
        const version = chromium ? Number(chromium.version) : Number((ua.match(/\bChrom(?:e|ium)\/(\d+)/) || [])[1]);
        if (!version) return null;
        const edge = brands.some((brand) => /Edge/i.test(brand.brand)) || /\bEdg\//.test(ua);
        return { browser: edge ? "Edge" : "Chrome", version };
    }

    function on(kind, fn) {
        if (!listeners.has(kind)) listeners.set(kind, new Set());
        listeners.get(kind).add(fn);
        return () => listeners.get(kind).delete(fn);
    }

    // The tab shown again: what another app (or this site in another tab)
    // changed in the project store meanwhile, read in before the user's next
    // click. The worker reads the folder only while requests pause
    // (workers/pythonWorker.js `idlePull`), and not on every request as it
    // did; this is where a return to the tab gets a fresh look.
    let shownAt = 0;
    if (global.document && typeof global.document.addEventListener === "function") {
        global.document.addEventListener("visibilitychange", () => {
            if (global.document.visibilityState !== "visible" || Date.now() - shownAt < 5000) return;
            shownAt = Date.now();
            ready.then(() => call("pull", {})).catch(() => null);
        });
    }

    global.PlexoraPython = Object.freeze({
        warm,
        start,
        ready,
        unsupported,
        fetch,
        mount: (dir, files) => ready.then(() => call("mount", { dir, files })),
        mountTree: (dir, blobs) => ready.then(() => call("mountTree", { dir, blobs })),
        nodeSpec: (name) => ready.then(() => call("nodeSpec", { name })),
        //: The page's licence records written where Python's `biocognia`
        //: reads them (services/licenseClient.js); resolves Python's status.
        licence: (files) => ready.then(() => call("licence", { files })),
        //: Who answers Python's questions of kind `kind`: `fn(args)` -> a
        //: promise of {value, transfer} (services/browserTable.js "columns",
        //: services/browserNode.js "window").
        answers: (kind, fn) => { answerers.set(kind, fn); },
        heap: () => ready.then(() => call("heap", {})),
        telemetry: (args) => ready.then(() => call("telemetry", args || {})),
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
        //: Whether a page has waited on Python more than `afterMs` for
        //: something still unanswered: a long answer (a project's load, a
        //: fit), or requests queued behind one. The page's own background
        //: work (a pyramid build, services/browserPyramid.js) steps back
        //: meanwhile; a quick answer, a status poll's, holds nothing back.
        busy: (afterMs = 50) => {
            const now = performance.now();
            for (const sent of foreground) if (now - sent.at >= afterMs) return true;
            return false;
        },
        recent: () => recent.slice(),
        //: "tab" (one SharedWorker per tab, kept across a reload) or "page"
        //: (a worker of this page's own); whether this page found its tab's
        //: Python running (a reload); Help › Restart Python.
        mode: () => mode,
        reattached: () => Boolean(attachInfo && attachInfo.booted),
        restart,
        steps: () => steps.slice(),
        tasks: () => tasks.slice(),
        cancel,
        on,
        saveWith: (fn) => { saver = fn; },
        //: Every save still running (tests, and a page about to close).
        saved: () => Promise.all([...saving]),
    });
})(typeof window !== "undefined" ? window : globalThis);
