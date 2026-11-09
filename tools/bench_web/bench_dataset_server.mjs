// Reference arm: the local Python webserver (this checkout, conda env) with
// its own data root; the same fixture imported through the same routes; the
// same viewer, tour, tools, figures page and reload.
//   node bench_dataset_server.mjs [--fixture DIR] [--data DIR]
import path from "node:path";
import { writeFileSync, mkdirSync } from "node:fs";
import { INIT, S, get, launch, openTools, pollReady, post, startServer, tileStats, tour, viewerDrawn, waitViewer } from "./benchlib.mjs";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const fixture = opt("--fixture", path.join(S, "fixture", "tonsil_srv"));
const data = opt("--data", path.join(S, "pxdata_srv"));
mkdirSync(data, { recursive: true });
const files = opt("--files", "image.ome.tif,mask.tif,cells.csv").split(",").map((f) => path.join(fixture, f));
const NO_RELOAD = args.includes("--no-reload");
const ONLY_TOOLS = opt("--tools", null);
const USE_INPUT = args.includes("--input");
const say = (m) => process.stderr.write(`${new Date().toISOString().slice(11,19)} ${m}\n`);
const out = { arm: "server", fixture, steps: {} };
const server = await startServer(data);
out.steps.servingMs = server.servingMs;
const browser = await launch();
const errors = [];
try {
    const page = await browser.context.newPage();
    page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
    await page.addInitScript(INIT);
    await page.setViewportSize({ width: 1440, height: 900 });
    let t0 = Date.now();
    await page.goto(server.url);
    await page.waitForSelector("#quick_view_go", { timeout: 60000 });
    say('homeMs'); out.steps.homeMs = Date.now() - t0;
    t0 = Date.now();
    const inspected = await post(page, "import/inspect", { paths: files });
    say('inspect'); out.steps.inspect = { ms: inspected.ms, status: inspected.status, layers: (inspected.body?.samples || []).flatMap((s) => (s.layers || []).map((l) => `${l.kind}:${l.label}`)) };
    const imported = await post(page, "import/sample", { paths: files, answers: {} });
    const name = imported.body?.name;
    say('importSample'); out.steps.importSample = { ms: imported.ms, status: imported.status, name, error: name ? undefined : (imported.text || JSON.stringify(imported.body).slice(0, 400)) };
    say('importTotalMs'); out.steps.importTotalMs = Date.now() - t0;
    if (!name) throw new Error("import failed");
    t0 = Date.now();
    await page.goto(`${server.url.replace(/\?token=.*/, "")}${name}`);
    say('viewerPageWrittenMs'); out.steps.viewerPageWrittenMs = Date.now() - t0;
    await page.waitForFunction(() => window.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount() > 0, null, { timeout: 120000, polling: 30 }).catch(() => null);
    say('viewerFirstItemMs'); out.steps.viewerFirstItemMs = Date.now() - t0;
    await waitViewer(page).catch(() => { out.steps.viewerTimedOut = true; });
    say('viewerDrawnMs'); out.steps.viewerDrawnMs = Date.now() - t0;
    say('ready'); out.steps.ready = await pollReady(page, name);
    say('readyTotalMs'); out.steps.readyTotalMs = Date.now() - t0;
    say('openTiles'); out.steps.openTiles = await page.evaluate(tileStats, 0);
    say('items'); out.steps.items = await page.evaluate(() => window.__plexora.seaDragonViewer.viewer.world.getItemCount());
    say('tour'); out.steps.tour = await tour(page);
    if (ONLY_TOOLS !== 'none') { say('tools'); out.steps.tools = await openTools(page, null, ONLY_TOOLS ? ONLY_TOOLS.split(',') : null); }
    if (args.includes('--no-figures')) throw Object.assign(new Error('stopped after the tour (--no-figures)'), { planned: true });
    t0 = Date.now();
    await page.evaluate(() => { const a = document.querySelector('a[href$="/figures"], a[href*="figures"]'); if (a) a.click(); else location.href = "/figures"; });
    await page.waitForFunction(() => /figures/.test(location.pathname) && document.readyState !== "loading", null, { timeout: 60000, polling: 30 }).catch(() => null);
    say('figuresPageMs'); out.steps.figuresPageMs = Date.now() - t0;
    say('figuresKeptViewer'); out.steps.figuresKeptViewer = await page.evaluate(() => Boolean(window.__plexora?.seaDragonViewer?.viewer)).catch(() => null);
    t0 = Date.now();
    await page.evaluate(() => history.back());
    await page.waitForFunction(viewerDrawn, null, { timeout: 120000, polling: 50 }).catch(() => { out.steps.backTimedOut = true; });
    say('backToViewerMs'); out.steps.backToViewerMs = Date.now() - t0;
    if (!NO_RELOAD) {
    t0 = Date.now();
    await page.reload();
    say('reloadPageWrittenMs'); out.steps.reloadPageWrittenMs = Date.now() - t0;
    await waitViewer(page).catch(() => { out.steps.reloadViewerTimedOut = true; });
    say('reloadViewerDrawnMs'); out.steps.reloadViewerDrawnMs = Date.now() - t0;
    say('reloadReady'); out.steps.reloadReady = await pollReady(page, name);
    say('reloadTotalMs'); out.steps.reloadTotalMs = Date.now() - t0;
    say('reloadTiles'); out.steps.reloadTiles = await page.evaluate(tileStats, 0);
    say('reloadTour'); out.steps.reloadTour = await tour(page);
    }
    say('perfMemory'); out.steps.perfMemory = await page.evaluate(async () => (performance.measureUserAgentSpecificMemory ? Math.round((await performance.measureUserAgentSpecificMemory()).bytes / 1e6) : null)).catch(() => null);
} catch (e) {
    out.error = e && e.planned ? undefined : String(e && e.stack || e).slice(0, 1000);
} finally {
    out.errors = errors.slice(0, 30);
    await browser.close();
    server.stop();
}
writeFileSync(path.join(S, "out", opt("--out", "dataset_server.json")), JSON.stringify(out, null, 1));
console.log(JSON.stringify(out).slice(0, 4000));
