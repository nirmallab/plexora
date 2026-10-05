// Frame-stepped capture of the live Plexora viewer, driven by choreo.js.
// node record.mjs [--from N] [--to N] [--dsf 1|2] [--name out] [--preview K]
import { chromium } from "playwright";
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, "out");
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const DSF = Number(arg("dsf", "1"));
const FROM = Number(arg("from", "0"));
const NAME = arg("name", `seg_${FROM}`);
const PREVIEW = Number(arg("preview", "15"));
const URL = "http://127.0.0.1:8791/lsp11385";
const ART = path.join(process.env.HOME, "Library/Application Support/plexora/.agent/artifacts/lsp11385");
const FF = fs.readdirSync(path.join(HERE, "pylib/imageio_ffmpeg/binaries")).find((f) => f.startsWith("ffmpeg"));
const FFMPEG = path.join(HERE, "pylib/imageio_ffmpeg/binaries", FF);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

fs.mkdirSync(path.join(OUT, "preview"), { recursive: true });
const log = fs.createWriteStream(path.join(OUT, `${NAME}.frames.jsonl`));

const browser = await chromium.launch({
  channel: "chrome", headless: false,
  args: ["--force-color-profile=srgb", "--window-size=1920,1080", "--hide-scrollbars",
         "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
         "--disable-backgrounding-occluded-windows", "--ignore-gpu-blocklist"],
});
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 }, deviceScaleFactor: DSF, colorScheme: "dark",
  reducedMotion: "no-preference", serviceWorkers: "block",
});
const page = await context.newPage();
page.on("pageerror", (e) => console.log("PAGEERR", e.message));
page.on("console", (m) => { if (m.type() === "error") console.log("CONSOLE", m.text().slice(0, 300)); });

// Plexora AI's gateway is never reached: the launcher's calls are answered here.
const json = (route, body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
await page.route(/\/ai\/v1\/balance/, (r) => json(r, {
  available_credits: 2480,
  estimates: { gating: { units: 8, unit: "marker", credits: 96, affordable: true },
               qc: { units: 40, unit: "channel", credits: 140, affordable: true } },
}));
await page.route(/\/ai\/v1\/runs\?limit/, (r) => json(r, { runs: [] }));
await page.route(/\/ai\/v1\/runs$/, (r) => json(r, { run_id: "air_demo", job_id: "job_demo" }));
await page.route(/\/ai\/v1\/runs\/air_demo/, (r) => json(r, { run_id: "air_demo", status: "running" }));
// The agent card's evidence thumbnails: the real session's own pictures.
await page.route(/\/agent\/v1\/captures\//, (r) => {
  const id = r.request().url().split("/").pop().split("?")[0];
  const f = path.join(ART, `${id}.png`);
  return fs.existsSync(f) ? r.fulfill({ path: f, contentType: "image/png" }) : r.fulfill({ status: 404 });
});

await page.clock.install({ time: new Date("2026-10-04T09:00:00Z") });
await page.addInitScript({ path: path.join(HERE, "choreo.js") });
await page.goto(URL);
await page.evaluate(() => window.__plexoraReady);
await sleep(1500);
await page.addStyleTag({ path: path.join(HERE, "overlay.css") });
console.log("prepare", JSON.stringify(await page.evaluate(() => window.__promo.prepare())));
const TOTAL = await page.evaluate(() => window.__promo.TOTAL);
const TO = Number(arg("to", String(TOTAL - 1)));

// Let the opening draw with the clock still running, then freeze it.
for (let i = 0; i < 200; i++) {
  const p = await page.evaluate(() => window.__promo.probe(false));
  if (p.fully && !p.jobs && !p.queue && !p.xhr && !p.gate) break;
  await sleep(100);
}
await sleep(1500);
const now0 = await page.evaluate(() => Date.now());
await page.clock.pauseAt(now0 + 5000);
let vt = 0;                     // virtual ms since the pause
const T = (n) => Math.round((n + 1) * 1000 / 30);

// CSS animations follow the virtual clock, not the wall.
const cdp = await context.newCDPSession(page);
const anims = new Map();        // id -> virtual start
cdp.on("Animation.animationStarted", (e) => { anims.set(e.animation.id, vt); });
cdp.on("Animation.animationCanceled", (e) => { anims.delete(e.id); });
await cdp.send("Animation.enable");
await cdp.send("Animation.setPlaybackRate", { playbackRate: 0 });
async function seekAnimations() {
  const groups = new Map();
  for (const [id, start] of anims) {
    const t = Math.max(0, vt - start);
    if (!groups.has(t)) groups.set(t, []);
    groups.get(t).push(id);
  }
  for (const [t, ids] of groups) {
    try { await cdp.send("Animation.seekAnimations", { animations: ids, currentTime: t }); }
    catch (e) { for (const id of ids) { try { await cdp.send("Animation.seekAnimations", { animations: [id], currentTime: t }); } catch (e2) { anims.delete(id); } } }
  }
}

async function tick(ms) { if (ms > 0) { await page.clock.runFor(ms); vt += ms; } }
const quiet = (p) => p.fully && !p.jobs && !p.queue && !p.xhr && !p.gate && !p.cue && !p.autoLeveling;

async function settle(deadlineMs) {
  const t0 = Date.now();
  let polls = 0, ticks = 0, idleSince = Date.now(), p;
  for (;;) {
    p = await page.evaluate(() => window.__promo.probe(true));
    polls++;
    if (quiet(p)) return { ok: true, ms: Date.now() - t0, polls, ticks, p };
    if (Date.now() - t0 > deadlineMs) return { ok: false, ms: Date.now() - t0, polls, ticks, p };
    // Something waits on the page's (frozen) timers rather than the network: let 16 ms pass.
    const network = p.jobs || p.queue || p.xhr || p.fetch;
    if (network) idleSince = Date.now();
    else if (Date.now() - idleSince > 150 && ticks < 12) { await tick(16); ticks++; idleSince = Date.now(); }
    await sleep(network ? 20 : 30);
  }
}

// ffmpeg: a 1080p master (lanczos from the frames) and, at DSF 2, the native 4K.
let ff = null;
if (TO >= FROM) {
  const vf = "scale=1920:1080:flags=lanczos,setsar=1,format=yuv420p";
  const a = ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", "30", "-i", "-"];
  if (DSF > 1) {
    a.push("-filter_complex", `[0:v]split=2[a][b];[a]${vf}[hd];[b]scale=3840:2160:flags=lanczos,setsar=1,format=yuv420p[uhd]`,
      "-map", "[hd]", "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", path.join(OUT, `${NAME}-1080p.mp4`),
      "-map", "[uhd]", "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", path.join(OUT, `${NAME}-4k.mp4`));
  } else {
    a.push("-vf", vf, "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", path.join(OUT, `${NAME}-1080p.mp4`));
  }
  ff = spawn(FFMPEG, a, { stdio: ["pipe", "inherit", "inherit"] });
}
const write = (buf) => new Promise((res) => { if (!ff.stdin.write(buf)) ff.stdin.once("drain", res); else res(); });

const started = Date.now();
for (let n = 0; n <= TO; n++) {
  const capture = n >= FROM;
  const fx = await page.evaluate((k) => window.__promo.step(k), n);
  if (fx.mouse) await page.mouse.move(fx.mouse[0], fx.mouse[1]);
  if (fx.click) { await page.mouse.down(); await page.mouse.up(); }
  if (fx.type) await page.keyboard.type(fx.type);
  await tick(16);
  const s = capture ? await settle(12000) : { ok: true, ms: 0, polls: 0, ticks: 0 };
  await tick(T(n) - vt);
  if (capture) {
    // one more drawn frame at the final time, then the animations to match it
    const p = await page.evaluate(() => window.__promo.probe(true));
    await seekAnimations();
    // The screen camera: a crop of the page, scaled to the output by ffmpeg.
    const [cx, cy, z] = fx.scam || [960, 540, 1];
    const w = 1920 / z, h = 1080 / z;
    const q = 1 / DSF;   // snap to device pixels
    const x = Math.round(Math.min(Math.max(cx - w / 2, 0), 1920 - w) / q) * q;
    const y = Math.round(Math.min(Math.max(cy - h / 2, 0), 1080 - h) / q) * q;
    const buf = await page.screenshot({ type: "png", clip: { x, y, width: Math.round(w / q) * q, height: Math.round(h / q) * q } });
    await write(buf);
    if (PREVIEW && n % PREVIEW === 0) fs.writeFileSync(path.join(OUT, "preview", `f${String(n).padStart(5, "0")}.png`), buf);
    log.write(JSON.stringify({ n, vt, ok: s.ok, ms: s.ms, polls: s.polls, ticks: s.ticks, block: s.ok ? undefined : s.p, fully: p.fully }) + "\n");
    if (!s.ok) console.log("frame", n, "not settled", JSON.stringify(s.p));
    if (n % 30 === 0) {
      const el = (Date.now() - started) / 1000;
      console.log(`frame ${n}/${TO}  ${(el / (n - FROM + 1)).toFixed(2)} s/frame  vt=${vt}`);
    }
  }
}
if (ff) { ff.stdin.end(); await new Promise((r) => ff.on("close", r)); }
log.end();
await context.close();
await browser.close();
console.log("done", NAME, ((Date.now() - started) / 1000).toFixed(0), "s");
