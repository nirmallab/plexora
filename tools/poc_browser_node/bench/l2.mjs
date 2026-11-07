// L2's de-risking record: openslide-js (OpenSlide 4.0.0 compiled to
// WebAssembly, @computationalpathologygroup/openslide-js) in headless Chrome
// against openslide-python (native OpenSlide) on OpenSlide's public test slides.
//
//   node bench/l2.mjs --pkg DIR --fixtures DIR --truth DIR --out DIR [--workers 4] [--only a,b]
//
// --pkg       the unpacked npm package (its dist/esm is served at /osjs/)
// --fixtures  the slides (served at /fx/), laid out as OpenSlide's index has them
// --truth     bench/truth_l2.py's output: truth.json and <name>/<i>.npy
// --out       where l2.json and js/<name>/<i>.rgba (the page's RGBA bytes) go;
//             bench/compare_l2.py then holds them to the truth.
//
// Per slide: the files fetched into File objects (not timed as the open), the
// slide opened (a multi-file slide as VirtualFile entries relative to the
// folder its index file is in, as a folder pick gives them), every truth
// region read one at a time, then the same regions with 8 in flight.
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launch, playwright } from "../../../tests/browser/lib.mjs";
import { spawn } from "node:child_process";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..", "..");
const PYTHON = process.env.PLEXORA_PYTHON || "C:/Users/aj/.conda/envs/plexora/python.exe";
const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const PKG = opt("--pkg");
const FIXTURES = opt("--fixtures");
const TRUTH = opt("--truth");
const OUT = opt("--out");
const WORKERS = Number(opt("--workers", "4"));
const ONLY = opt("--only", null)?.split(",");
//: How many opens of one slide the spread read uses (one per decode worker).
const SPREAD = Number(opt("--spread", String(WORKERS)));

function serve() {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tools", "browser_build", "serve.py"),
            "--tree", `osjs=${PKG}`, "--tree", `fx=${FIXTURES}`, "--tree", `l2=${path.join(HERE, "l2_page")}`],
        { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) resolve({ port: JSON.parse(buffer.split("\n")[0]).port, stop: () => child.kill() });
        });
        child.on("exit", (code) => reject(new Error(`serve.py exited ${code}`)));
    });
}

function walk(dir, prefix = "") {
    const out = [];
    for (const name of readdirSync(dir)) {
        const full = path.join(dir, name);
        if (statSync(full).isDirectory()) out.push(...walk(full, `${prefix}${name}/`));
        else out.push(`${prefix}${name}`);
    }
    return out;
}

//: The files a slide is opened from, as [{path (inside the pick), url}], and
//: whether it is a multi-file slide.
function filesOf(rel) {
    const full = path.join(FIXTURES, rel);
    const dir = path.dirname(full);
    const relDir = path.posix.dirname(rel.replaceAll("\\", "/"));
    const base = path.basename(full);
    const url = (inside) => `/fx/${relDir}/${inside}`.replace(/\/+/g, "/");
    if (base.toLowerCase().endsWith(".mrxs")) {
        const stem = base.slice(0, -5);
        const inside = walk(path.join(dir, stem), `${stem}/`);
        return { multi: true, main: base, files: [base, ...inside].map((p) => ({ path: p, url: url(p) })) };
    }
    if (base.toLowerCase().endsWith(".dcm")) {
        const dcm = readdirSync(dir).filter((n) => n.toLowerCase().endsWith(".dcm"));
        return { multi: true, main: base, files: dcm.map((p) => ({ path: p, url: url(p) })) };
    }
    return { multi: false, main: base, files: [{ path: base, url: url(base) }] };
}

const pct = (values, q) => {
    const sorted = [...values].sort((a, b) => a - b);
    const at = (sorted.length - 1) * q;
    const lo = Math.floor(at);
    return sorted[lo] + (sorted[Math.min(lo + 1, sorted.length - 1)] - sorted[lo]) * (at - lo);
};

const truth = JSON.parse(readFileSync(path.join(TRUTH, "truth.json"), "utf8"));
mkdirSync(OUT, { recursive: true });
//: Another engine for the function check (`--browser firefox|webkit`):
//: Playwright's own builds.
const ENGINE = opt("--browser", "chrome");

async function otherEngine(name) {
    const engines = await playwright();
    const instance = await engines[name].launch({ headless: true });
    const context = await instance.newContext();
    return { context, close: () => instance.close(), version: instance.version() };
}

const server = await serve();
const browser = ENGINE === "chrome" ? await launch({}) : await otherEngine(ENGINE);
const record = { openslideJs: null, chrome: null, workers: WORKERS, slides: {} };
try {
    const page = await browser.context.newPage();
    const logs = [];
    page.on("console", (m) => { if (["error", "warning"].includes(m.type())) logs.push(m.text().slice(0, 300)); });
    page.on("pageerror", (e) => logs.push(`pageerror: ${e}`));
    await page.goto(`http://127.0.0.1:${server.port}/l2/index.html`);
    await page.waitForFunction(() => window.l2);
    record.engine = ENGINE;
    record.chrome = browser.version || browser.context.browser()?.version() || await page.evaluate(() => navigator.userAgent);
    record.init = await page.evaluate((workers) => window.l2.init({ workers }), WORKERS);
    record.openslideJs = record.init.version;
    for (const [name, entry] of Object.entries(truth.slides)) {
        if (ONLY && !ONLY.includes(name)) continue;
        const spec = filesOf(entry.path);
        const row = { path: entry.path, multi: spec.multi, files: spec.files.length };
        record.slides[name] = row;
        try {
            row.fetch = await page.evaluate((files) => window.l2.fetch(files), spec.files);
            row.open = await page.evaluate((s) => window.l2.open(s), { main: spec.main, multi: spec.multi });
        } catch (error) {
            row.error = String(error.message || error).split("\n")[0].slice(0, 400);
            console.error(name, "open failed:", row.error);
            await page.evaluate(() => window.l2.close()).catch(() => {});
            continue;
        }
        const regions = (entry.tiles || []).map((t) => ({ i: t.i, level: t.level, x: t.x, y: t.y, w: t.w, h: t.h }));
        const dir = path.join(OUT, "js", name);
        mkdirSync(dir, { recursive: true });
        const tiles = [];
        try {
            for (let k = 0; k < regions.length; k += 6) {
                const part = await page.evaluate(([r]) => window.l2.read(r, { keep: true }), [regions.slice(k, k + 6)]);
                for (const t of part) {
                    writeFileSync(path.join(dir, `${String(t.i).padStart(3, "0")}.rgba`), Buffer.from(t.bytes, "base64"));
                    tiles.push({ i: t.i, ms: t.ms, w: t.w, h: t.h, sha: t.sha });
                }
            }
            // Warm: the same regions again, one at a time, nothing kept.
            const warm = await page.evaluate(([r]) => window.l2.read(r, { keep: false }), [regions]);
            row.tiles = tiles;
            const cold = tiles.map((t) => t.ms);
            row.tileMs = { p50: pct(cold, 0.5), p95: pct(cold, 0.95) };
            row.tileMsWarm = { p50: pct(warm.map((t) => t.ms), 0.5), p95: pct(warm.map((t) => t.ms), 0.95) };
            row.parallel8 = await page.evaluate(([r]) => window.l2.parallel(r, 8), [regions]);
            // The timing set (viewer-sized tiles over the tissue), as the truth
            // timed it: first touch, then again, one at a time; then spread over
            // the slide opened on SPREAD workers, all in flight.
            const tour = (entry.tour || []).map((t, i) => ({ i, ...t }));
            if (tour.length) {
                const first = await page.evaluate(([r]) => window.l2.read(r, { keep: false }), [tour]);
                const again = await page.evaluate(([r]) => window.l2.read(r, { keep: false }), [tour]);
                const stats = (list) => ({ p50: pct(list.map((t) => t.ms), 0.5), p95: pct(list.map((t) => t.ms), 0.95),
                                           total_ms: list.reduce((a, t) => a + t.ms, 0), tiles: list.length });
                row.tourFirst = stats(first);
                row.tourAgain = stats(again);
                row.openMany = await page.evaluate(([k, s]) => window.l2.openMany(k, s),
                                                   [SPREAD - 1, { main: spec.main, multi: spec.multi }]);
                const spread = await page.evaluate(([r]) => window.l2.spread(r), [tour]);
                row.tourSpread = { ms: spread.ms, tiles: spread.tiles, slides: spread.slides,
                                   msPerTile: spread.ms / spread.tiles };
            }
        } catch (error) {
            row.error = `read: ${String(error.message || error).split("\n")[0].slice(0, 400)}`;
            console.error(name, row.error);
        }
        await page.evaluate(() => window.l2.close());
        console.error(name, row.open ? `open ${row.open.ms.toFixed(0)} ms` : "",
                      row.tileMs ? `tile p50 ${row.tileMs.p50.toFixed(1)} p95 ${row.tileMs.p95.toFixed(1)}` : "",
                      row.tourFirst ? `tour p50 ${row.tourFirst.p50.toFixed(1)} p95 ${row.tourFirst.p95.toFixed(1)} again ${row.tourAgain.p50.toFixed(1)} spread ${row.tourSpread.msPerTile.toFixed(1)}/tile` : "");
    }
    record.logs = logs.slice(0, 40);
} finally {
    await browser.close();
    server.stop();
}
writeFileSync(path.join(OUT, "l2.json"), JSON.stringify(record, null, 1));
console.log(path.join(OUT, "l2.json"));
