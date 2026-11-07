// The website's browser node against Plexora's Python, tile for tile: each
// fixture of node_fixtures.py is mounted as a pick would mount it, imported,
// and then every HD tile of every channel and level, and every tile of the
// mask's levels, is asked of both -- the page's own reader
// (PlexoraNode.answer) and the worker's Flask app (PlexoraPython.fetch) --
// and the two PNGs are decoded here and compared sample for sample. A part
// the node must leave to Python (refused) is checked to be answered by Python
// through the page's backend.
// Prints JSON per fixture and part (image, labels): served or refused (and
// why), tiles compared, mismatches, and the per-tile times of both paths.
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
    // A small Buffer is a view into a shared pool: hand UPNG its own bytes.
    const bytes = Buffer.from(base64, "base64");
    const png = UPNG.decode(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    return { width: png.width, height: png.height, depth: png.depth, ctype: png.ctype,
             data: new Uint8Array(png.data) };
};

//: How many samples differ, and by how much at most (16-bit grey, or bytes).
const compare = (a, b) => {
    let differ = 0;
    let worst = 0;
    const wide = a.depth === 16;
    for (let i = 0; i + (wide ? 1 : 0) < a.data.length; i += wide ? 2 : 1) {
        const va = wide ? (a.data[i] << 8) | a.data[i + 1] : a.data[i];
        const vb = wide ? (b.data[i] << 8) | b.data[i + 1] : b.data[i];
        if (va !== vb) {
            differ += 1;
            worst = Math.max(worst, Math.abs(va - vb));
        }
    }
    return { differ, worst };
};

const percentile = (values, p) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return Math.round(sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))] * 10) / 10;
};

//: Both answers to one tile, timed in the page.
const askBoth = (page, url) => page.evaluate(async (u) => {
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
    return { pyStatus: py.status, pyBody, pyMs, jsBody, jsMs, jsError };
}, url);

//: Every tile of `urls` from both, compared.
async function comparePart(page, urls) {
    const times = { python: [], browser: [] };
    const mismatches = [];
    let maxDiff = 0;
    let compared = 0;
    for (const url of urls) {
        const pair = await askBoth(page, url);
        times.python.push(pair.pyMs);
        if (pair.jsBody) times.browser.push(pair.jsMs);
        if (pair.pyStatus !== 200 || !pair.jsBody) {
            mismatches.push({ url, why: `python ${pair.pyStatus}, browser `
                                      + (pair.jsError ? `failed: ${pair.jsError}` : pair.jsBody ? "answered" : "declined") });
            continue;
        }
        let a;
        let b;
        try {
            a = decode(pair.pyBody);
            b = decode(pair.jsBody);
        } catch (error) {
            const head = (body) => Buffer.from(body, "base64").subarray(0, 40).toString("latin1");
            mismatches.push({ url, why: `${error.message || error}: python ${JSON.stringify(head(pair.pyBody))}, `
                                      + `browser ${JSON.stringify(head(pair.jsBody))}` });
            continue;
        }
        compared += 1;
        if (a.width !== b.width || a.height !== b.height || a.depth !== b.depth || a.ctype !== b.ctype) {
            mismatches.push({ url, why: `python ${a.width}x${a.height} d${a.depth} c${a.ctype}, `
                                      + `browser ${b.width}x${b.height} d${b.depth} c${b.ctype}` });
            continue;
        }
        const { differ, worst } = compare(a, b);
        maxDiff = Math.max(maxDiff, worst);
        if (differ) mismatches.push({ url, why: `${differ} samples differ, by up to ${worst}` });
    }
    return {
        tiles: urls.length, compared, mismatchCount: mismatches.length, mismatches: mismatches.slice(0, 8), maxDiff,
        ms: {
            python: { p50: percentile(times.python, 0.5), p95: percentile(times.python, 0.95) },
            browser: { p50: percentile(times.browser, 0.5), p95: percentile(times.browser, 0.95) },
        },
        ok: mismatches.length === 0 && compared === urls.length,
    };
}

//: A refused part's tile, through the page's backend: Python's, and 200.
const fallback = (page, url) => page.evaluate(async (u) => {
    const response = await window.PlexoraBackend.answer(new Request(location.origin + u));
    return { status: response.status, by: response.headers.get("X-Plexora-Served-By") };
}, url);

const grid = (height, width, [tw, th]) => {
    const tiles = [];
    for (let ty = 0; ty * th < height; ty += 1) {
        for (let tx = 0; tx * tw < width; tx += 1) tiles.push(`${tx}_${ty}`);
    }
    return tiles;
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
        const files = made[name].map((f) => path.basename(f));
        const record = { files };
        out[name] = record;
        const where = await page.evaluate(async ([n, fs]) => {
            const picked = await Promise.all(fs.map(async (f) => new File(
                [await (await fetch(`/fixture/${f}`)).blob()], f)));
            const mounted = await window.PlexoraPython.mount(`/picked/np-${n}`, picked);
            mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, picked[i]));
            return mounted;
        }, [name, files]);
        const imported = await frame.evaluate(async (ps) => {
            const response = await fetch("import/sample", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paths: ps }),
            });
            return { status: response.status, body: await response.json() };
        }, where);
        const project = imported.body && imported.body.name;
        if (!project) {
            record.error = `import ${imported.status}: ${JSON.stringify(imported.body).slice(0, 300)}`;
            continue;
        }
        record.project = project;
        // A mask is converted or checked by a job after the import: what
        // the node may serve is asked once it is done.
        const started = Date.now();
        record.mask = await frame.evaluate(async (n) => {
            for (let i = 0; i < 240; i += 1) {
                const status = await (await fetch(`get_segmentation_status?datasource=${encodeURIComponent(n)}`)).json();
                if (status.status !== "pending" && status.status !== "running") return status.status;
                await new Promise((resolve) => setTimeout(resolve, 500));
            }
            return "timed out";
        }, project);
        record.maskMs = Date.now() - started;
        const opened = Date.now();
        const entry = await page.evaluate(async (n) => {
            window.PlexoraNode.revalidate();
            const held = await window.PlexoraNode.project(n);
            return { spec: held.spec || null,
                     image: held.image.refused || null, labels: held.labels.refused || null };
        }, project);
        record.openMs = Date.now() - opened;
        const spec = entry.spec;
        const base = `/generated/data/${encodeURIComponent(project)}`;
        if (!spec) {
            record.ok = false;
            record.error = entry.image;
            continue;
        }
        // The image: every HD tile of every channel and level.
        if (entry.image) {
            record.image = { refused: entry.image, fallback: await fallback(page, `${base}/ch_0/0/0_0.png?q=hd`) };
            record.image.ok = record.image.fallback.status === 200 && !record.image.fallback.by;
        } else {
            const urls = spec.levels.flatMap(([channels, height, width], level) =>
                Array.from({ length: channels }, (_, c) => grid(height, width, spec.tile)
                    .map((t) => `${base}/ch_${c}/${level}/${t}.png?q=hd`)).flat());
            record.image = { levels: spec.levels, ...(await comparePart(page, urls)) };
        }
        // The mask: every tile of the levels it holds.
        if (spec.labels.reason === "no mask") {
            record.labels = null;
        } else if (entry.labels) {
            record.labels = { refused: entry.labels, fallback: await fallback(page, `${base}/mask/0/0_0.png?v=2`) };
            record.labels.ok = record.labels.fallback.status === 200 && !record.labels.fallback.by;
        } else {
            const urls = spec.labels.levels.flatMap(([level, height, width]) =>
                grid(height, width, spec.tile).map((t) => `${base}/mask/${level}/${t}.png?v=2`));
            record.labels = { levels: spec.labels.levels, dtype: spec.labels.dtype, ...(await comparePart(page, urls)) };
        }
        record.ok = record.image.ok && (!record.labels || record.labels.ok);
    }
    out.errors = errors.slice(0, 12);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
