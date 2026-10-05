// Frame-stepped capture of the live Plexora page, driven by pv.js + a storyboard.
//   node record.mjs [--config video.json] [--dsf 1|2|3] [--from N] [--to N]
//                   [--name out] [--preview K] [--4k]
// Run from a work directory holding a copy of this kit (see SKILL.md).
import { chromium } from "playwright";
import { spawn, execFileSync } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const flag = (k) => process.argv.includes(`--${k}`);
const CFG_PATH = path.resolve(arg("config", path.join(HERE, "video.json")));
const DIR = path.dirname(CFG_PATH);
const CFG = JSON.parse(fs.readFileSync(CFG_PATH, "utf8"));
const DSF = Number(arg("dsf", "1"));
const FROM = Number(arg("from", "0"));
const NAME = arg("name", CFG.name || "take");
const PREVIEW = Number(arg("preview", "30"));
const UHD = flag("4k");
const OUT = path.join(DIR, "out");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findFfmpeg() {
  if (process.env.PV_FFMPEG) return process.env.PV_FFMPEG;
  const bin = path.join(DIR, "pylib/imageio_ffmpeg/binaries");
  if (fs.existsSync(bin)) {
    const f = fs.readdirSync(bin).find((x) => x.startsWith("ffmpeg"));
    if (f) return path.join(bin, f);
  }
  try { execFileSync("ffmpeg", ["-version"], { stdio: "ignore" }); return "ffmpeg"; } catch (e) {}
  throw new Error("no ffmpeg with libx264: pip install imageio-ffmpeg --target pylib (see SKILL.md)");
}
const FFMPEG = findFfmpeg();

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
page.on("console", (m) => { if (m.type() === "error" || /^pv[: ]/.test(m.text())) console.log("CONSOLE", m.text().slice(0, 300)); });

// Network stubs (AI gateway, remote hosts...): a module exporting default async (page, cfg).
if (CFG.stubs) await (await import(pathToFileURL(path.resolve(DIR, CFG.stubs)).href)).default(page, CFG);

// The fake clock must exist before the bundle: OSD captures rAF and Date.now at load.
await page.clock.install({ time: new Date("2026-01-01T09:00:00Z") });
await page.addInitScript({ path: path.join(HERE, "pv.js") });
await page.addInitScript({ path: path.resolve(DIR, CFG.storyboard || "storyboard.js") });
await page.goto(CFG.url);
if (CFG.ready) await page.evaluate(`(async () => { await (${CFG.ready}); })()`);
await sleep(1500);
await page.addStyleTag({ path: path.join(HERE, "pv.css") });
console.log("prepare", JSON.stringify(await page.evaluate(() => window.__promo.prepare())));
const TOTAL = await page.evaluate(() => window.__promo.TOTAL);
const TO = Math.min(Number(arg("to", String(TOTAL - 1))), TOTAL - 1);

// One document per take: a navigation would wipe pv.js's state mid-video.
let navigated = false;
page.on("framenavigated", (f) => { if (f === page.mainFrame()) navigated = true; });

for (let i = 0; i < 200; i++) {
  const p = await page.evaluate(() => window.__promo.probe(false));
  if (p.fully && !p.jobs && !p.queue && !p.xhr && !p.app) break;
  await sleep(100);
}
await sleep(1500);
const now0 = await page.evaluate(() => Date.now());
await page.clock.pauseAt(now0 + 5000);   // flushes debounces and start-up timers
let vt = 0;                               // virtual ms since the pause
let shift = 0;                            // virtual ms spent in holds
const T = (n) => Math.round((n + 1) * 1000 / 30) + shift;

// CSS animations follow the virtual clock, not the wall.
const cdp = await context.newCDPSession(page);
const anims = new Map();
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
const quiet = (p) => p.fully && !p.jobs && !p.queue && !p.xhr && !p.app && !p.cue && !p.autoLeveling;
async function settle(deadlineMs) {
  const t0 = Date.now();
  let polls = 0, ticks = 0, idleSince = Date.now(), p;
  for (;;) {
    p = await page.evaluate(() => window.__promo.probe(true));
    polls++;
    if (quiet(p)) return { ok: true, ms: Date.now() - t0, polls, ticks, p };
    if (Date.now() - t0 > deadlineMs) return { ok: false, ms: Date.now() - t0, polls, ticks, p };
    // waiting on the page's (frozen) timers rather than the network: let 16 ms pass
    const network = p.jobs || p.queue || p.xhr || p.fetch;
    if (network) idleSince = Date.now();
    else if (Date.now() - idleSince > 150 && ticks < 12) { await tick(16); ticks++; idleSince = Date.now(); }
    await sleep(network ? 20 : 30);
  }
}

let ff = null;
if (TO >= FROM) {
  const enc = (crf, file) => ["-c:v", "libx264", "-preset", "slow", "-crf", String(crf), "-colorspace", "bt709",
    "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", path.join(OUT, file)];
  const vf = "scale=1920:1080:flags=lanczos,setsar=1,format=yuv420p";
  const a = ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", "30", "-i", "-"];
  if (UHD) {
    a.push("-filter_complex", `[0:v]split=2[a][b];[a]${vf}[hd];[b]scale=3840:2160:flags=lanczos,setsar=1,format=yuv420p[uhd]`,
      "-map", "[hd]", ...enc(15, `${NAME}-1080p.mp4`), "-map", "[uhd]", ...enc(16, `${NAME}-4k.mp4`));
  } else {
    a.push("-vf", vf, ...enc(15, `${NAME}-1080p.mp4`));
  }
  ff = spawn(FFMPEG, a, { stdio: ["pipe", "inherit", "inherit"] });
}
const write = (buf) => new Promise((res) => { if (!ff.stdin.write(buf)) ff.stdin.once("drain", res); else res(); });

const started = Date.now();
let bad = 0;
for (let n = 0; n <= TO; n++) {
  if (navigated) {
    // A same-document navigation (pushState, a hash: opening a tool does it)
    // keeps the runtime; only a real load wipes it.
    navigated = false;
    if (!(await page.evaluate(() => Boolean(window.__promo && window.PV)))) {
      throw new Error(`frame ${n}: the page navigated -- split the video into takes (SKILL.md)`);
    }
  }
  const capture = n >= FROM;
  const fx = await page.evaluate((k) => window.__promo.step(k), n);
  if (fx.mouse) await page.mouse.move(fx.mouse[0], fx.mouse[1]);
  if (fx.click) await page.mouse.click(fx.click.at[0], fx.click.at[1], { button: fx.click.button, clickCount: fx.click.count });
  if (fx.type) await page.keyboard.type(fx.type);
  if (fx.key) await page.keyboard.press(fx.key);
  if (fx.files) await page.setInputFiles(fx.files.sel, fx.files.paths.map((p) => path.resolve(DIR, p)));
  if (fx.hold) {
    // Real work (an import, a connection): run the clock in real time, film nothing.
    // The page clock follows the wall clock, so the app's own polling keeps pace.
    const h0 = Date.now();
    let last = h0;
    while (!(await page.evaluate(() => window.__promo.holdCheck()))) {
      if (Date.now() - h0 > fx.hold.max * 1000) throw new Error(`frame ${n}: wait "${fx.hold.label}" timed out`);
      await sleep(100);
      const d = Date.now() - last;
      last = Date.now();
      await tick(d);
      shift += d;
    }
    const ms = Date.now() - h0;
    await page.evaluate((m) => window.__promo.held(m), ms);
    console.log(`frame ${n}: held ${(ms / 1000).toFixed(1)} s for "${fx.hold.label}"`);
  }
  await tick(16);
  const s = capture ? await settle(12000) : { ok: true, ms: 0, polls: 0, ticks: 0 };
  await tick(T(n) - vt);
  if (!capture) continue;
  const p = await page.evaluate(() => window.__promo.probe(true));
  await seekAnimations();
  // the screen camera: a crop of the page, scaled to the output by ffmpeg
  const [cx, cy, z] = fx.scam || [960, 540, 1];
  const w = 1920 / z, h = 1080 / z, q = 1 / DSF;
  const x = Math.round(Math.min(Math.max(cx - w / 2, 0), 1920 - w) / q) * q;
  const y = Math.round(Math.min(Math.max(cy - h / 2, 0), 1080 - h) / q) * q;
  const buf = await page.screenshot({ type: "png", clip: { x, y, width: Math.round(w / q) * q, height: Math.round(h / q) * q } });
  await write(buf);
  if (PREVIEW && n % PREVIEW === 0) fs.writeFileSync(path.join(OUT, "preview", `f${String(n).padStart(5, "0")}.png`), buf);
  if (!s.ok) { bad++; console.log("frame", n, "not settled", JSON.stringify(s.p)); }
  log.write(JSON.stringify({ n, vt, ok: s.ok, ms: s.ms, polls: s.polls, ticks: s.ticks, z, block: s.ok ? undefined : s.p, fully: p.fully }) + "\n");
  if (n % 30 === 0) console.log(`frame ${n}/${TO}  ${((Date.now() - started) / 1000 / (n - FROM + 1)).toFixed(2)} s/frame`);
}
if (ff) { ff.stdin.end(); await new Promise((r) => ff.on("close", r)); }
log.end();
await context.close();
await browser.close();
console.log(`done ${NAME} ${((Date.now() - started) / 1000).toFixed(0)} s, ${bad} unsettled frame(s)`);
