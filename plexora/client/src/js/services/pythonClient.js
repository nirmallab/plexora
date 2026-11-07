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
 * - **Cancels.** A job's cancel is written into shared memory the worker reads
 *   at the job's next check (`cancel(id)`); the site is cross-origin isolated
 *   for exactly this, and a panel's Cancel, or `job_cancel`, while the job
 *   runs is answered here. Without shared memory it is a request like any
 *   other, answered once the job has ended its turn.
 *
 *   start({base, origin, store, telemetry})  -> promise of the boot's record ({phases, info, ...});
 *                           `store` a granted folder to keep the projects in;
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
    const cancels = shared ? new SharedArrayBuffer(CANCEL_BYTES) : null;
    //: Requests posted and not yet received by the worker, counted where the
    //: worker can read them while Python runs: a long task lets them in at its
    //: next checkpoint (a message alone cannot say it is waiting until the
    //: worker's event loop delivers it, which a busy loop starves).
    const inFlight = shared ? new Int32Array(new SharedArrayBuffer(4)) : null;
    const cancelled = [];
    //: Saves of picked files Python wrote (plexora/server/utils/source_writes.py),
    //: running: a request's answer waits for the ones its request started,
    //: whose events the worker posted before the answer.
    const saving = new Set();
    let saver = null;

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
            if (event.kind === "chat" && event.conversation && event.event) remember(event);
            if (event.kind === "wakeup") wake(event.topic || null);
            if (event.kind === "sourceWrite") save(event);
            emit(event.kind, event);
            return;
        }
        if (data.syncChannel) syncChannel = data.syncChannel;
        if (data.mark) emit("mark", data);
        if (data.log) console.warn("[python]", data.log);
    }

    //: The site's base path ("" at a site's root; see services/shell.js):
    //: the app's routes are under it on the page's side, at "/" in Python.
    let siteBase = "";

    function start({ base, origin = global.location.origin, root = "/", siteBase: under = "",
                     debug = false, store = null, telemetry = null } = {}) {
        if (worker) return ready;
        siteBase = String(under || "").replace(/\/+$/, "");
        worker = new Worker(new URL(root + WORKER, global.location.href), { type: "module" });
        worker.onmessage = onMessage;
        worker.onerror = (event) => console.error("[python] worker failed:", event.message);
        call("boot", { base, origin, siteBase, cancels, inFlight, debug, store, telemetry }).then(resolveReady, (error) => {
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
        const answer = await call("request", {
            method, path: route(url.pathname), query: url.search.replace(/^\?/, ""), headers,
            body: body ? body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) : null,
        });
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

    async function fetch(input, init) {
        await ready;
        const request = isRequest(input) ? input : null;
        const url = new URL(request ? request.url : String(input), global.location.href);
        const method = ((init && init.method) || (request && request.method) || "GET").toUpperCase();
        const mine = await whileBusy(url, method, init, request);
        if (mine) return mine;
        const chat = method === "GET" && url.pathname.match(/\/ai\/v1\/conversations\/([^/]+)\/events$/);
        if (chat) {
            const answered = await chatEvents(decodeURIComponent(chat[1]), url);
            if (answered) return answered;
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
        await hold(topicOf(url), wait);
        if (chat) {
            const answered = await chatEvents(decodeURIComponent(chat[1]), url);
            if (answered) return answered;
        }
        url.searchParams.set("wait", "0");
        return send(url, init, request);
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
        recent: () => recent.slice(),
        steps: () => steps.slice(),
        tasks: () => tasks.slice(),
        cancel,
        on,
        saveWith: (fn) => { saver = fn; },
        //: Every save still running (tests, and a page about to close).
        saved: () => Promise.all([...saving]),
    });
})(typeof window !== "undefined" ? window : globalThis);
