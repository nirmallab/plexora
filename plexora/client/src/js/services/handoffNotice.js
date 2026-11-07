/**
 * PlexoraEnvelope -- on the website, saying when something is more than a
 * browser tab does well, and offering what does it better: the desktop app,
 * which opens the project with nothing to export (services/desktopHandoff.js),
 * or a node beside the data (services/handoff.js `pairNode`).
 *
 * The limits are measured (client/src/envelope.json, each with its date) and
 * a situation within them gets no notice at all. Four moments:
 *
 * - **open** (`check`): the project's table against the worker's heap (P9's
 *   2.1 GiB at 36.6 M values, of the ~3 GiB a tab's worker has), scaled to
 *   this device where the browser says how much memory it has;
 * - **batch** (the worker's job records, `PlexoraPython.on("job")`): before a
 *   dataset job runs, images x the measured seconds per image against the
 *   limit (ten minutes with the tab open); while any job runs, its own rate
 *   once it has done a step;
 * - **model** (`beforeModel`, from services/onnxExecutor.js): magic select's
 *   model about to load where the browser has no WebGPU adapter, with the
 *   measured time per click on WebAssembly;
 * - **stall** (`watchTiles`, a PerformanceObserver on the app's frame): the
 *   rolling p95 of the last 50 tiles over the limit, after the open's own
 *   tiles and never while a job holds the worker: slow media, where a node
 *   beside the data helps and the desktop app on the same share would not.
 *
 * Each says what was found in a short title, one line with the measured
 * numbers rounded, and its buttons on one row. Once per project per tab;
 * "Continue here" or the notice's x is remembered for 30 days.
 *
 *   check(project, app)          when the app's frame opens a project
 *   judge(size, limits, device)  -> {verdict: "website"|"desktop", title, note}
 *   judgeBatch(job, limits, now) -> {verdict: "website"|"desktop", title, note, seconds}
 *   judgeModel(gpu, limits)      -> {verdict: "website"|"node", title, note}
 *   judgeStall(samples, limits)  -> {verdict: "website"|"node", title, note, p95}
 *   onJob(event) / beforeModel() / tile(ms, app)  the triggers (tests call them)
 *
 * Classic script, `window.PlexoraEnvelope`, in the site's host page only: the
 * desktop app shows no notice. Uses PlexoraHandoff and the frame's
 * PlexoraToast and PlexoraWebHandoff.
 */
(function (global) {
    "use strict";

    // Under the site's base path (services/shell.js `BASE`; "" at its root).
    const LIMITS = (global.PLEXORA_SITE_BASE || "") + "/client/src/envelope.json";
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

    async function rules() {
        if (!limits) limits = await (await global.fetch(LIMITS)).json().catch(() => null);
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
        const node = () => ({ label: "Connect a node", onSelect: () => app && app.PlexoraWebHandoff && app.PlexoraWebHandoff.pairNode() });
        const stay = { label: "Continue here", onSelect: () => remember(key) };
        if (kind === "open") return [{ ...desktop("Open in Desktop App"), primary: true }, stay];
        if (kind === "batch") return [{ ...desktop("Desktop app"), primary: true }, node(), stay];
        if (kind === "model") return [{ ...node(), primary: true }, desktop("Desktop app"), stay];
        return [{ ...node(), primary: true }, stay];
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
        watchTiles(app);
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
        const verdict = judgeStall(watch.samples, limits);
        if (verdict.verdict === "node") await notice("stall", projectOf(app), app, verdict);
        return verdict;
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

    if (global.PlexoraPython && typeof global.PlexoraPython.on === "function") {
        global.PlexoraPython.on("job", (event) => { onJob(event).catch(() => null); });
    }

    global.PlexoraEnvelope = Object.freeze({ check, judge, judgeBatch, judgeModel, judgeStall, p95,
                                             onJob, beforeModel, tile, watchTiles });
})(typeof window !== "undefined" ? window : globalThis);
