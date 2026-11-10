// Website arm: the static site ($PLEXORA_BENCH_WORK/site, built by static_build) opened in a fresh
// Chrome; the tonsil fixture imported through the routes; the viewer, a tour,
// every tool, the figure library, and a reload.
//   node bench_dataset_web.mjs [--fixture DIR]
import path from "node:path";
import { writeFileSync } from "node:fs";
import { INIT, WORKER_INIT, S, byRoute, get, launch, openTools, pollReady, post, pyRecent, startStatic, tileStats, tour, viewerDrawn, waitViewer } from "./benchlib.mjs";
import { seedBrowserStore } from "../../tests/browser/lib.mjs";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const fixture = opt("--fixture", path.join(S, "fixture", "tonsil"));
const files = opt("--files", "image.ome.tif,mask.tif,cells.csv").split(",").map((f) => path.join(fixture, f));
const NO_RELOAD = args.includes("--no-reload");
const ONLY_TOOLS = opt("--tools", null);
const PROFILE_SETUP = args.includes("--profile-setup");
const USE_INPUT = args.includes("--input");
const say = (m) => process.stderr.write(`${new Date().toISOString().slice(11,19)} ${m}\n`);
const out = { arm: "website", fixture, steps: {} };
// `--site`: a static build (default $PLEXORA_BENCH_WORK/site); `--py-dir`: a
// bundle's py/ folder served beside it (a site built with `--py /py/<version>/`).
const pyDir = opt("--py-dir", null);
const server = await startStatic(opt("--site", path.join(S, "site")),
                                 ["--tree", `fixture=${fixture}`, ...(pyDir ? ["--py", pyDir] : [])]);
const browser = await launch();
const errors = [];
try {
    const page = await browser.context.newPage();
    page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
    page.on("console", (m) => { const t = m.text(); if (t.startsWith("BENCH-BIG")) process.stderr.write(`${t}\n`); if (m.type() === "error") errors.push(t.slice(0, 200)); });
    // Every navigation of the app's frame after the first page: a tool or a
    // fetch that fails for no reason of its own is often a frame reloaded under it.
    const started = Date.now();
    out.frameNavigations = [];
    // Workers' consoles capped too (benchlib WORKER_INIT), each as it starts.
    out.workers = [];
    const workers = [];
    page.on("worker", (w) => { workers.push(w); w.evaluate(WORKER_INIT).then((url) => out.workers.push(url.slice(-60))).catch(() => null); });
    page.on("framenavigated", (f) => { if (f !== page.mainFrame()) out.frameNavigations.push({ ms: Date.now() - started, url: f.url().slice(0, 120) }); });
    await page.addInitScript(INIT);
    await page.setViewportSize({ width: 1440, height: 900 });
    const origin = `http://127.0.0.1:${server.port}`;
    await seedBrowserStore(browser.context, origin);
    let t0 = Date.now();
    // `--profile-setup`: the worker's debug hooks (`?pydebug`), for the profile below.
    await page.goto(`${origin}/${PROFILE_SETUP ? "?pydebug" : ""}`);
    let frameEl = await page.waitForSelector("#plexora-app", { timeout: 120000 });
    let frame = await frameEl.contentFrame();
    await frame.waitForFunction(() => document.getElementById("quick_view_go") && !document.getElementById("plexora-boot"), null, { timeout: 120000, polling: 20 });
    say('homeMs'); out.steps.homeMs = Date.now() - t0;
    // Which stage arrived when, and what asked for it (plexora/browser/stages.py).
    await page.evaluate(() => { window.__benchStages = []; window.PlexoraPython.on("stage", (e) => window.__benchStages.push({ stage: e.stage, importer: e.importer, ms: e.ms, at: Math.round(performance.now()) })); }).catch(() => null);
    await page.waitForFunction(() => window.PlexoraShellStarted !== undefined, null, { timeout: 120000 });
    await page.evaluate(() => window.PlexoraShellStarted);
    say('pythonStartedMs'); out.steps.pythonStartedMs = Date.now() - t0;

    // Pick as Chrome's real picker would: File System Access handles the
    // page can remember and reopen after a reload. The fixture is copied into
    // this profile's OPFS (served by serve.py --tree), and showOpenFilePicker
    // is stood in to hand those handles back.
    if (USE_INPUT) {
        await page.evaluate(() => { const i = document.createElement("input"); i.type = "file"; i.multiple = true; i.id = "pick"; i.hidden = true; document.body.append(i); });
        await page.setInputFiles("#pick", files);
        await page.evaluate(() => { window.showOpenFilePicker = undefined; });
    }
    say("copying fixture into OPFS");
    const copyMs = USE_INPUT ? 0 : await page.evaluate(async (names) => {
        const t = performance.now();
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle("bench-fixture", { create: true });
        const handles = [];
        for (const n of names) {
            const h = await dir.getFileHandle(n, { create: true });
            if ((await h.getFile()).size === 0) { const r = await fetch(`/fixture/${n}`); await r.body.pipeTo(await h.createWritable()); }
            handles.push(h);
        }
        window.__handles = handles;
        window.showOpenFilePicker = async () => window.__handles;
        return Math.round(performance.now() - t);
    }, files.map((f) => path.basename(f)));
    out.steps.fixtureCopyMs = copyMs;
    say("picking");
    const paths = USE_INPUT ? await page.evaluate(async () => {
        const fs = [...document.getElementById("pick").files];
        const mounted = await window.PlexoraPython.mount("/picked/bench", fs);
        mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, fs[i]));
        return mounted;
    }) : await page.evaluate(async () => {
        const picked = await window.PlexoraPicks.pick({ multiple: true });
        return Array.isArray(picked) ? picked : [picked];
    });
    out.steps.picked = paths;
    // Import (inspect, sample). The flat mask makes the page build its pyramid.
    t0 = Date.now();
    const inspected = await post(frame, "import/inspect", { paths });
    say('inspect'); out.steps.inspect = { ms: inspected.ms, status: inspected.status, layers: (inspected.body?.samples || []).flatMap((s) => (s.layers || []).map((l) => `${l.kind}:${l.label}`)),
                          questions: (inspected.body?.samples || []).flatMap((s) => (s.questions || []).map((q) => q.id)) };
    const imported = await post(frame, "import/sample", { paths, answers: {} });
    const name = imported.body?.name;
    say('importSample'); out.steps.importSample = { ms: imported.ms, status: imported.status, name, error: name ? undefined : (imported.text || JSON.stringify(imported.body).slice(0, 400)) };
    say('importTotalMs'); out.steps.importTotalMs = Date.now() - t0;
    say('pyramidBuilds'); out.steps.pyramidBuilds = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));
    if (!name) throw new Error("import failed");

    // The viewer.
    t0 = Date.now();
    const recentBefore = (await pyRecent(page)).length;
    await frame.evaluate((n) => { location.href = `/${n}`; }, name);
    frameEl = await page.waitForSelector("#plexora-app"); frame = await frameEl.contentFrame();
    await frame.waitForFunction(() => window.PlexoraShellFramed && document.body && !document.getElementById("plexora-boot"), null, { timeout: 120000, polling: 20 });
    say('viewerPageWrittenMs'); out.steps.viewerPageWrittenMs = Date.now() - t0;
    await frame.waitForFunction(() => window.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount() > 0, null, { timeout: 120000, polling: 30 }).catch(() => null);
    say('viewerFirstItemMs'); out.steps.viewerFirstItemMs = Date.now() - t0;
    await waitViewer(frame).catch(() => { out.steps.viewerTimedOut = true; });
    say('viewerDrawnMs'); out.steps.viewerDrawnMs = Date.now() - t0;
    say('ready'); out.steps.ready = await pollReady(frame, name);
    say('readyTotalMs'); out.steps.readyTotalMs = Date.now() - t0;
    say('openTiles'); out.steps.openTiles = await frame.evaluate(tileStats, 0);
    out.steps.openTileFetch = await frame.evaluate((() => { const s = window.__plexora?.seaDragonViewer?.tileFetch?.stats; return s ? { ...s } : null; }));
    const recentOpen = (await pyRecent(page)).slice(recentBefore);
    say('openPython'); out.steps.openPython = { n: recentOpen.length, ms: recentOpen.reduce((a, r) => a + r.ms, 0), py: recentOpen.reduce((a, r) => a + (r.pyMs || 0), 0), routes: byRoute(recentOpen) };
    say('items'); out.steps.items = await frame.evaluate(() => window.__plexora.seaDragonViewer.viewer.world.getItemCount());
    say('pyramidBuildsAfter'); out.steps.pyramidBuildsAfter = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));

    // Tour.
    say('tour'); out.steps.tour = await tour(frame);
    // Tools.
    if (PROFILE_SETUP) {
        // Saving a tool's set-up form reloads the table: profiled in the
        // website's own Python (cProfile, in the worker), the same answer the
        // form sends with its defaults.
        say('setupProfile'); out.steps.setupProfile = JSON.parse(await page.evaluate((code) => window.PlexoraPython.run(code), `
import cProfile, io, json, pstats, time
from plexora import app as _app
_client = _app.test_client()
_profile = cProfile.Profile()
_t0 = time.perf_counter()
_profile.enable()
_reply = _client.post("/${name}/requirements", json={"tool": "roi", "roles": {"cell_id": "CellID"}})
_profile.disable()
_ms = round((time.perf_counter() - _t0) * 1000)
_out = io.StringIO()
pstats.Stats(_profile, stream=_out).sort_stats("cumulative").print_stats(45)
json.dumps({"status": _reply.status_code, "ms": _ms, "profile": _out.getvalue()[-14000:]})
`));
    }
    say('tools'); out.steps.tools = await openTools(frame, page, ONLY_TOOLS ? ONLY_TOOLS.split(',') : null);
    // Figure library and back (the router or a full navigation?).
    t0 = Date.now();
    const viewerBefore = await frame.evaluate(() => Boolean(window.__plexora?.seaDragonViewer?.viewer));
    await frame.evaluate(() => { const a = document.querySelector('a[href$="/figures"], a[href*="figures"]'); if (a) a.click(); else location.href = "/figures"; });
    await frame.waitForFunction(() => /figures/.test(location.pathname) && document.readyState !== "loading", null, { timeout: 60000, polling: 30 }).catch(() => null);
    frameEl = await page.waitForSelector("#plexora-app"); frame = await frameEl.contentFrame();
    await frame.waitForFunction(() => !document.getElementById("plexora-boot") && document.body && document.body.innerText.length > 0, null, { timeout: 60000, polling: 30 }).catch(() => null);
    say('figuresPageMs'); out.steps.figuresPageMs = Date.now() - t0;
    say('figuresKeptViewer'); out.steps.figuresKeptViewer = await frame.evaluate(() => Boolean(window.__plexora?.seaDragonViewer?.viewer)).catch(() => null);
    t0 = Date.now();
    await frame.evaluate((n) => { history.back(); }, name);
    await page.waitForTimeout(100);
    frameEl = await page.waitForSelector("#plexora-app"); frame = await frameEl.contentFrame();
    await frame.waitForFunction(viewerDrawn, null, { timeout: 120000, polling: 50 }).catch(() => { out.steps.backTimedOut = true; });
    say('backToViewerMs'); out.steps.backToViewerMs = Date.now() - t0;
    say('viewerBeforeFigures'); out.steps.viewerBeforeFigures = viewerBefore;

    if (!NO_RELOAD) {
    // Reload (the whole site): Python starts again; the pyramid must be reused.
    t0 = Date.now();
    const builds0 = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));
    await page.reload();
    frameEl = await page.waitForSelector("#plexora-app", { timeout: 120000 }); frame = await frameEl.contentFrame();
    const asked = [];
    const askWatch = setInterval(async () => {
        try {
            const row = await frame.evaluate(() => { const b = [...document.querySelectorAll(".plexora-boot-actions button")]; const line = document.getElementById("plexora-boot-status")?.textContent; return b.length ? { line, buttons: b.map((x) => x.textContent) } : null; });
            if (row) { asked.push(row); await frame.evaluate(() => { const b = [...document.querySelectorAll(".plexora-boot-actions button")]; (b.find((x) => /Skip|browser/i.test(x.textContent)) || b[0]).click(); }); }
        } catch {}
    }, 500);
    await frame.waitForFunction(() => window.PlexoraShellFramed && document.body && !document.getElementById("plexora-boot"), null, { timeout: 120000, polling: 20 }).catch(() => { out.steps.reloadPageTimedOut = true; });
    clearInterval(askWatch);
    out.steps.reloadLoaderAsked = asked;
    out.steps.reloadPicks = await page.evaluate(async () => (await window.PlexoraPicks.list()).map((p) => ({ id: p.id, kind: p.kind, files: (p.files || []).length, handles: Boolean(p.handles || p.handle) })));
    say('reloadPageWrittenMs'); out.steps.reloadPageWrittenMs = Date.now() - t0;
    await page.evaluate(() => { window.__benchStages = window.__benchStages || []; window.PlexoraPython.on("stage", (e) => window.__benchStages.push({ stage: e.stage, importer: e.importer, ms: e.ms, at: Math.round(performance.now()) })); }).catch(() => null);
    await waitViewer(frame).catch(() => { out.steps.reloadViewerTimedOut = true; });
    say('reloadViewerDrawnMs'); out.steps.reloadViewerDrawnMs = Date.now() - t0;
    say('reloadReady'); out.steps.reloadReady = await pollReady(frame, name);
    say('reloadTotalMs'); out.steps.reloadTotalMs = Date.now() - t0;
    say('reloadInfo'); out.steps.reloadInfo = await page.evaluate(async () => { const i = await window.PlexoraPython.ready; return { derived: i.derived, store: i.store, phases: i.phases && Object.fromEntries(Object.entries(i.phases).map(([k, v]) => [k, Math.round(v - i.phases.start)])) }; });
    say('reloadBuilds'); out.steps.reloadBuilds = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));
    say('reloadTiles'); out.steps.reloadTiles = await frame.evaluate(tileStats, 0);
    out.steps.reloadTileFetch = await frame.evaluate((() => { const s = window.__plexora?.seaDragonViewer?.tileFetch?.stats; return s ? { ...s } : null; }));
    say('reloadTour'); out.steps.reloadTour = await tour(frame);
    say('heapMB'); out.steps.heapMB = await page.evaluate(async () => Math.round((await window.PlexoraPython.heap()) / 1e6));
    }
    say('perfMemory'); out.steps.perfMemory = await page.evaluate(async () => (performance.measureUserAgentSpecificMemory ? Math.round((await performance.measureUserAgentSpecificMemory()).bytes / 1e6) : null)).catch(() => null);
    say('heapMB'); out.steps.heapMB = await page.evaluate(async () => Math.round((await window.PlexoraPython.heap()) / 1e6));
    out.steps.recentTail = (await pyRecent(page)).slice(-60).map((r) => ({ p: r.path.slice(0, 60), ms: r.ms, py: r.pyMs, at: r.at }));
    out.steps.stages = await page.evaluate(() => window.__benchStages || []).catch(() => null);
    out.steps.stepsTail = await page.evaluate(() => window.PlexoraPython.steps().slice(-80));
    out.steps.handoffs = await frame.evaluate(() => (window.PlexoraBackend && window.PlexoraBackend.handoffs) || []).catch(() => null);
    out.workerBigLogs = (await Promise.all(workers.map((w) => w.evaluate(() => self.__bigLogs || []).catch(() => null)))).filter((x) => x && x.length);
    out.bigLogs = { frame: await frame.evaluate(() => window.__bigLogs || []).catch(() => null), host: await page.evaluate(() => window.__bigLogs || []).catch(() => null) };
    out.steps.toasts = await frame.evaluate(() => [...document.querySelectorAll('.plx-toast')].map((t) => t.innerText.slice(0, 200))).catch(() => null);
} catch (e) {
    out.error = String(e && e.stack || e).slice(0, 1000);
} finally {
    out.errors = errors.slice(0, 30);
    await browser.close();
    server.stop();
}
writeFileSync(path.join(S, "out", opt("--out", "dataset_web.json")), JSON.stringify(out, null, 1));
console.log(JSON.stringify(out).slice(0, 4000));
