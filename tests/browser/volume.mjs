// A z-stack on the website and on the desktop, side by side (plan L11): the
// files picked (a folder for an OME-Zarr), Plexora's own import, the viewer,
// the 3-D view entered as a user enters it, and then the volume routes the
// viewer asks -- info, the channel windows, the brick manifest, whole levels,
// bricks, a section and its tile, a plane and a projection, the 3-D objects --
// each answer saved, for tests/browser/volume_web.py `compare` to hold the
// website's to the desktop's byte for byte (a PNG tile: pixel for pixel).
// The 3-D view must have DRAWN: its canvas is read back (createImageBitmap)
// and its lit share recorded, and a screenshot kept -- a "ready" status alone
// can lie (memory: verify-benchmarks-by-pixels).
//
// The desktop arm is this checkout's Plexora in CPython
// (tools/bench3d/serve.py, a data root of its own) in the same Chrome.
// Timings per arm: import, open (2-D drawn), the 3-D view drawn, windows
// exact, and single-brick latency cold then warm. `--ops` also runs
// volume_web.py's OPS (isosurface, cell surfaces, object measurement) in the
// website's Python and in CPython with numba and without.
//
//   node tests/browser/volume.mjs --files IMAGE[,MASK] --out DIR
//        [--arms website,desktop] [--ops] [--bricks 8] [--headed] [--site DIR]
// Prints the record as JSON (also DIR/record.json); exits 1 when a check fails.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const files = (opt("--files", "") || "").split(",").filter(Boolean).map((p) => path.resolve(p));
const out = opt("--out", null);
const arms = opt("--arms", "website,desktop").split(",");
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
const brickCount = Number(opt("--bricks", "8"));
const runOps = args.includes("--ops");
const headed = args.includes("--headed");
//: Bodies larger than this are compared by their sha256 alone (not saved).
const SAVE_MAX = Number(opt("--save-max", String(48 << 20)));
if (!files.length || !out) throw new Error("--files IMAGE[,MASK] --out DIR");
mkdirSync(out, { recursive: true });
const hasMask = files.length > 1;
const names = files.map((f) => path.basename(f));
const HELPER = path.join(REPO, "tests", "browser", "volume_web.py");
const env = { ...process.env, PYTHONPATH: REPO };

const record = { files: names, arms: {}, checks: {} };
const check = (name, ok, detail) => { record.checks[name] = { ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) }; };
const pct = (values, q) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1) + 0.5))];
};

/** Every file under `dir`, relative, with forward slashes. */
function walk(dir, prefix = "") {
    const found = [];
    for (const entry of readdirSync(dir)) {
        const full = path.join(dir, entry);
        if (statSync(full).isDirectory()) found.push(...walk(full, `${prefix}${entry}/`));
        else found.push(`${prefix}${entry}`);
    }
    return found;
}

// -- the two arms' setups ------------------------------------------------------

async function websiteArm() {
    const server = await startServer(["--site", site]);
    const browser = await launch({ headed });
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.addInitScript(() => {
        window.__stages = [];
        const watch = () => {
            if (window.PlexoraPython && window.PlexoraPython.on) {
                window.PlexoraPython.on("stage", (event) => window.__stages.push({ ...event, at: Math.round(performance.now()) }));
            } else setTimeout(watch, 5);
        };
        watch();
    });
    const base = `http://127.0.0.1:${server.port}`;
    await page.goto(`${base}/?pydebug`);
    let frame = await appFrame(page);
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (!keep) return false;
        keep.click();
        return true;
    }, null, { timeout: 5000, polling: 200 }).catch(() => null);
    await page.evaluate(() => window.PlexoraPython.ready);
    // The picks: files mounted as PlexoraPicks.pick mounts them, a folder as
    // its tree (pickedFiles.mountTree), each adopted so the page can find it.
    const paths = [];
    for (const [i, file] of files.entries()) {
        const folder = statSync(file).isDirectory();
        const inside = folder ? walk(file) : [path.basename(file)];
        await page.evaluate((id) => {
            const input = document.createElement("input");
            input.type = "file"; input.multiple = true; input.id = id; input.hidden = true;
            document.body.append(input);
        }, `vol-pick-${i}`);
        await page.setInputFiles(`#vol-pick-${i}`, folder ? inside.map((p) => path.join(file, p)) : [file]);
        paths.push(await page.evaluate(async ([id, isFolder, rels, name]) => {
            const picked = [...document.getElementById(id).files];
            const root = `/picked/vol${id.split("-").pop()}`;
            if (!isFolder) {
                const [mounted] = await window.PlexoraPython.mount(root, picked);
                window.PlexoraPicks.adopt(mounted, picked[0]);
                return mounted;
            }
            const blobs = picked.map((data, k) => ({ name: `${name}/${rels[k]}`, data }));
            await window.PlexoraPython.mountTree(root, blobs);
            for (const blob of blobs) window.PlexoraPicks.adopt(`${root}/${blob.name}`, blob.data);
            return `${root}/${name}`;
        }, [`vol-pick-${i}`, folder, inside, path.basename(file)]));
    }
    /**
     * The product's own path to a surface: POST /volume/<ds>/surfaces, its
     * build queued and run between requests, then its mesh and objects.
     * Surfaces are Pro: a throwaway licence is minted and trusted in this
     * Python only, as tests/conftest.py's `_issuer` and P9 did.
     */
    async function surfaceRoute(name, iso) {
        const licence = await page.evaluate((code) => window.PlexoraPython.run(code), [
            "import os",
            "if not hasattr(os, 'link'):",
            "    def _link(src, dst):",
            "        raise OSError(38, 'link() is not available here')",
            "    os.link = _link",
            "from biocognia.testing import Issuer",
            "from plexora.licensing import LICENSING, PRODUCT",
            "class Patch:",
            "    setattr = staticmethod(setattr)",
            "issuer = Issuer(Patch(), PRODUCT)",
            "issuer.install(LICENSING, issuer.issue(PRODUCT, ent=['volume']))",
            "from plexora import licensing",
            "str(licensing.allows('volume:surfaces'))",
        ].join("\n")).catch((error) => `error: ${String(error).slice(0, 800)}`);
        const out = { licence };
        const ds = encodeURIComponent(name);
        const channel = (await frame.evaluate(async (d) => (await (await fetch(`/get_channel_names?datasource=${d}&shortNames=1`)).json()), ds))[0];
        const t0 = Date.now();
        const made = await frame.evaluate(async ([d, body]) => {
            const response = await fetch(`/volume/${d}/surfaces`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                                 body: JSON.stringify(body) });
            return { status: response.status, body: await response.json().catch(() => null) };
        }, [ds, { type: "isosurface", channel, iso, smoothing: "medium" }]);
        out.create = { status: made.status, layer: made.body?.layer, error: made.body?.error };
        const layer = made.body?.layer;
        let entry = null;
        for (let i = 0; layer && i < 2400; i += 1) {
            const listed = await frame.evaluate(async (d) => (await (await fetch(`/volume/${d}/surfaces`, { cache: "no-store" })).json()), ds);
            entry = (listed.surfaces || []).find((x) => x.id === layer);
            if (entry && ["ready", "error", "failed"].includes(entry.status)) break;
            await page.waitForTimeout(250);
        }
        out.buildMs = Date.now() - t0;
        out.status = entry?.status;
        out.summary = entry?.summary && { triangles: entry.summary.triangles, level: entry.summary.level,
                                          objects: entry.summary.objects };
        out.job = entry?.job && { status: entry.job.status, error: entry.job.error, message: entry.job.message };
        if (entry?.status === "ready") {
            const id = encodeURIComponent(layer);
            for (const part of ["mesh", "objects", "manifest"]) {
                out[part] = await frame.evaluate(async (u) => {
                    const response = await fetch(u, { cache: "no-store" });
                    return { status: response.status, bytes: (await response.arrayBuffer()).byteLength };
                }, `/volume/${ds}/surfaces/${id}/${part}`);
            }
            // The viewer reads its surface layers as it opens: open the
            // project again, enter 3-D, and photograph the surface drawn.
            await frame.evaluate((n) => { location.href = `/${encodeURIComponent(n)}`; }, name);
            frame = await appFrame(page);
            await frame.waitForFunction(() => window.__plexora?.viewState, null, { timeout: 60000 });
            await frame.evaluate(async () => {
                window.__plexora.viewState.set({ mode: "volume", layout: "single" });
                const end = performance.now() + 60000;
                while (performance.now() < end) {
                    const r = window.__plexora.volumeView?.renderer;
                    if (r && r.status === "ready" && !r._loads?.size) break;
                    await new Promise((resolve) => setTimeout(resolve, 50));
                }
            });
            await page.waitForTimeout(3000);
            await page.screenshot({ path: path.join(out_dir(), "3d-surface.png") });
        }
        return out;
    }
    const out_dir = () => path.join(out, "website");
    return {
        page, frame, base, paths,
        open: async (name) => {
            await frame.evaluate((n) => { location.href = `/${encodeURIComponent(n)}`; }, name);
            frame = await appFrame(page);
            return frame;
        },
        extra: async () => ({
            stages: await page.evaluate(() => window.__stages),
            heapBytes: await page.evaluate(() => window.PlexoraPython.heap()),
            python: await page.evaluate(() => window.PlexoraPython.recent()
                .sort((a, b) => b.ms - a.ms).slice(0, 15)
                .map((r) => ({ path: r.path.slice(0, 90), ms: Math.round(r.ms), py: r.pyMs }))),
        }),
        ops: async (name) => {
            const { readFileSync } = await import("node:fs");
            const source = readFileSync(HELPER, "utf8");
            const ops = source.slice(source.indexOf("OPS = r'''") + 10, source.indexOf("'''", source.indexOf("OPS = r'''") + 10));
            const started = Date.now();
            const answer = await page.evaluate((code) => window.PlexoraPython.run(code),
                `${ops}\nimport json\njson.dumps(volume_ops(${JSON.stringify(name)}, mask=${hasMask ? "True" : "False"}, objects=${hasMask ? "True" : "False"}))`)
                .catch((error) => JSON.stringify({ error: String(error).slice(0, 1500) }));
            const website = { ...JSON.parse(answer), wallMs: Date.now() - started };
            return { website, route: await surfaceRoute(name, website.isosurface?.iso) };
        },
        close: async () => { await browser.close(); server.stop(); },
    };
}

async function desktopArm() {
    const data = path.join(out, "desktop-data");
    const started = spawnSync(PYTHON, [path.join(REPO, "tools", "bench3d", "serve.py"), "--data", data],
                              { cwd: REPO, encoding: "utf8", env });
    if (started.status !== 0) throw new Error(`desktop server: ${started.stderr.slice(-2000)}`);
    const server = JSON.parse(started.stdout.trim().split("\n").pop());
    const stop = () => spawnSync(PYTHON, [path.join(REPO, "tools", "bench3d", "serve.py"), "--stop", String(server.pid)],
                                 { cwd: REPO, env });
    const browser = await launch({ headed });
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    const base = `http://127.0.0.1:${server.port}`;
    await page.goto(`${base}/`);
    let stopped = false;
    return {
        page, frame: page, base, paths: files,
        open: async (name) => { await page.goto(`${base}/${encodeURIComponent(name)}`); return page; },
        extra: async () => ({ server: { port: server.port, log: server.log } }),
        ops: async (name) => {
            // The server stopped first: the operations write the project.
            stop(); stopped = true;
            const run = (extraEnv, repeat = 1) => {
                const t0 = Date.now();
                const done = spawnSync(PYTHON, [HELPER, "ops", name, "--repeat", String(repeat),
                                                ...(hasMask ? ["--mask", "--objects"] : [])],
                                       { cwd: REPO, encoding: "utf8", maxBuffer: 64 << 20,
                                         env: { ...env, PLEXORA_DATA_PATH: data, PLEXORA_TELEMETRY: "off", ...extraEnv } });
                if (done.status !== 0) return { error: done.stderr.slice(-1500) };
                return { ...JSON.parse(done.stdout.trim().split("\n").pop()), wallMs: Date.now() - t0 };
            };
            // numba's first call loads (or compiles) each kernel: its second run.
            return { numba: run({}, 2), python: run({ PLEXORA_NO_NUMBA: "1" }) };
        },
        close: async () => { await browser.close(); if (!stopped) stop(); },
    };
}

// -- what an arm does ----------------------------------------------------------

async function runArm(kind) {
    const arm = kind === "website" ? await websiteArm() : await desktopArm();
    const dir = path.join(out, kind);
    mkdirSync(dir, { recursive: true });
    const rec = { timings: {} };
    const errors = [];
    arm.page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    arm.page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    try {
        let target = arm.frame;
        const post = (route, body) => target.evaluate(async ([r, b]) => {
            const t = performance.now();
            const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify(b) });
            return { status: response.status, ms: Math.round(performance.now() - t), body: await response.json().catch(() => null) };
        }, [route, body]);
        const getJson = (route) => target.evaluate(async (r) => {
            const response = await fetch(r, { cache: "no-store" });
            return { status: response.status, body: await response.json().catch(() => null) };
        }, route);

        // The import, as the Import dialog makes it.
        const t0 = Date.now();
        const inspected = await post("/import/inspect", { paths: arm.paths });
        const imported = await post("/import/sample", { paths: arm.paths, answers: {} });
        rec.timings.importMs = Date.now() - t0;
        const name = imported.body && imported.body.name;
        rec.name = name;
        rec.inspect = { status: inspected.status, layers: (inspected.body?.samples || []).flatMap((s) => (s.layers || []).map((l) => `${l.kind}:${l.label}`)) };
        if (!name) throw new Error(`import failed: ${JSON.stringify(imported.body).slice(0, 800)}`);

        // The viewer, its first plane drawn.
        const opened = Date.now();
        target = await arm.open(name);
        await target.waitForFunction(() => {
            const viewer = window.__plexora?.seaDragonViewer?.viewer;
            if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
            for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
                if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
            }
            return true;
        }, null, { timeout: 300000, polling: 100 }).catch(() => { rec.planeTimedOut = true; });
        rec.timings.openMs = Date.now() - opened;
        const ds = encodeURIComponent(name);
        const info = (await getJson(`/volume/${ds}/info`)).body || {};
        // The channels by the names the viewer asks for them with.
        const named = (await getJson(`/get_channel_names?datasource=${ds}&shortNames=1`)).body;
        const channels = (Array.isArray(named) ? named : (named?.channels || named?.names || []))
            .map((c) => (typeof c === "string" ? c : c.name || c.shortname || c.fullname)).filter(Boolean);
        rec.info = { volumetric: info.volumetric, shape: info.shape, levels: (info.levels || []).length,
                     channels, dtype: info.dtype, source_kind: info.source_kind, named };

        // The windows, until exact (the viewer asks until each is), before the
        // 3-D view: entered on a provisional window it uploads its level twice,
        // and which of the two is on screen when sampled is a matter of timing.
        const w0 = Date.now();
        let windows = {};
        const query = channels.map((c) => `channel=${encodeURIComponent(c)}`).join("&");
        for (let i = 0; i < 600; i += 1) {
            windows = (await getJson(`/volume/${ds}/window?${query}`)).body?.windows || {};
            if (channels.every((c) => windows[c]?.exact)) break;
            await arm.page.waitForTimeout(250);
        }
        rec.timings.windowsExactMs = Date.now() - w0;
        rec.windows = windows;
        // The 3-D view, entered as a user enters it, until it has drawn.
        await target.waitForFunction(() => window.__plexora?.viewState, null, { timeout: 60000 });
        const entered = await target.evaluate(async () => {
            const P = window.__plexora;
            const t = performance.now();
            P.viewState.set({ mode: "volume", layout: "single" });
            const end = t + 300000;
            let status = null;
            while (performance.now() < end) {
                const r = P.volumeView?.renderer;
                status = r?.status || null;
                if (r && r.status === "ready" && !r._loads?.size && !(r.animating && r.animating())) break;
                if (r && ["unavailable", "error"].includes(r.status)) break;
                await new Promise((resolve) => setTimeout(resolve, 20));
            }
            return { ms: Math.round(performance.now() - t), status };
        });
        rec.timings.enter3dMs = entered.ms;
        rec.renderer = entered.status;
        // What the canvas shows: every pixel's brightest sample, read back.
        const sample = (settled = false) => target.evaluate(async (still) => {
            const renderer = window.__plexora?.volumeView?.renderer;
            if (still) {
                // The sidebar's auto-contrast (pass 2, the GMM fit) lands when
                // it lands and moves the picture: wait for the status chip to
                // have shown no "Auto-contrast" for a second.
                const end = performance.now() + 60000;
                let quiet = 0;
                while (performance.now() < end && quiet < 1000) {
                    quiet = /Auto-contrast/.test(document.body.innerText) ? 0 : quiet + 100;
                    await new Promise((resolve) => setTimeout(resolve, 100));
                }
            }
            if (still && renderer?.whenSettled) {
                // The capture path (video export, viewer_capture): settled,
                // then one frame at full quality -- the frame a still picture
                // is, whatever the renderer's adaptive quality did meanwhile.
                await Promise.race([renderer.whenSettled(), new Promise((resolve) => setTimeout(resolve, 60000))]);
                renderer.renderFrame();
            }
            const canvas = renderer?.canvas;
            if (!canvas || !canvas.width) return null;
            const bitmap = await createImageBitmap(canvas);
            const off = new OffscreenCanvas(bitmap.width, bitmap.height);
            const g = off.getContext("2d");
            g.drawImage(bitmap, 0, 0);
            const data = g.getImageData(0, 0, bitmap.width, bitmap.height).data;
            let lit = 0; let sum = 0;
            for (let i = 0; i < data.length; i += 4) {
                const v = Math.max(data[i], data[i + 1], data[i + 2]);
                sum += v;
                if (v > 24) lit += 1;
            }
            const n = data.length / 4;
            // A 32 x 32 grid of block means: the picture, small enough to set
            // beside the other arm's.
            const grid = new Array(32 * 32).fill(0);
            const counts = new Array(32 * 32).fill(0);
            for (let y = 0; y < bitmap.height; y += 1) {
                const gy = Math.min(31, Math.floor((y * 32) / bitmap.height));
                for (let x = 0; x < bitmap.width; x += 1) {
                    const i = (y * bitmap.width + x) * 4;
                    const cell = gy * 32 + Math.min(31, Math.floor((x * 32) / bitmap.width));
                    grid[cell] += Math.max(data[i], data[i + 1], data[i + 2]);
                    counts[cell] += 1;
                }
            }
            return { width: bitmap.width, height: bitmap.height, litShare: lit / n, mean: sum / n,
                     grid: grid.map((v, k) => Math.round(v / Math.max(1, counts[k]))) };
        }, settled);
        let pixels = null;
        for (let i = 0; i < 50; i += 1) {
            pixels = await sample();
            if (pixels && pixels.litShare > 0.002) break;
            await arm.page.waitForTimeout(200);
        }
        rec.pixels = pixels;
        // The mask's "ready" notice closes itself: the screenshot is of the view.
        await target.waitForFunction(() => !/Segmentation mask ready|Preparing/.test(document.body.innerText),
                                     null, { timeout: 20000, polling: 250 }).catch(() => null);
        await arm.page.screenshot({ path: path.join(dir, "3d.png") });

        const stamp = (c) => `${windows[c]?.qmin},${windows[c]?.qmax}`;
        const manifest = (await getJson(`/volume/${ds}/bricks/manifest`)).body || {};
        rec.bricks = (manifest.levels || []).map((l) => ({ level: l.level, pool: l.pool, grid: l.grid }));

        // The viewer's own requests, as this page made them.
        const viewerAsked = await target.evaluate(() => [...new Set(performance.getEntriesByType("resource")
            .map((e) => new URL(e.name).pathname + new URL(e.name).search)
            .filter((u) => (u.startsWith("/volume/") && !/\/(window|perf|info)\b/.test(u))
                    || (u.startsWith("/generated/data/") && u.includes("view="))))]);
        rec.viewerAsked = viewerAsked.length;
        const tile = await target.evaluate(() => performance.getEntriesByType("resource")
            .map((e) => new URL(e.name).pathname).find((u) => u.startsWith("/generated/data/")));

        // The routes asked explicitly: the same addresses on both arms.
        const urls = [`/volume/${ds}/info`, `/volume/${ds}/bricks/manifest`,
                      `/volume/${ds}/window?${query}`];
        const levels = (info.levels || []).length;
        for (const c of channels) {
            const q = `channel=${encodeURIComponent(c)}&w=${stamp(c)}`;
            urls.push(`/volume/${ds}/level?${q}`, `/volume/${ds}/level?${q}&level=${levels - 1}`);
        }
        if (channels.length) urls.push(`/volume/${ds}/level?channel=${encodeURIComponent(channels[0])}&w=${stamp(channels[0])}&format=f16`);
        const brickAsk = (entry, ats) => {
            const q = new URLSearchParams({ level: String(entry.level), t: "0", format: "u8" });
            for (const c of channels) { q.append("channel", c); q.append("w", stamp(c)); }
            q.set("at", ats.join(","));
            return `/volume/${ds}/bricks?${q}`;
        };
        const allAt = (entry) => {
            const ats = [];
            for (let z = 0; z < entry.grid[0]; z += 1) for (let y = 0; y < entry.grid[1]; y += 1)
                for (let x = 0; x < entry.grid[2]; x += 1) ats.push(`${z}.${y}.${x}`);
            return ats;
        };
        const brickLevels = manifest.levels || [];
        if (brickLevels.length) {
            urls.push(brickAsk(brickLevels[0], allAt(brickLevels[0]).slice(0, brickCount)));
            const coarse = brickLevels[brickLevels.length - 1];
            urls.push(brickAsk(coarse, allAt(coarse).slice(0, 48)));
        }
        const { depth = 1, height = 1, width = 1 } = info;
        const midY = Math.floor(height / 2);
        urls.push(`/volume/${ds}/section?view=xzy${midY}`, `/volume/${ds}/section?view=yzx${Math.floor(width / 2)}`);
        if (tile) {
            const [, , , , chan] = tile.split("/");
            const coarsest = levels - 1;
            for (const view of [`z${Math.floor(depth / 2)}`, `z0-${depth - 1}max`, `xzy${midY}`]) {
                urls.push(`/generated/data/${ds}/${chan}/${coarsest}/0_0.png?q=hd&view=${view}`);
            }
        }
        if (hasMask) urls.push(`/volume/${ds}/objects`);
        rec.asked = urls.length;
        const all = [...new Set([...urls, ...viewerAsked])];

        const answers = [];
        for (const url of all) {
            const got = await target.evaluate(async ([u, max]) => {
                const t = performance.now();
                const response = await fetch(u, { cache: "no-store" });
                const buffer = await response.arrayBuffer();
                const ms = performance.now() - t;
                const digest = await crypto.subtle.digest("SHA-256", buffer);
                const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
                let body = null;
                if (buffer.byteLength <= max) {
                    const bytes = new Uint8Array(buffer);
                    let text = "";
                    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
                    body = btoa(text);
                }
                const headers = Object.fromEntries([...response.headers.entries()]
                    .filter(([k]) => k.startsWith("x-plexora-") && k !== "x-plexora-served-by"));
                return { status: response.status, type: response.headers.get("Content-Type") || "", bytes: buffer.byteLength,
                         sha256: hex, ms: Math.round(ms), headers, body };
            }, [url, SAVE_MAX]);
            const file = `${answers.length}.bin`;
            if (got.body !== null) writeFileSync(path.join(dir, file), Buffer.from(got.body, "base64"));
            answers.push({ url, file: got.body !== null ? file : null, status: got.status, type: got.type,
                           bytes: got.bytes, sha256: got.sha256, ms: got.ms, headers: got.headers,
                           viewer: viewerAsked.includes(url), explicit: urls.includes(url) });
        }
        writeFileSync(path.join(dir, "answers.json"), JSON.stringify(answers, null, 1));
        rec.answers = answers.map(({ url, status, bytes, ms }) => ({ url: url.slice(0, 160), status, bytes, ms }));
        rec.failedAnswers = answers.filter((a) => a.status >= 400).map((a) => `${a.status} ${a.url.slice(0, 160)}`);

        // Single-brick latency at the finest brick level: cold, then warm.
        if (brickLevels.length) {
            const ats = allAt(brickLevels[0]);
            const picks = ats.slice(Math.max(0, ats.length - brickCount));   // not asked above
            const time = async () => {
                const ms = [];
                for (const at of picks) {
                    ms.push(await target.evaluate(async (u) => {
                        const t = performance.now();
                        const response = await fetch(u, { cache: "no-store" });
                        await response.arrayBuffer();
                        return performance.now() - t;
                    }, brickAsk(brickLevels[0], [at])));
                }
                return ms;
            };
            const cold = await time();
            const warm = await time();
            rec.brickLatency = { level: brickLevels[0].level, n: picks.length,
                                 coldP50: pct(cold, 0.5), coldP95: pct(cold, 0.95),
                                 warmP50: pct(warm, 0.5), warmP95: pct(warm, 0.95) };
        }
        // The picture once the view has been still and loaded for a second
        // (the exact windows and the HD level in): the arms' are compared.
        await target.evaluate(async () => {
            const r = window.__plexora?.volumeView?.renderer;
            const end = performance.now() + 60000;
            let quiet = 0;
            while (r && performance.now() < end && quiet < 1000) {
                const busy = r.status !== "ready" || r._loads?.size || (r.animating && r.animating());
                quiet = busy ? 0 : quiet + 50;
                await new Promise((resolve) => setTimeout(resolve, 50));
            }
        });
        rec.settled = await sample(true);
        // What the renderer is drawing with (its own scalar state), to explain
        // a picture that differs.
        rec.rendererState = await target.evaluate(() => {
            const r = window.__plexora?.volumeView?.renderer;
            if (!r) return null;
            const flat = {};
            for (const [key, value] of Object.entries(r)) {
                if (["number", "string", "boolean"].includes(typeof value)) flat[key] = value;
                else if (Array.isArray(value) && value.length <= 8 && value.every((v) => typeof v !== "object")) flat[key] = value;
            }
            flat.textures = [...(r.textures?.entries() || [])].map(([key, e]) => ({
                key, id: e.id, format: e.format, level: e.level, shape: e.shape, made: e.levelWindow }));
            flat.shown = [...(r._shownKey?.entries() || [])];
            try {
                flat.channels = (r.channels?.() || []).map((c) => ({ name: c.name, stamp: c.stamp, window: c.window,
                                                                     raw: c.raw, curve: c.curve || null }));
            } catch (error) { flat.channels = String(error); }
            return flat;
        });
        await arm.page.screenshot({ path: path.join(dir, "3d-settled.png") });
        Object.assign(rec, await arm.extra());
        if (runOps) rec.ops = await arm.ops(name);
    } catch (error) {
        rec.error = String(error.stack || error).slice(0, 1500);
    } finally {
        rec.errors = errors.slice(0, 20);
        await arm.close();
    }
    return rec;
}

for (const kind of arms) record.arms[kind] = await runArm(kind);

for (const [kind, rec] of Object.entries(record.arms)) {
    check(`${kind}: imported and opened`, rec.name && !rec.error, rec.error);
    check(`${kind}: a volume`, rec.info?.volumetric === true, rec.info);
    check(`${kind}: the 3-D view drew`, rec.renderer === "ready" && rec.pixels && rec.pixels.litShare > 0.002,
          { renderer: rec.renderer, pixels: rec.pixels });
    check(`${kind}: every answer 2xx`, rec.failedAnswers && !rec.failedAnswers.length, rec.failedAnswers);
}
if (record.arms.website && record.arms.desktop && !record.arms.website.error && !record.arms.desktop.error) {
    const done = spawnSync(PYTHON, [HELPER, "compare", path.join(out, "website"), path.join(out, "desktop"),
                                    "--names", names.join(",")],
                           { cwd: REPO, encoding: "utf8", maxBuffer: 64 << 20, env });
    if (done.status !== 0) {
        check("compared", false, done.stderr.slice(-1500));
    } else {
        const compared = JSON.parse(done.stdout.trim().split("\n").pop());
        record.compared = compared;
        const verdicts = {};
        for (const row of Object.values(compared)) verdicts[row.verdict] = (verdicts[row.verdict] || 0) + 1;
        record.verdicts = verdicts;
        // An address only one arm's viewer asked (its timing, not its answer)
        // is reported, not failed; one asked of both arms must be answered alike.
        const bad = Object.entries(compared).filter(([, row]) => !["same", "same_pixels", "same_json"].includes(row.verdict)
                                                     && !(row.verdict === "missing" && !row.explicit));
        check("every answer the desktop's", !bad.length, Object.fromEntries(bad.slice(0, 12)));
        record.oneSided = Object.entries(compared).filter(([, row]) => row.verdict === "missing").map(([url]) => url);
    }
    const [web, desk] = [record.arms.website.settled, record.arms.desktop.settled];
    if (web?.grid && desk?.grid) {
        // The 3-D view's picture on both arms: block means 0..255 apart.
        let diff = 0;
        for (let k = 0; k < web.grid.length; k += 1) diff += Math.abs(web.grid[k] - desk.grid[k]);
        // The display contrast each arm's sidebar set (raw units). Its second
        // pass, the GMM fit, sometimes never lands on a slot on either app
        // (viewerSidebar's stillCurrent()), and then the two pictures differ
        // by their contrast, not their voxels: compared only when it agrees.
        const contrast = (arm) => (arm.rendererState?.channels || []).map((c) => c.raw || []);
        const [cw, cd] = [contrast(record.arms.website), contrast(record.arms.desktop)];
        const sameContrast = cw.length === cd.length && cw.every((r, i) => r.length === cd[i].length
            && r.every((v, j) => Math.abs(v - cd[i][j]) <= 0.01 * Math.max(1, Math.abs(cd[i][j]))));
        record.pictureDiff = { meanAbs: diff / web.grid.length, websiteMean: web.mean, desktopMean: desk.mean,
                               contrast: { website: cw, desktop: cd } };
        if (sameContrast) {
            check("the 3-D view shows what the desktop's shows", record.pictureDiff.meanAbs < 2, record.pictureDiff);
        } else {
            record.pictureSkipped = "the sidebars' contrasts differ (auto-contrast's fit landed on one arm only)";
        }
    }
}
const webOps = record.arms.website?.ops?.website;
const deskOps = record.arms.desktop?.ops?.numba;
if (webOps && deskOps) {
    // What each operation made, on the website (the kernels as Python) and
    // on the desktop (numba): the same meshes and the same object table.
    const made = (ops) => ({
        isosurface: ops.isosurface?.mesh || ops.isosurface?.error?.slice(-300),
        measured: ops.isosurface?.measured_12,
        cell_surfaces: ops.cell_surfaces?.mesh || ops.cell_surfaces?.error?.slice(-300),
        objects: ops.objects?.table || ops.objects?.error?.slice(-300),
    });
    const [w, d] = [made(webOps), made(deskOps)];
    const differ = Object.keys(w).filter((k) => JSON.stringify(w[k]) !== JSON.stringify(d[k]));
    check("the 3-D operations make what the desktop makes", !differ.length,
          Object.fromEntries(differ.map((k) => [k, { website: w[k], desktop: d[k] }])));
    // Columns whose last bits differ (a transcendental function's libm).
    const exact = [webOps.isosurface?.measured || {}, deskOps.isosurface?.measured || {}];
    record.lastBitColumns = Object.keys(exact[0]).filter((k) => exact[0][k] !== exact[1][k]);
}
record.ok = Object.values(record.checks).every((c) => c.ok);
writeFileSync(path.join(out, "record.json"), JSON.stringify(record, null, 1));
console.log(JSON.stringify(record, null, 1));
if (!record.ok) process.exitCode = 1;
