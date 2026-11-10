// The user's flow on the website with real file handles: the fixture's files
// hard-linked into this profile's OPFS (same disk), picked as Chrome's picker
// hands them back, so a reload restores them silently as "Allow on every
// visit" does. Import, view, turn channels on (eye), reload; brightness of the
// viewer sampled from screenshots throughout. The local server's arm is
// bench_flow_server.mjs.
//
//   node tools/bench_web/bench_flow_web.mjs --site SITE --py-dir BUNDLE/py --fixture DIR
//        [--files image,mask,table] [--pause MS] [--turn N] [--channel-watch MS]
//        [--reload-watch MS] [--key-reload] [--trace] [--out FILE] [--shots DIR]
//   (or --origin https://viewer.plexoraapp.com instead of --site, for the live site)
//
// steps: openDrawnMs (first item fully drawn), openLitMs (5% of the viewer
// lit), channelRequests (stats/fit/auto-contrast after the eyes), reloadLitMs,
// keptReport (answers given from the reload's cache, any found changed),
// builds (the page's pyramid builds). --trace (needs ?pydebug) logs Python's
// load path; --key-reload reloads the frame only (F5 in the app).
// Use a plain (not pyramidal) mask too: a prepared pyramid hides the page's
// build and what the reload's cache keeps while it runs.
import path from "node:path";
import { writeFileSync, mkdtempSync, readdirSync, statSync, readFileSync, unlinkSync, linkSync, openSync, readSync, closeSync } from "node:fs";
import { tmpdir } from "node:os";
import { INIT, WORKER_INIT, launch, levelWatch, levelsSeen, post, pyRecent, startStatic } from "./benchlib.mjs";
import { seedBrowserStore } from "../../tests/browser/lib.mjs";
const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const site = opt("--site", null);
const served = site ? await startStatic(site, opt("--py-dir", null) ? ["--py", opt("--py-dir")] : []) : null;
const origin = served ? `http://127.0.0.1:${served.port}` : opt("--origin", "https://viewer.plexoraapp.com");
const fixture = opt("--fixture");
const names = opt("--files", "image.ome.tif,mask.labels.pyramid.ome.tiff,cells.csv").split(",");
const outFile = opt("--out", "handles.json");
const shots = opt("--shots", ".");
const RELOAD_WATCH_MS = Number(opt("--reload-watch", "300000"));
const CHANNEL_WATCH_MS = Number(opt("--channel-watch", "90000"));
const T0 = Date.now();
const say = (m) => process.stderr.write(`${((Date.now() - T0) / 1000).toFixed(1)}s ${m}\n`);
const out = { origin, failed: [], errors: [], steps: {}, samples: [] };
const profile = mkdtempSync(path.join(tmpdir(), "pxhandles-"));
const browser = await launch(profile);
const page = await browser.context.newPage();
page.on("pageerror", (e) => out.errors.push({ t: Date.now() - T0, e: String(e).slice(0, 300) }));
page.on("console", (m) => { if (m.type() === "error") out.errors.push({ t: Date.now() - T0, e: m.text().slice(0, 300) }); });
page.on("requestfailed", (r) => { if (!r.url().includes("cloudflareinsights")) out.failed.push({ t: Date.now() - T0, url: r.url().slice(0, 160), why: r.failure()?.errorText }); });
page.on("response", (r) => { if (r.status() >= 400) out.failed.push({ t: Date.now() - T0, url: r.url().slice(0, 160), status: r.status() }); });
out.logs = []; out.keptWarn = [];
page.on("console", (m) => { if (/kept answers/.test(m.text())) out.keptWarn.push({ t: Date.now() - T0, text: m.text().slice(0, 600) }); });
const keep = (where, text) => { if (text.startsWith("TRACE") && out.logs.length < 6000) { out.logs.push({ t: Date.now() - T0, where: where.slice(-14), text: text.slice(0, 200) }); return; } if (/could not answer|Traceback|Error|error|window|freeze|scan|failed|Exception/.test(text) && out.logs.length < 400) out.logs.push({ t: Date.now() - T0, where, text: text.slice(0, 400) }); };
page.on("console", (m) => keep("page:" + m.type(), m.text()));
page.on("worker", (w) => { w.on("console", (m) => keep("worker:" + w.url().slice(-40), m.text())); w.evaluate(WORKER_INIT).catch(() => null); });
await page.addInitScript(INIT);
await page.setViewportSize({ width: 1440, height: 900 });
const keptDump = async () => page.evaluate(async () => { try { const c = await caches.open("plexora-kept"); const out = []; for (const r of await c.keys()) { if (!/channel_names|spec|ome_metadata|database_description/.test(r.url)) continue; const b = await (await c.match(r)).text(); out.push({ url: r.url.slice(-90), body: b.slice(0, 1500) }); } return out; } catch (e) { return String(e); } });
const frameOf = async () => (await page.waitForSelector("#plexora-app", { timeout: 120000 })).contentFrame();
// Mean brightness of the viewer's area (x > 380), from a screenshot decoded in the page.
async function brightness(label) {
  try {
    const png = await page.screenshot({ clip: { x: 400, y: 60, width: 1020, height: 820 } });
    if (label) writeFileSync(path.join(shots, `${label}.png`), png);
    return await page.evaluate(async (b64) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const bmp = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
      const c = new OffscreenCanvas(bmp.width, bmp.height); const g = c.getContext("2d"); g.drawImage(bmp, 0, 0);
      const d = g.getImageData(0, 0, bmp.width, bmp.height).data; let s = 0, lit = 0;
      for (let i = 0; i < d.length; i += 16) { const v = (d[i] + d[i + 1] + d[i + 2]) / 3; s += v; if (v > 24) lit++; }
      return { mean: +(s / (d.length / 16)).toFixed(2), lit: +(lit / (d.length / 16)).toFixed(4) };
    }, png.toString("base64"));
  } catch (e) { return { error: String(e).slice(0, 100) }; }
}
const state = async (frame) => frame.evaluate(() => {
  const v = window.__plexora?.seaDragonViewer?.viewer;
  let notLoaded = null; if (v) { notLoaded = 0; for (let i = 0; i < v.world.getItemCount(); i++) if (!v.world.getItemAt(i).getFullyLoaded()) notLoaded++; }
  const slots = [...document.querySelectorAll(".channel-slot-eye")].map((e) => e.getAttribute("aria-pressed"));
  return { path: location.pathname, boot: document.getElementById("plexora-boot-status")?.textContent || null,
           spinner: Boolean(document.querySelector(".viewer-loading:not([hidden]), .osd-loading:not([hidden])")),
           items: v ? v.world.getItemCount() : null, notLoaded, slots: slots.join("") };
}).catch((e) => ({ error: String(e).slice(0, 120) }));
const queue = async () => page.evaluate(() => window.PlexoraPython?.state?.()).catch(() => null);
const sample = async (frame, phase, label) => { const row = { t: Date.now() - T0, phase, ...(await state(frame)), b: await brightness(label), q: await queue() }; out.samples.push(row); return row; };
function findMarked(dir, marker, found = []) {
  for (const n of readdirSync(dir)) { const p = path.join(dir, n); let s; try { s = statSync(p); } catch { continue; }
    if (s.isDirectory()) findMarked(p, marker, found);
    else if (s.size > 0 && s.size < 4096) { try { if (readFileSync(p).includes(marker)) found.push(p); } catch {} } }
  return found;
}
try {
  // The storage choice made first, so the onboarding (a local image's, or a save's) never shows.
  await seedBrowserStore(browser.context, origin);
  await page.goto(`${origin}/${args.includes("--trace") ? "?pydebug" : ""}`);
  let frame = await frameOf();
  await frame.waitForFunction(() => document.getElementById("quick_view_go") && !document.getElementById("plexora-boot"), null, { timeout: 120000, polling: 50 });
  await page.waitForFunction(() => window.PlexoraShellStarted !== undefined, null, { timeout: 120000 });
  await page.evaluate(() => window.PlexoraShellStarted);
  out.steps.version = await page.evaluate(async () => (await (await fetch("/client/shell.json", { cache: "no-store" })).json()).version).catch(() => null);
  say(`python started, site ${out.steps.version}`);
  if (args.includes("--trace")) {
    out.traceInstall = await page.evaluate((code) => window.PlexoraPython.run(code), `
import js, time, functools
from plexora.server.models import data_model as _dm
from plexora.server.providers import browser as _pb
from plexora import _work as _w
_t0 = time.perf_counter()
def _wrap(mod, name):
    fn = getattr(mod, name)
    @functools.wraps(fn)
    def inner(*a, **k):
        tag = name + "(" + ",".join(str(x)[:60] for x in a[:2]) + ")"
        js.console.log(f"TRACE {round((time.perf_counter()-_t0)*1000)} > {tag}")
        try:
            out = fn(*a, **k)
            js.console.log(f"TRACE {round((time.perf_counter()-_t0)*1000)} < {tag} = {str(out)[:60]}")
            return out
        except BaseException as e:
            js.console.log(f"TRACE {round((time.perf_counter()-_t0)*1000)} ! {tag} {type(e).__name__}")
            raise
    setattr(mod, name, inner)
for n in ("load_datasource",):
    if hasattr(_dm, n): _wrap(_dm, n)

from plexora.agent.evidence import calibration as _cal, crops as _crops, cell_window as _cw
from plexora.browser import bridge as _br
from plexora.agent.evidence import image_qc as _iq
for m, ns in ((_cal, ("calibrate", "compute", "_anchors", "_cell_caps", "_table_fingerprint", "_qc_fingerprint")), (_crops, ("read_cell_crops",)), (_cw, ("cell_windows",)), (_iq, ("read_overview", "tissue_mask", "overview_level")), (_crops, ("_read_plane_crops",)), (__import__("plexora.server.utils.source_image", fromlist=["x"]).SourceImage, ("read_many",)), (__import__("plexora.server.providers.local", fromlist=["x"]).LocalSegmentationProvider, ("read_region",))):
    for n in ns:
        if hasattr(m, n): _wrap(m, n)
import threading as _th
_OrigLock = type(_th.Lock())

_orig_task_run = _w.Task.run
def _task_run(self):
    js.console.log(f"TRACE {round((time.perf_counter()-_t0)*1000)} task> {self.name}")
    try: return _orig_task_run(self)
    finally: js.console.log(f"TRACE {round((time.perf_counter()-_t0)*1000)} task< {self.name}")
_w.Task.run = _task_run
"ok"
`).catch((e) => String(e));
    say("trace " + out.traceInstall);
  }
  // Marked stand-ins in OPFS, then the real files hard-linked over them.
  const tag = Math.random().toString(36).slice(2);
  await page.evaluate(async ([names, tag]) => {
    const root = await navigator.storage.getDirectory(); const dir = await root.getDirectoryHandle("bench-fixture", { create: true });
    for (const n of names) { const h = await dir.getFileHandle(n, { create: true }); const w = await h.createWritable(); await w.write(`PXMARK:${tag}:${n}`); await w.close(); }
  }, [names, tag]);
  for (const n of names) {
    const found = findMarked(profile, `PXMARK:${tag}:${n}`);
    if (found.length !== 1) throw new Error(`OPFS backing file for ${n}: ${found.length} found`);
    unlinkSync(found[0]); linkSync(path.resolve(fixture, n) === path.join(fixture, n) ? (await import("node:fs")).realpathSync(path.join(fixture, n)) : path.join(fixture, n), found[0]);
  }
  const sizes = await page.evaluate(async (names) => {
    const root = await navigator.storage.getDirectory(); const dir = await root.getDirectoryHandle("bench-fixture");
    const hs = []; for (const n of names) hs.push(await dir.getFileHandle(n));
    window.__handles = hs; window.showOpenFilePicker = async () => window.__handles;
    return Promise.all(hs.map(async (h) => (await h.getFile()).size));
  }, names);
  out.steps.sizes = sizes; say(`handles: ${sizes}`);
  const paths = await page.evaluate(async () => { const p = await window.PlexoraPicks.pick({ multiple: true }); return Array.isArray(p) ? p : [p]; });
  let t = Date.now();
  const inspected = await post(frame, "import/inspect", { paths });
  const imported = await post(frame, "import/sample", { paths, answers: {} });
  const name = imported.body?.name; out.steps.import = { ms: Date.now() - t, name, status: imported.status };
  say(`imported ${name} in ${Date.now() - t} ms`);
  if (!name) throw new Error("import failed " + (imported.text || JSON.stringify(imported.body)).slice(0, 300));
  t = Date.now();
  await frame.evaluate((n) => { location.href = `/${n}`; }, name);
  await page.waitForTimeout(300);
  frame = await frameOf();
  await frame.waitForFunction((n) => location.pathname === `/${n}` && window.PlexoraShellFramed && document.body && !document.getElementById("plexora-boot") && document.querySelector(".channel-slot-eye"), name, { timeout: 120000, polling: 50 });
  // Until the first channel is lit, or 60 s.
  for (let i = 0; i < 40; i++) { const r = await sample(frame, "open"); if (r.items >= 1 && r.notLoaded === 0 && !out.steps.openDrawnMs) out.steps.openDrawnMs = Date.now() - t; if (r.b?.lit > 0.05) { out.steps.openLitMs = Date.now() - t; break; } await page.waitForTimeout(500); }
  out.steps.keptDumpOpen = await keptDump();
  say(`open: lit after ${out.steps.openLitMs ?? "never (60 s)"} ms`); await brightness("1_open");
  if (opt("--pause", null)) await page.waitForTimeout(Number(opt("--pause")));
  // Channels on: the eye of each slot that is off.
  t = Date.now();
  const before = (await sample(frame, "channels")).b;
  const eyes = await frame.$$(".channel-slot-eye");
  await levelWatch(frame).catch(() => null);
  let turned = 0; for (const e of eyes) { const on = await e.getAttribute("aria-pressed"); if (on === "false" && turned < Number(opt("--turn", "4"))) { await e.click(); turned++; await page.waitForTimeout(150); } }
  out.steps.turnedOn = turned; say(`turned on ${turned} channels`);
  const since = await page.evaluate(() => performance.now());
  out.scanDebug = [];
  for (let end = Date.now() + CHANNEL_WATCH_MS; Date.now() < end;) {
    const r = await sample(frame, "channels");
    if (!r.q) {
      const ws = page.workers();
      if (!out.stuckStack) {
        try {
          const cdp = await browser.context.newCDPSession(page);
          const { targetInfos } = await cdp.send("Target.getTargets");
          const py = targetInfos.find((t) => t.url.includes("pythonWorker.js"));
          const { sessionId } = await cdp.send("Target.attachToTarget", { targetId: py.targetId, flatten: false });
          let n = 1; const pending = new Map();
          cdp.on("Target.receivedMessageFromTarget", (e) => { if (e.sessionId !== sessionId) return; const m = JSON.parse(e.message);
            if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
            if (m.method === "Debugger.paused") out.stuckStack = m.params.callFrames.slice(0, 25).map((f) => `${f.functionName || "(anon)"} ${f.url.split("/").pop()}:${f.location.lineNumber + 1}`); });
          const send = (method, params = {}) => new Promise((res) => { const id = n++; pending.set(id, res); cdp.send("Target.sendMessageToTarget", { sessionId, message: JSON.stringify({ id, method, params }) }); });
          await send("Debugger.enable"); await send("Debugger.pause");
          for (let i = 0; i < 50 && !out.stuckStack; i++) await page.waitForTimeout(100);
        } catch (e) { out.stuckStack = ["cdp failed: " + String(e).slice(0, 200)]; }
      }
      out.workersAlive = await Promise.all(ws.map((w) => Promise.race([w.evaluate(() => 1).then(() => "ok"), new Promise((res) => setTimeout(() => res("STUCK"), 3000))]).then((s) => ({ url: w.url().slice(-50), s }))));
    }
    await page.waitForTimeout(1000);
  }
  await brightness("2_channels");
  out.steps.levels = await levelsSeen(frame).catch((e) => String(e));
  say(`levels: fit ${out.steps.levels?.fitMs} ms, calibrated ${out.steps.levels?.calibratedMs} ms after the first click`);
  out.steps.channelRequests = (await pyRecent(page)).filter((r) => r.at >= since - 2000).filter((r) => /channel_stats|gmm|display_contrast/.test(r.path)).map((r) => ({ p: r.path.slice(0, 80), ms: r.ms, py: r.pyMs, at: r.at }));
  out.steps.stepsBeforeReload = await page.evaluate(() => window.PlexoraPython.steps().slice(-500)).catch(() => null);
  out.steps.recentBeforeReload = (await pyRecent(page)).slice(-80).map((r) => ({ p: r.path.slice(0, 80), ms: r.ms, py: r.pyMs, at: r.at, status: r.status }));
  say(`channels: ${JSON.stringify(out.steps.channelRequests.map((r) => r.ms))}`);
  // Reload.
  out.steps.keptDumpBefore = await keptDump();
  out.steps.builds = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null)).catch((e) => String(e));
  out.steps.keptBefore = await page.evaluate(async () => { try { const c = await caches.open("plexora-kept"); return (await c.keys()).map((r) => new URL(r.url).pathname + new URL(r.url).search).filter((u) => /stats|gmm|image$|channel_list/.test(u)); } catch (e) { return String(e); } });
  say("reload"); t = Date.now();
  if (args.includes("--key-reload")) {
    // ⌘R inside the app: the frame reloads, the page and its Python stay.
    await frame.focus("body").catch(() => null);
    await page.keyboard.press("F5");
    await page.waitForTimeout(300);
  } else {
    await page.reload();
  }
  frame = await frameOf();
  const asked = [];
  for (let end = Date.now() + RELOAD_WATCH_MS; Date.now() < end;) {
    try { frame = await (await page.$("#plexora-app")).contentFrame(); } catch {}
    const btns = await frame.evaluate(() => [...document.querySelectorAll(".plexora-boot-actions button")].map((b) => b.textContent)).catch(() => []);
    if (btns.length) asked.push({ t: Date.now() - t, btns });
    const r = await sample(frame, "reload", null);
    if (r.b?.lit > 0.05 && !out.steps.reloadLitMs) { out.steps.reloadLitMs = Date.now() - t; say(`reload lit at ${out.steps.reloadLitMs}`); }
    if (out.steps.reloadLitMs && Date.now() - t > out.steps.reloadLitMs + 30000) break;
    await page.waitForTimeout(200);
  }
  out.steps.reloadAsked = asked;
  out.steps.keptDumpAfter = await keptDump();
  out.steps.early = await frame.evaluate(() => window.__plexoraEarly || null).catch((e) => String(e));
  out.steps.keptReport = await page.evaluate(() => (window.PlexoraKept && window.PlexoraKept.report ? window.PlexoraKept.report() : null)).catch((e) => String(e));
  await brightness("3_reload");
  say(`reload: lit ${out.steps.reloadLitMs ?? "NEVER"}`);
  out.steps.recentAfterReload = (await pyRecent(page)).slice(-80).map((r) => ({ p: r.path.slice(0, 80), ms: r.ms, py: r.pyMs, at: r.at }));
  out.steps.stepsAfterReload = await page.evaluate(() => window.PlexoraPython.steps().slice(-40)).catch(() => null);
  out.steps.picksAfterReload = await page.evaluate(async () => (await window.PlexoraPicks.list()).map((p) => ({ id: p.id, files: (p.files || []).length }))).catch((e) => String(e));
} catch (e) { out.fatal = String(e).slice(0, 500); say("FATAL " + out.fatal); await brightness("fatal"); }
finally { writeFileSync(outFile, JSON.stringify(out, null, 1)); await browser.context.close(); if (served) served.stop(); }
