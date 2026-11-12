/**
 * PlexoraEnvelope -- on the website, saying when something is more than a
 * browser tab does well, and offering what does it better: the desktop app,
 * which opens the project with nothing to export (services/desktopHandoff.js).
 * One notice offers a node: waiting on the network (`pace`), where a node
 * beside the data is the one thing that is faster (the user's call,
 * 2026-11-11). Every other notice leaves nodes to the Remote menu.
 *
 * The limits are measured (client/src/envelope.json, each with its date) and
 * a situation within them gets no notice at all. Eight moments (the stall one
 * is judged but never said, see `tile`):
 *
 * - **open** (`check`): the project's table against the worker's heap (P9's
 *   2.1 GiB at 36.6 M values, of the ~3 GiB a tab's worker has), scaled to
 *   this device where the browser says how much memory it has;
 * - **batch** (the worker's job records, `PlexoraPython.on("job")`): before a
 *   dataset job runs, images x the measured seconds per image against the
 *   limit (ten minutes with the tab open); while any job runs, its own rate
 *   once it has done a step;
 * - **build** (`beforeBuild`, from services/browserPyramid.js): a flat
 *   image's pyramid about to be built, its pixels at the measured rate a
 *   band worker against two minutes, and what a copy kept in the browser
 *   writes against the room the browser says it has left;
 * - **model** (`beforeModel`, from services/onnxExecutor.js): magic select's
 *   model about to load where the browser has no WebGPU adapter, with the
 *   measured time per click on WebAssembly;
 * - **stall** (`watchTiles`, a PerformanceObserver on the app's frame, not
 *   started by `check`): the rolling p95 of the last 50 tiles over the
 *   limit, after the open's own tiles and never while a job holds the
 *   worker. Judged only -- the one answer to slow media is a node, and no
 *   notice offers one;
 * - **refused** (`refused`, Python's "envelope" event): a build or read
 *   this tab cannot hold, refused before it started (server/utils/
 *   tab_room.py) -- its size and the desktop app;
 * - **pace** (`pace`: how long reads waited, as intervals -- Python's reads
 *   from each remote host, server/utils/read_pace.py; the picked file's
 *   reads, Python's (workers/pythonWorker.js `drivePace`) and the tile
 *   workers' (services/browserNode.js `paced`)): one cause held the page
 *   `waited_s` of the last `window_s` seconds at under its rate --
 *   "Waiting on the network" (the host and its MB/s; a data node beside the
 *   data) or "Reading a slow drive" (the file and its MB/s; a copy on this
 *   computer). The waiting, not the work between reads, is what counts;
 * - **stopped** (`stopped`, the worker's `fatal` event): Python failed for
 *   good -- past the open notice, a table that does not fit the worker's
 *   4 GiB ends Pyodide when an allocation fails -- "This tab ran out of
 *   memory" with the desktop app and Reload, never remembered.
 *
 * Each says what was found in a short title, one line with the measured
 * numbers rounded, and its buttons on one row. Once per project per tab;
 * "Continue here" or the notice's x is remembered for 30 days.
 *
 *   check(project, app)          when the app's frame opens a project
 *   judge(size, limits, device)  -> {verdict: "website"|"desktop", title, note}
 *   judgeBatch(job, limits, now) -> {verdict: "website"|"desktop", title, note, seconds}
 *   judgeBuild(build, limits, device) -> {verdict: "website"|"desktop", title, note, seconds}
 *   judgeModel(gpu, limits)      -> {verdict: "website"|"node", title, note}
 *   judgeStall(samples, limits)  -> {verdict: "website"|"node", title, note, p95}
 *   judgeSlow(cause, source, events, limits, now) -> {verdict: "website"|"node"|"copy",
 *                                title, note, waited, rate}
 *   pace(event)                  a read interval {cause, source, start, end, busy, bytes}
 *   onJob(event) / beforeBuild(build) / beforeModel() / tile(ms, app) / stopped(event)
 *                                the triggers (tests call them)
 *
 * Classic script, `window.PlexoraEnvelope`, in the site's host page. The
 * desktop app loads it too, in its own page (templates/base.html, with
 * `data-reads="desktop"`), for the one moment that applies there, **pace**:
 * it asks its Python how long reads waited (GET /read_pace every 2 s while
 * visible; server/utils/read_pace.py) -- the network per host, and tifffile's
 * reads of each file -- and says it the same way. The rest are about what a
 * browser tab holds. Uses PlexoraHandoff and the frame's PlexoraToast.
 */
(function (global) {
    "use strict";

    //: The desktop app's own page (templates/base.html), not the site's host.
    const SCRIPT = global.document && global.document.currentScript;
    const DESKTOP = Boolean(SCRIPT && SCRIPT.dataset && SCRIPT.dataset.reads === "desktop");
    // Under the site's base path (services/shell.js `BASE`; "" at its root);
    // on the desktop, beside this script, under the server's own base.
    const LIMITS = DESKTOP ? new URL("../../envelope.json", SCRIPT.src).href
        : (global.PLEXORA_SITE_BASE || "") + "/client/src/envelope.json";
    const REMEMBER_DAYS = 30;
    //: `${project}|${reason}` already said in this tab.
    const shown = new Set();
    //: Projects whose size was judged in this tab.
    const opened = new Set();
    let limits = null;
    //: The app's frame and its project, as last opened.
    const current = { project: null, app: null };
    //: Jobs the worker runs now (their ids), and the jobs already judged.
    const running = new Set();
    const judged = new Set();
    let gpu = null;

    const count = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n));
    const minutes = (seconds) => (seconds >= 5400 ? `${Math.round(seconds / 3600)} h` : `${Math.max(1, Math.round(seconds / 60))} min`);

    //: Whether `size` ({rows, columns}) fits a browser tab here.
    function judge(size, rules, device = {}) {
        const table = rules.table;
        const rows = Number(size && size.rows) || 0;
        const columns = Number(size && size.columns) || 0;
        const values = rows * columns;
        let limit = table.worker_values;
        if (device.memoryGb) limit *= Math.min(1, device.memoryGb / table.reference_device_memory_gb);
        if (values > limit) {
            const gb = (values * table.worker_bytes_per_value) / 1e9;
            return {
                verdict: "desktop",
                title: "Large for a browser tab",
                note: `${count(rows)} cells × ${columns} columns need about ${gb.toFixed(1)} GB here. `
                    + "The desktop app opens it with room to spare.",
            };
        }
        return { verdict: "website" };
    }

    /**
     * Whether a job (the worker's record: capability, status, progress
     * {done, total}, started_at) is longer than a tab should be held for.
     * Before it has done a step: its images x the measured seconds per image
     * of its capability (none measured: no verdict yet). Once it has: its own
     * rate so far.
     */
    function judgeBatch(job, rules, now = Date.now()) {
        const batch = rules.batch || {};
        const limit = Number(batch.limit_s) || 600;
        const progress = (job && job.progress) || {};
        const total = Number(progress.total) || 0;
        const done = Number(progress.done) || 0;
        if (!job || total < 2 || done >= total) return { verdict: "website" };
        const started = Date.parse(job.started_at || "");
        let seconds = null;
        let note = null;
        if (done >= 1 && Number.isFinite(started) && now > started) {
            seconds = ((now - started) / 1000 / done) * total;
            note = `About ${minutes(seconds)} here at this pace, with this tab open.`;
        } else {
            const each = Number((batch.per_image_s || {})[job.capability]);
            if (each > 0) {
                seconds = total * each;
                note = `${total} images: about ${minutes(seconds)} here, with this tab open.`;
            }
        }
        if (seconds === null || seconds <= limit) return { verdict: "website", seconds };
        return { verdict: "desktop", title: "A long run for a browser tab", note, seconds };
    }

    const ITEM = { uint8: 1, int8: 1, bool: 1, uint16: 2, int16: 2, float16: 2,
                   uint32: 4, int32: 4, float32: 4, uint64: 8, int64: 8, float64: 8 };

    /**
     * Whether a flat image's (or mask's) pyramid is a short build here
     * (services/browserPyramid.js, before it starts): its pixels at the
     * measured rate a band worker, with as many workers as the page starts
     * (`device.cores` less two, at most `build_workers_max`), against the
     * limit; and, for a copy kept in the browser, what it writes against the
     * room the browser says the site has left (`device.room`, bytes).
     */
    function judgeBuild(build, rules, device = {}) {
        const image = rules.image || {};
        const shape = (build && build.shape) || [];
        const raw = shape.reduce((n, side) => n * (Number(side) || 0), shape.length ? 1 : 0)
            * (ITEM[String(build && build.dtype)] || 2);
        if (!(raw > 0)) return { verdict: "website" };
        const workers = Math.max(1, Math.min(Number(image.build_workers_max) || 12, (Number(device.cores) || 4) - 2));
        const seconds = raw / ((Number(image.build_bytes_per_second_per_worker) || 23e6) * workers);
        const written = raw * (Number(image.build_written_per_raw) || 0.6);
        const what = build.kind === "labels" ? "labels" : "pixels";
        const gb = (bytes) => (bytes >= 10e9 ? Math.round(bytes / 1e9) : (bytes / 1e9).toFixed(1));
        if (Number.isFinite(device.room) && written > device.room) {
            return {
                verdict: "desktop", seconds,
                title: "No room for this pyramid here",
                note: `About ${gb(written)} GB to keep; ${gb(Math.max(0, device.room))} GB of room left here.`,
            };
        }
        if (seconds <= (Number(image.build_limit_s) || 120)) return { verdict: "website", seconds };
        return {
            verdict: "desktop", seconds,
            title: "A long build for a browser tab",
            note: `${gb(raw)} GB of ${what}: about ${minutes(seconds)} here, with this tab open.`,
        };
    }

    //: Whether magic select's model has a GPU here (`gpu`: a WebGPU adapter).
    function judgeModel(hasGpu, rules) {
        if (hasGpu) return { verdict: "website" };
        const seconds = Number((rules.model || {}).wasm_click_s) || 1;
        return {
            verdict: "node",
            title: "Magic select without a GPU here",
            note: `No WebGPU here: about ${seconds < 1.5 ? "1" : Math.round(seconds)} s a click `
                + `(${seconds} s measured).`,
        };
    }

    //: The 95th percentile of `samples` (ms), or null for none.
    function p95(samples) {
        if (!samples.length) return null;
        const sorted = [...samples].sort((a, b) => a - b);
        return sorted[Math.min(sorted.length - 1, Math.ceil(0.95 * sorted.length) - 1)];
    }

    //: Whether the last tiles (ms) say the image is read slowly here.
    function judgeStall(samples, rules) {
        const stall = rules.stall || {};
        const window = Number(stall.tiles) || 50;
        if (samples.length < window) return { verdict: "website" };
        const value = p95(samples.slice(-window));
        if (value <= (Number(stall.p95_ms) || 150)) return { verdict: "website", p95: value };
        return {
            verdict: "node",
            title: "Slow to read here",
            note: `Tiles take ${Math.round(value)} ms here; a node beside the data: `
                + `~${Number(stall.node_p95_ms) || 35} ms.`,
            p95: value,
        };
    }

    //: Of `events` ({start, end, busy, bytes}, ms since the epoch), the ms
    //: spent waiting and the bytes read within [from, to]. An interval that
    //: is all waiting (a tile's read) is joined with those it overlaps --
    //: tiles are read in parallel; one whose waiting is only part of its span
    //: (reads with work between them) counts its share.
    function waitedIn(events, from, to) {
        let waited = 0;
        let bytes = 0;
        const whole = [];
        for (const e of events) {
            const start = Math.max(from, e.start);
            const end = Math.min(to, e.end);
            if (!(end > start)) continue;
            const span = Math.max(1, e.end - e.start);
            const share = (end - start) / span;
            bytes += (Number(e.bytes) || 0) * share;
            const busy = Math.min(Math.max(0, Number(e.busy) || 0), span);
            if (busy >= 0.95 * span) whole.push([start, end]);
            else waited += busy * share;
        }
        whole.sort((a, b) => a[0] - b[0]);
        let run = null;
        for (const [start, end] of whole) {
            if (!run || start > run[1]) {
                if (run) waited += run[1] - run[0];
                run = [start, end];
            } else {
                run[1] = Math.max(run[1], end);
            }
        }
        if (run) waited += run[1] - run[0];
        return { waited, bytes };
    }

    const rateText = (rate) => (rate >= 10e6 ? `${Math.round(rate / 1e6)} MB/s`
        : rate >= 0.1e6 ? `${(rate / 1e6).toFixed(1)} MB/s` : "under 0.1 MB/s");

    /**
     * Whether reads from `source` ("network": a host; "drive": a picked file)
     * held the page: `waited_s` of the last `window_s` seconds spent waiting
     * on them, at under that cause's rate. A fast source read a lot is not
     * slow -- the work is large, and the other notices speak to that.
     */
    function judgeSlow(cause, source, events, rules, now = Date.now()) {
        const slow = (rules && rules.slow) || {};
        const windowMs = (Number(slow.window_s) || 15) * 1000;
        const waitedMs = (Number(slow.waited_s) || 10) * 1000;
        const limit = cause === "network" ? Number(slow.network_bytes_per_second) || 50e6
            : Number(slow.drive_bytes_per_second) || 100e6;
        const { waited, bytes } = waitedIn(events || [], now - windowMs, now);
        if (waited < waitedMs) return { verdict: "website", waited };
        const rate = bytes / (waited / 1000);
        if (rate >= limit) return { verdict: "website", waited, rate };
        const short = (text, most) => (text.length > most ? `${text.slice(0, most - 1)}…` : text);
        if (cause === "network") {
            return { verdict: "node", waited, rate, title: "Waiting on the network",
                     note: `${rateText(rate)} from ${short(source, 40)}.` };
        }
        return { verdict: "copy", waited, rate, title: "Reading a slow drive",
                 note: `${short(source, 24)} reads at ${rateText(rate)}; copy it here.` };
    }

    //: Read intervals by "cause|source", the last minute only.
    const paces = new Map();
    const PACE_KEEP_MS = 60000;

    /** One read interval, judged with the others from its source: said
     *  (`notice`) the first time its cause holds the page. Resolves the
     *  verdict, or null for an interval that says nothing. */
    function pace(event) {
        if (!event || !event.source || !(Number(event.end) > Number(event.start))) return null;
        const cause = event.cause === "network" ? "network" : "drive";
        const source = String(event.source);
        const key = `${cause}|${source}`;
        const now = Date.now();
        const start = Number(event.start);
        const end = Number(event.end);
        const list = (paces.get(key) || []).filter((e) => e.end >= now - PACE_KEEP_MS);
        list.push({ start, end, bytes: Number(event.bytes) || 0,
                    busy: event.busy !== undefined ? Number(event.busy) : end - start });
        paces.set(key, list);
        if (!limits) rules().catch(() => null);
        const verdict = judgeSlow(cause, source, list, limits || {}, now);
        if (verdict.verdict === "website") return verdict;
        const app = appNow();
        notice(cause === "network" ? "slow-network" : "slow-drive", projectOf(app, null) || "", app,
               verdict, `plexora-notice:slow-${cause}:${source}`).catch(() => null);
        return verdict;
    }

    function remembered(key) {
        try {
            const until = Number(global.localStorage.getItem(key));
            return Number.isFinite(until) && until > Date.now();
        } catch (error) {
            return false;
        }
    }

    function remember(key) {
        try {
            global.localStorage.setItem(key, String(Date.now() + REMEMBER_DAYS * 864e5));
        } catch (error) {
            // Not kept: asked again next time.
        }
    }

    //: The one fetch of the limits in flight, shared by everything that
    //: asks before it answers (each read interval does).
    let asking = null;

    async function rules() {
        if (limits) return limits;
        if (!asking) {
            asking = Promise.resolve().then(() => global.fetch(LIMITS))
                .then((answer) => answer.json()).catch(() => null);
        }
        limits = await asking;
        if (!limits) asking = null;   // failed: asked again next time
        return limits;
    }

    //: The app's frame now (the host's #plexora-app), else the last one seen.
    function appNow() {
        try {
            const frame = global.document && global.document.getElementById("plexora-app");
            if (frame && frame.contentWindow) return frame.contentWindow;
        } catch (error) { /* gone */ }
        return current.app;
    }

    function projectOf(app, fallback) {
        try {
            return (app && app.flaskVariables && app.flaskVariables.datasource) || fallback || current.project;
        } catch (error) {
            return fallback || current.project;
        }
    }

    //: The actions, in the plan's order, each on one row with the others.
    function actions(kind, project, app, key) {
        const desktop = (label) => ({ label, onSelect: () => global.PlexoraHandoff && global.PlexoraHandoff.open(project, app) });
        const stay = { label: "Continue here", onSelect: () => remember(key) };
        if (kind === "open") return [{ ...desktop("Open in Desktop App"), primary: true }, stay];
        if (kind === "batch" || kind === "build" || kind === "model") return [{ ...desktop("Desktop app"), primary: true }, stay];
        if (kind === "slow-network") {
            const node = {
                label: "Set up a data node", primary: true,
                onSelect: () => {
                    const modal = app && app.PlexoraConnectionModal;
                    if (modal) modal.open({ kind: "node", view: "recipes",
                                            intent: "A data node beside the data reads it there and sends only what is on screen." });
                },
            };
            return [node, { label: "Close", onSelect: () => remember(key) }];
        }
        if (kind === "slow-drive") return [{ label: "OK", primary: true, onSelect: () => remember(key) }];
        // Refused, not slow: nothing to continue, so the other button closes it.
        if (kind === "tab-limit") return [{ ...desktop("Desktop app"), primary: true }, { label: "Close", onSelect: () => null }];
        if (kind === "stopped") {
            const reload = { label: "Reload", onSelect: () => global.location.reload() };
            return project ? [{ ...desktop("Open in Desktop App"), primary: true }, reload]
                : [{ ...reload, primary: true }];
        }
        return [{ ...stay, primary: true }];
    }

    /** The notice for `reason` on `project`, unless said in this tab or
     *  dismissed in the last 30 days. Resolves whether it was shown. */
    async function notice(reason, project, app, verdict, key = `plexora-notice:${project}:${reason}`) {
        const once = `${project}|${reason}`;
        if (!app || shown.has(once) || remembered(key)) return false;
        shown.add(once);
        for (let i = 0; i < 50 && !app.PlexoraToast; i += 1) await new Promise((r) => global.setTimeout(r, 200));
        if (!app.PlexoraToast) return false;
        app.PlexoraToast.show({
            title: verdict.title, note: verdict.note, timeout: 0,
            actions: actions(reason, project, app, key),
            onDismiss: (why) => { if (why === "user") remember(key); },
        });
        return true;
    }

    async function sizeOf(app, project) {
        const answer = await app.fetch(`${global.PLEXORA_SITE_BASE || ""}/project/${encodeURIComponent(project)}/size`);
        return answer.ok ? answer.json() : null;
    }

    async function check(project, app) {
        if (!project) return null;
        current.project = project;
        current.app = app;
        // Tiles are not watched: their time is the website's own tile path as
        // much as the media (they crossed the stall limit on a large image
        // with nothing slow about the media). Slow media is told from the
        // reads' own waiting instead (`pace`). `watchTiles` stays for a caller
        // that wants the measurement.
        // Judged once per project per tab: every page of a project opens a frame.
        if (opened.has(project)) return null;
        opened.add(project);
        if (!await rules()) return null;
        // The table may still be loading behind the viewer: asked again once.
        let size = await sizeOf(app, project).catch(() => null);
        if (size && size.rows == null) {
            await new Promise((resolve) => global.setTimeout(resolve, 15000));
            size = await sizeOf(app, project).catch(() => null);
        }
        if (!size) return null;
        const device = { memoryGb: global.navigator.deviceMemory || null };
        const verdict = judge(size, limits, device);
        if (verdict.verdict !== "desktop") return verdict;
        await notice("open", project, app, verdict, `plexora-notice:${project}:desktop`);
        return verdict;
    }

    // -- batch: the worker's job records (services/pythonClient.js) ------------

    async function onJob(event) {
        const job = event && event.job;
        if (!job || !job.job_id) return null;
        if (job.status !== "queued" && job.status !== "running") {
            running.delete(job.job_id);
            return null;
        }
        running.add(job.job_id);
        if (judged.has(job.job_id) || !await rules()) return null;
        const verdict = judgeBatch(job, limits);
        if (verdict.verdict !== "desktop") return verdict;
        judged.add(job.job_id);
        const app = appNow();
        await notice("batch", projectOf(app, job.project), app, verdict);
        return verdict;
    }

    // -- build: a flat image's pyramid about to start (services/browserPyramid.js)

    //: `build`: {shape, dtype, kind, browser} -- `browser` when the copy is
    //: kept in the site's storage rather than in a folder on disk.
    async function beforeBuild(build) {
        if (!build || !await rules()) return null;
        const nav = global.navigator || {};
        const device = { cores: nav.hardwareConcurrency || null };
        if (build.browser && nav.storage && typeof nav.storage.estimate === "function") {
            try {
                const { quota, usage } = await nav.storage.estimate();
                if (quota) device.room = quota - (usage || 0);
            } catch (error) { /* no estimate: time only */ }
        }
        const verdict = judgeBuild(build, limits, device);
        if (verdict.verdict === "website") return verdict;
        const app = appNow();
        await notice("build", projectOf(app), app, verdict);
        return verdict;
    }

    // -- model: magic select about to load (services/onnxExecutor.js) ----------

    function hasGpu() {
        if (!gpu) {
            const nav = global.navigator || {};
            gpu = nav.gpu && typeof nav.gpu.requestAdapter === "function"
                ? nav.gpu.requestAdapter().then(Boolean, () => false) : Promise.resolve(false);
        }
        return gpu;
    }

    async function beforeModel() {
        if (!await rules()) return null;
        const verdict = judgeModel(await hasGpu(), limits);
        if (verdict.verdict === "website") return verdict;
        const app = appNow();
        await notice("model", projectOf(app), app, verdict);
        return verdict;
    }

    // -- stall: the frame's tile timings ------------------------------------------

    const TILE = /\/generated\/(?:data|layer)\/|\/node\/v1\/(?:image|seg)\/[^/]+\/tile\//;

    //: One tile's time (ms) on `app`'s frame; resolves a verdict once the
    //: window is full. Tiles while a job holds the worker are not counted.
    //: The verdict is returned, never shown: no notice suggests a node.
    async function tile(ms, app) {
        const watch = app && app.__plexoraStall;
        if (!watch || !(ms >= 0) || running.size) return null;
        watch.seen += 1;
        const stall = (limits && limits.stall) || {};
        if (watch.seen <= (Number(stall.skip) || 50)) return null;
        watch.samples.push(ms);
        const window = Number(stall.tiles) || 50;
        if (watch.samples.length > window) watch.samples.shift();
        if (watch.samples.length < window || !limits) return null;
        return judgeStall(watch.samples, limits);
    }

    function watchTiles(app) {
        if (!app || app.__plexoraStall) return;
        app.__plexoraStall = { seen: 0, samples: [] };
        rules().catch(() => null);
        const Observer = app.PerformanceObserver;
        if (typeof Observer !== "function") return;
        try {
            new Observer((list) => {
                for (const entry of list.getEntries()) {
                    if (entry.initiatorType !== "fetch" && entry.initiatorType !== "xmlhttprequest"
                        && entry.initiatorType !== "img") continue;
                    const where = app.PlexoraPerf && app.PlexoraPerf.classify ? app.PlexoraPerf.classify(entry.name) : null;
                    const isTile = where ? where.kind !== "points" : TILE.test(entry.name);
                    if (isTile) tile(entry.responseEnd - entry.startTime, app).catch(() => null);
                }
            }).observe({ type: "resource", buffered: true });
        } catch (error) {
            // An engine without resource timing: nothing to watch.
        }
    }

    // -- stopped: Python failed for good (the worker's `fatal` event) --------

    async function stopped(event) {
        const app = appNow();
        const project = projectOf(app, null);
        const verdict = event && event.memory
            ? { title: "This tab ran out of memory",
                note: "This project needs more than the 4 GB a browser tab can give. "
                    + "The desktop app opens it with room to spare." }
            : { title: "Plexora stopped in this tab",
                note: "Its Python failed and cannot go on"
                    + (event && event.cause ? ` (${String(event.cause).slice(0, 120)})` : "")
                    + ". Reload, or open the project in the desktop app." };
        // Never remembered: a dismissal must not hide the next failure.
        return notice("stopped", project || "", app, verdict, `plexora-notice:stopped:${Date.now()}`);
    }

    /**
     * Python refused a build or read this tab cannot hold (server/utils/
     * tab_room.py, the "envelope" event): said once per project in this tab
     * (one sentence is enough; the next refusal's own error says the rest),
     * never remembered across visits.
     */
    async function refused(event) {
        if (!event || !event.title) return false;
        const app = appNow();
        const project = projectOf(app, null) || "";
        return notice(String(event.reason || "tab-limit"), project, app,
                      { title: String(event.title), note: String(event.note || "") },
                      `plexora-notice:refused:${Date.now()}`);
    }

    // -- the desktop: its Python's read intervals, asked for ---------------------

    const READS_EVERY_MS = 2000;

    /** Ask the desktop's Python for the read intervals since the last ask
     *  (server/routes/system_routes.py `/read_pace`) and judge each. Its
     *  clock is lined up with this page's by the `now` it answers with. */
    function watchReads(app, url) {
        let after = 0;
        let busy = false;
        current.app = app;
        async function ask() {
            if (busy || (app.document && app.document.visibilityState !== "visible")) return;
            busy = true;
            try {
                const answer = await app.fetch(`${url}?after=${after}`, { cache: "no-store" });
                if (!answer.ok) return;
                const body = await answer.json();
                const shift = Number(body.now) ? Date.now() - Number(body.now) : 0;
                if (Number.isFinite(Number(body.last))) after = Number(body.last);
                for (const event of body.events || []) {
                    pace({ ...event, start: Number(event.start) + shift, end: Number(event.end) + shift });
                }
            } catch (error) {
                // The server is away: appStatus says so; asked again next time.
            } finally {
                busy = false;
            }
        }
        rules().catch(() => null);
        ask();
        return global.setInterval(ask, READS_EVERY_MS);
    }

    if (DESKTOP) watchReads(global, new URL("../../../../read_pace", SCRIPT.src).href);

    if (global.PlexoraPython && typeof global.PlexoraPython.on === "function") {
        global.PlexoraPython.on("job", (event) => { onJob(event).catch(() => null); });
        global.PlexoraPython.on("fatal", (event) => { stopped(event).catch(() => null); });
        global.PlexoraPython.on("envelope", (event) => { refused(event).catch(() => null); });
        global.PlexoraPython.on("pace", (event) => { pace(event); });
    }

    global.PlexoraEnvelope = Object.freeze({ check, judge, judgeBatch, judgeBuild, judgeModel, judgeStall, p95,
                                             onJob, beforeBuild, beforeModel, tile, watchTiles, stopped,
                                             refused, judgeSlow, pace, watchReads });
})(typeof window !== "undefined" ? window : globalThis);
