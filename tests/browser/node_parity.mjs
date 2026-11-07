// The website's browser node against Plexora's Python, tile for tile: each
// fixture of node_fixtures.py is mounted as a pick would mount it, imported,
// and then every HD tile of every channel and level is asked of both -- the
// page's own reader (PlexoraNode.answer) and the worker's Flask app
// (PlexoraPython.fetch) -- and the two PNGs are decoded here and compared
// sample for sample. A fixture the node must leave to Python (refused) is
// checked to be answered by Python through the page's backend.
// Prints JSON per fixture: served or refused (and why), tiles compared,
// mismatches, and the per-tile times of both paths.
//
//   node tests/browser/node_parity.mjs [--dir FIXTURE_DIR] [--only a,b]
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const dir = opt("--dir", "C:/plexora-bench/browser_node/node_fixtures");
const only = opt("--only", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));

const require = createRequire(path.join(REPO, "plexora", "client", "package.json"));
const UPNG = require("upng-js");

const made = JSON.parse(execFileSync(PYTHON, [path.join(REPO, "tests", "browser", "node_fixtures.py"), dir],
                                     { encoding: "utf8" }));
const names = Object.keys(made).filter((n) => !only || only.split(",").includes(n));

const decode = (base64) => {
    const png = UPNG.decode(Buffer.from(base64, "base64").buffer);
    return { width: png.width, height: png.height, depth: png.depth, ctype: png.ctype,
             data: new Uint8Array(png.data) };
};

const percentile = (values, p) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return Math.round(sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))] * 10) / 10;
};

const server = await startServer(["--site", site, "--tree", `fixture=${dir}`]);
const browser = await launch({ headed: args.includes("--headed") });
const out = {};
try {
    const page = await browser.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const frame = await appFrame(page);
    for (const name of names) {
        const file = path.basename(made[name]);
        const record = { file };
        out[name] = record;
        const where = await page.evaluate(async ([n, f]) => {
            const picked = new File([await (await fetch(`/fixture/${f}`)).blob()], f);
            const [mounted] = await window.PlexoraPython.mount(`/picked/np-${n}`, [picked]);
            window.PlexoraPicks.adopt(mounted, picked);
            return mounted;
        }, [name, file]);
        const imported = await frame.evaluate(async (p) => {
            const response = await fetch("import/sample", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paths: [p] }),
            });
            return { status: response.status, body: await response.json() };
        }, where);
        const project = imported.body && imported.body.name;
        if (!project) {
            record.error = `import ${imported.status}: ${JSON.stringify(imported.body).slice(0, 300)}`;
            continue;
        }
        record.project = project;
        const opened = Date.now();
        const entry = await page.evaluate(async (n) => {
            const held = await window.PlexoraNode.project(n);
            return { spec: held.spec || null, refused: held.refused || null,
                     levels: held.geometry ? held.geometry.levels.map((l) => [l.width, l.height]) : null };
        }, project);
        record.openMs = Date.now() - opened;
        record.refused = entry.refused;
        if (!entry.spec || !entry.spec.served || entry.refused) {
            // Left to Python: the page's backend must still answer the tile.
            const fallback = await page.evaluate(async (n) => {
                const url = `${location.origin}/generated/data/${encodeURIComponent(n)}/ch_0/0/0_0.png?q=hd`;
                const response = await window.PlexoraBackend.answer(new Request(url));
                return { status: response.status, by: response.headers.get("X-Plexora-Served-By") };
            }, project);
            record.fallback = fallback;
            record.ok = fallback.status === 200 && !fallback.by;
            continue;
        }
        const { levels, tile: [tw, th] } = entry.spec;
        record.levels = levels;
        record.tile = [tw, th];
        const urls = [];
        for (let level = 0; level < levels.length; level += 1) {
            const [channels, height, width] = levels[level];
            for (let c = 0; c < channels; c += 1) {
                for (let ty = 0; ty * th < height; ty += 1) {
                    for (let tx = 0; tx * tw < width; tx += 1) {
                        urls.push(`/generated/data/${encodeURIComponent(project)}/ch_${c}/${level}/${tx}_${ty}.png?q=hd`);
                    }
                }
            }
        }
        const times = { python: [], browser: [] };
        const mismatches = [];
        let maxDiff = 0;
        let compared = 0;
        for (const url of urls) {
            const pair = await page.evaluate(async (u) => {
                const b64 = async (response) => {
                    const bytes = new Uint8Array(await response.arrayBuffer());
                    let text = "";
                    for (let i = 0; i < bytes.length; i += 0x8000) {
                        text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                    }
                    return btoa(text);
                };
                let t0 = performance.now();
                const py = await window.PlexoraPython.fetch(u);
                const pyBody = await b64(py);
                const pyMs = performance.now() - t0;
                t0 = performance.now();
                let js = null;
                let jsError = null;
                try {
                    js = await window.PlexoraNode.answer(new Request(location.origin + u));
                } catch (error) {
                    jsError = String(error.message || error).split(" | ")[0].slice(0, 200);
                }
                const jsBody = js ? await b64(js) : null;
                const jsMs = performance.now() - t0;
                return { pyStatus: py.status, pyBody, pyMs, jsBody, jsMs, jsError,
                         jsBy: js && js.headers.get("X-Plexora-Served-By") };
            }, url);
            times.python.push(pair.pyMs);
            if (pair.jsBody) times.browser.push(pair.jsMs);
            if (pair.pyStatus !== 200 || !pair.jsBody) {
                mismatches.push({ url, why: `python ${pair.pyStatus}, browser `
                                          + (pair.jsError ? `failed: ${pair.jsError}` : pair.jsBody ? "answered" : "declined") });
                continue;
            }
            const a = decode(pair.pyBody);
            const b = decode(pair.jsBody);
            compared += 1;
            if (a.width !== b.width || a.height !== b.height || a.depth !== b.depth || a.ctype !== b.ctype) {
                mismatches.push({ url, why: `python ${a.width}x${a.height} d${a.depth} c${a.ctype}, `
                                          + `browser ${b.width}x${b.height} d${b.depth} c${b.ctype}` });
                continue;
            }
            let differ = 0;
            let worst = 0;
            for (let i = 0; i + 1 < a.data.length; i += 2) {
                const va = (a.data[i] << 8) | a.data[i + 1];
                const vb = (b.data[i] << 8) | b.data[i + 1];
                if (va !== vb) {
                    differ += 1;
                    worst = Math.max(worst, Math.abs(va - vb));
                }
            }
            maxDiff = Math.max(maxDiff, worst);
            if (differ) mismatches.push({ url, why: `${differ} samples differ, by up to ${worst}` });
        }
        record.tiles = urls.length;
        record.compared = compared;
        record.mismatches = mismatches.slice(0, 8);
        record.mismatchCount = mismatches.length;
        record.maxDiff = maxDiff;
        record.ms = {
            python: { p50: percentile(times.python, 0.5), p95: percentile(times.python, 0.95) },
            browser: { p50: percentile(times.browser, 0.5), p95: percentile(times.browser, 0.95) },
        };
        record.ok = mismatches.length === 0 && compared === urls.length;
    }
    out.errors = errors.slice(0, 12);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
