/**
 * The POC page's entry: window.POC, called by bench/run.mjs through
 * page.evaluate. Each POC function returns plain JSON for the record.
 *
 * Files come from the page's <input type=file> (the driver fills it with
 * setInputFiles, which hands the page file-backed Files exactly as a user's
 * pick would), never from a path: the page has no filesystem.
 */
import { createViewer, waitFull, canvasPixels, SHIMS, installShims } from "./viewer.js";
import { TiffPool } from "./tiffPool.js";
import { NodeClient } from "./nodeClient.js";
import { decodePng, sha256, firstDifference } from "./png.js";
import { chooseOverviewLevel, overviewOf, statsOf, gmmOf } from "./stats.js";
import { p3, p3Ingest, p3Reopen } from "./p3.js";
import { p4 } from "./p4.js";
import { p2Viewer, p2bBuild, p2bIdentity, p2bReopen, buildPyramid, pyramidReopen, p7Identity } from "./p2.js";
import { p5Protocol, p5Reach, p5Isolation, p5Poll, p5Pair, p5Rnode, p5Lna, p5LnaStart, p5LnaPoll } from "./p5.js";

const status = (text) => { document.getElementById("status").textContent = text; };
const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const PALETTE = ["#0000ff", "#00ff00", "#ff0000", "#ffff00", "#ff00ff", "#00ffff", "#ff8000",
    "#8000ff", "#80ff00", "#ff0080", "#0080ff", "#ffffff", "#808000", "#008080", "#800080"];

export function summary(values) {
    const v = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
    if (!v.length) return { n: 0 };
    const q = (p) => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1) + 0.5))];
    const mean = v.reduce((a, b) => a + b, 0) / v.length;
    return { n: v.length, min: v[0], p50: q(0.5), p90: q(0.9), p95: q(0.95), max: v[v.length - 1], mean };
}

export function rng(seed) {
    let s = seed >>> 0;
    return () => {
        s = (s + 0x6d2b79f5) >>> 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function pickedFile(name) {
    const files = [...document.getElementById("picker").files];
    const file = files.find((f) => f.name === name);
    if (!file) throw new Error(`no picked file named ${name} (have: ${files.map((f) => f.name).join(", ")})`);
    return file;
}

const workerUrl = (name) => new URL(`dist/${name}`, location.href).href;

/** What this browser is, for every record. */
async function env() {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2");
    const info = gl?.getExtension("WEBGL_debug_renderer_info");
    let estimate = null;
    try { estimate = await navigator.storage.estimate(); } catch (e) { /* none */ }
    return {
        userAgent: navigator.userAgent,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory ?? null,
        webgl2: Boolean(gl),
        gpu: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null,
        crossOriginIsolated: self.crossOriginIsolated,
        isSecureContext: self.isSecureContext,
        origin: location.origin,
        storage: estimate ? { quota: estimate.quota, usage: estimate.usage } : null,
        hasShowOpenFilePicker: typeof window.showOpenFilePicker === "function",
        hasOPFS: typeof navigator.storage?.getDirectory === "function",
        jsHeap: performance.memory ? performance.memory.usedJSHeapSize : null,
    };
}

/** All tiles of a level, as [x, y]. */
function tilesOf(level, tw = 1024, th = 1024) {
    const out = [];
    for (let y = 0; y < Math.ceil(level.height / th); y += 1) {
        for (let x = 0; x < Math.ceil(level.width / tw); x += 1) out.push([x, y]);
    }
    return out;
}

/** `count` random (channel, level, x, y), every level equally likely. */
function randomTiles(geometry, count, seed, channels = null) {
    const rand = rng(seed);
    const pool = channels || [...Array(geometry.numChannels).keys()];
    const out = [];
    for (let i = 0; i < count; i += 1) {
        const level = Math.floor(rand() * geometry.levels.length);
        const tiles = tilesOf(geometry.levels[level]);
        const [x, y] = tiles[Math.floor(rand() * tiles.length)];
        out.push({ channel: pool[Math.floor(rand() * pool.length)], level, x, y });
    }
    return out;
}

// ---------------------------------------------------------------------------
// P0: does the rendering layer run from a static page, unmodified?

async function p0({ node, image, glProbe = true }) {
    installShims();
    const globals = {};
    for (const name of ["_", "d3", "OpenSeadragon", "GLRenderer", "UPNG", "PNG", "Buffer", "RGB_TILE_FORMAT",
                        "PlexoraGL", "PlexoraTileColorize", "PlexoraTileDecode", "PlexoraTileFetch",
                        "PlexoraLabelTile", "PlexoraLabelGpu", "PlexoraLayerStack", "plexoraUrl",
                        "PlexoraDisplayCurve", "toFloatColor", "datasource", "flaskVariables"]) {
        globals[name] = typeof globalThis[name];
    }
    const out = { env: await env(), globals, shims: { ...SHIMS }, base: window.PLEXORA_BASE_URL,
                  shaderUrl: plexoraUrl("client/src/shaders/frag.glsl") };

    // Byte order: the node's HD PNG vs geotiff.js's samples, same tile.
    const client = new NodeClient(node.endpoint, node.token);
    const pool = new TiffPool(workerUrl("tiffWorker.js"), 1);
    const file = pickedFile(image.file);
    const opened = await pool.open({ file, decoder: "native" });
    const g = opened.geometry;
    const coarse = g.levels.length - 1;
    const png = await decodePng(await client.tileBytes(node.imageId, 0, coarse, 0, 0));
    const raw = await pool.tile(0, coarse, 0, 0, { raw: true });
    const le = new Uint16Array(raw.array.buffer.slice(0));
    const pngView = new DataView(png.data.buffer);
    let beMatches = 0, leMatches = 0;
    for (let i = 0; i < le.length; i += 1) {
        if (pngView.getUint16(i * 2, false) === le[i]) beMatches += 1;
        if (pngView.getUint16(i * 2, true) === le[i]) leMatches += 1;
    }
    out.byteOrder = {
        tile: { channel: 0, level: coarse, x: 0, y: 0, width: raw.width, height: raw.height },
        pngIsBigEndian: beMatches === le.length,
        geotiffIsLittleEndian: g.littleEndian,
        samples: le.length, beMatches, leMatches,
        conclusion: beMatches === le.length
            ? "the HD PNG carries big-endian samples; geotiff.js returns native little-endian, so the browser node swaps before delivery"
            : "MISMATCH: see counts",
    };
    pool.terminate();

    // frag.glsl's u16 reconstruction: draw a known ramp through the product's
    // own renderer and read it back.
    if (glProbe) out.shader = await probeU16Shader();
    return out;
}

/**
 * Draw a 256 x 1 tile of values v = k * 257 (k = 0..255; hi byte = lo byte =
 * k) through glInit's renderer and frag.glsl with range [0, 1] and colour
 * white, and read the red channel back. Exact reconstruction gives k; the
 * shader's `r * 255 + g` gives round(k * 256 / 257).
 */
async function probeU16Shader() {
    installShims();
    let resolveReady;
    const ready = new Promise((r) => { resolveReady = r; });
    let probe = null;
    const renderer = PlexoraGL.createGLRenderer({
        // ImageViewer.indexOfTexture's constant units: every sampler on a unit
        // of its own, or GL refuses the draw (two sampler types on unit 0).
        indexOfTexture: (label) => {
            const i = ["ids", "centers", "ranges", "pickings"].indexOf(label);
            return i > -1 ? i + probe._otherOffset : -1;
        },
        selectTexture: (gl, texture, idx) => {
            gl.activeTexture(gl["TEXTURE" + idx]);
            gl.bindTexture(gl.TEXTURE_2D, texture);
            gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
            gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        },
        resolveGLReady: () => resolveReady(),
    });
    probe = renderer;
    const w = 256, h = 1;
    renderer.width = w;
    renderer.height = h;
    renderer.updateShape(w, h);
    await renderer.ready();
    await ready;
    const gl = renderer.gl;
    let key = 0;
    const draw = (values, range) => {
        const bytes = new Uint8Array(w * 2);
        values.forEach((v, k) => { bytes[2 * k] = v >> 8; bytes[2 * k + 1] = v & 0xff; });
        renderer.gl_arguments = {
            pie_radius_1f: 8.5, magnitude_2iv: [1, 1], id_end_1i: 0, picked_end_1i: -1,
            modes_2i: [false, false], marker_sample_4iv: [-1, -1, -1, -1],
            tile_scale_1f: 1, tile_fraction_1f: 1,
            x_bounds_2fv: new Float32Array([0, 1]), y_bounds_2fv: new Float32Array([0, 1]),
            origin_2fv: new Float32Array([0, 0]), centers: [],
            color_3fv: new Float32Array([1, 1, 1]), range_2fv: new Float32Array(range),
            cofactor_1f: 0, fmt_1i: 16, alpha_mode_1i: 0,
        };
        const probeKey = `probe-${key++}`;
        const source = { format: 16, getTileKey: () => probeKey };
        renderer.loadArray({ tiledImage: { source }, tile: { level: 0, x: 0, y: 0, _array: bytes, _format: "u16" } }, w, h);
        const px = new Uint8Array(w * h * 4);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
        const red = values.map((_, k) => px[4 * k]);
        const exact = values.map((v) => Math.round(Math.min(1, Math.max(0, (v / 65535 - range[0]) / (range[1] - range[0]))) * 255));
        const diff = red.map((r, k) => r - exact[k]);
        return { range, red, exact, maxAbsError: Math.max(...diff.map(Math.abs)),
                 meanError: diff.reduce((a, b) => a + b, 0) / diff.length,
                 firstErrorAt: diff.findIndex((d) => Math.abs(d) > 1), values: [values[0], values[values.length - 1]] };
    };
    // Full window over the whole 16-bit range: v = k * 257.
    const full = draw(Array.from({ length: w }, (_, k) => k * 257), [0, 1]);
    // A narrow window high in the range -- a bright channel levelled tight.
    const lo = 0.5, hi = 0.55;
    const narrow = draw(Array.from({ length: w }, (_, k) => Math.round((lo + (hi - lo) * k / (w - 1)) * 65535)), [lo, hi]);
    return {
        full: { ...full, red: full.red.filter((_, k) => k % 32 === 0) },
        narrow: { ...narrow, red: narrow.red.filter((_, k) => k % 32 === 0), exact: narrow.exact.filter((_, k) => k % 32 === 0) },
        note: "frag.glsl:504 rebuilds value = r*255 + g; exact is r*256 + g. Errors are in 8-bit display levels.",
    };
}

// ---------------------------------------------------------------------------
// P1: local OME-TIFF through a geotiff.js worker pool

async function p1Open({ image, poolSize, decoder = "native", prefetchOffsets = true }) {
    const file = pickedFile(image.file);
    const pool = new TiffPool(workerUrl("tiffWorker.js"), poolSize);
    const opened = await pool.open({ file, decoder, prefetchOffsets });
    return { pool, opened };
}

/** Per-tile latency (one at a time) and throughput (all in flight) for one pool. */
async function tileBench(pool, geometry, { count = 200, seed = 7, concurrency = null } = {}) {
    const tiles = randomTiles(geometry, count, seed);
    const sequential = [];
    const worker = [];
    for (const t of tiles.slice(0, Math.min(count, 100))) {
        const t0 = performance.now();
        const r = await pool.tile(t.channel, t.level, t.x, t.y);
        sequential.push(performance.now() - t0);
        worker.push(r.timing);
    }
    const t0 = performance.now();
    const limit = concurrency || pool.workers.length * 2;
    let next = 0;
    const lat = [];
    await Promise.all(Array.from({ length: limit }, async () => {
        while (next < tiles.length) {
            const t = tiles[next++];
            const s = performance.now();
            await pool.tile(t.channel, t.level, t.x, t.y);
            lat.push(performance.now() - s);
        }
    }));
    const wall = performance.now() - t0;
    return {
        sequentialMs: summary(sequential),
        workerDecodeMs: summary(worker.map((w) => w.decodeMs)),
        workerReadMs: summary(worker.map((w) => w.readMs)),
        workerSwapMs: summary(worker.map((w) => w.swapMs)),
        workerTotalMs: summary(worker.map((w) => w.totalMs)),
        bytesPerTile: summary(worker.map((w) => w.bytes)),
        parallel: { tiles: tiles.length, wallMs: wall, tilesPerSec: tiles.length / (wall / 1000), latencyMs: summary(lat), inFlight: limit },
    };
}

/** 200 random tiles: geotiff.js bytes (as delivered) vs the node's HD PNG. */
async function identity(pool, geometry, client, imageId, { count = 200, seed = 11 } = {}) {
    const tiles = randomTiles(geometry, count, seed);
    const mismatches = [];
    let identical = 0;
    const hashes = [];
    for (const t of tiles) {
        const [mine, theirs] = await Promise.all([
            pool.tile(t.channel, t.level, t.x, t.y),
            client.tileBytes(imageId, t.channel, t.level, t.x, t.y).then(decodePng),
        ]);
        const same = mine.width === theirs.width && mine.height === theirs.height
            && firstDifference(mine.array, theirs.data) === -1;
        if (same) identical += 1;
        else mismatches.push({ ...t, mine: [mine.width, mine.height], node: [theirs.width, theirs.height],
                               firstDiff: firstDifference(mine.array, theirs.data) });
        if (hashes.length < 5) hashes.push({ ...t, sha256: await sha256(mine.array) });
    }
    return { count, identical, mismatches: mismatches.slice(0, 20), sampleHashes: hashes };
}

/** Auto-level in the page vs the node's /stats and /gmm, a few channels. */
async function autoLevel(pool, geometry, client, imageId, channels) {
    const level = chooseOverviewLevel(geometry.levels);
    const out = [];
    for (const channel of channels) {
        const t0 = performance.now();
        const plane = await pool.level(channel, level);
        const tRead = performance.now();
        const values = plane.bits === 16
            ? new Uint16Array(plane.array.buffer, plane.array.byteOffset, plane.array.byteLength / 2)
            : new Float32Array(plane.array.buffer);
        const ov = overviewOf(values, plane.width, plane.height);
        const stats = statsOf(ov.data);
        const tStats = performance.now();
        const gmm = gmmOf(ov.data);
        const tGmm = performance.now();
        const [nodeStats, nodeGmm] = await Promise.all([client.imageStats(imageId, channel), client.imageGmm(imageId, channel)]);
        out.push({
            channel, level, overview: [ov.width, ov.height],
            readMs: tRead - t0, provisionalMs: tStats - t0, gmmMs: tGmm - tStats, totalMs: tGmm - t0,
            browser: { vmin_hint: stats.vmin_hint, vmax_hint: stats.vmax_hint, image_min: stats.image_min,
                       image_max: stats.image_max, vmin: gmm.vmin, vmax: gmm.vmax, iterations: gmm.fit.iterations },
            node: { vmin_hint: nodeStats.vmin_hint, vmax_hint: nodeStats.vmax_hint, image_min: nodeStats.image_min,
                    image_max: nodeStats.image_max, vmin: nodeGmm.vmin, vmax: nodeGmm.vmax },
        });
    }
    return out;
}

async function p1Tiles({ image, node, poolSizes = [1, 2, 4, 8], decoders = ["native", "native-blob", "pako"], count = 200 }) {
    const out = { env: await env(), open: [], bench: [], decoders: [] };
    const client = new NodeClient(node.endpoint, node.token);
    for (const size of poolSizes) {
        status(`P1 pool ${size}`);
        const { pool, opened } = await p1Open({ image, poolSize: size });
        out.geometry ||= opened.geometry;
        out.open.push({ poolSize: size, wallMs: opened.wallMs, perWorker: opened.timings });
        out.bench.push({ poolSize: size, ...(await tileBench(pool, opened.geometry, { count })) });
        pool.terminate();
    }
    for (const decoder of decoders) {
        status(`P1 decoder ${decoder}`);
        const { pool, opened } = await p1Open({ image, poolSize: 1, decoder });
        out.decoders.push({ decoder, ...(await tileBench(pool, opened.geometry, { count: 50, seed: 3 })) });
        pool.terminate();
    }
    status("P1 identity");
    const { pool, opened } = await p1Open({ image, poolSize: 4 });
    out.identity = await identity(pool, opened.geometry, client, node.imageId, { count });
    status("P1 auto-level");
    const n = opened.geometry.numChannels;
    out.autoLevel = await autoLevel(pool, opened.geometry, client, node.imageId, [0, Math.floor(n / 2), n - 1]);
    pool.terminate();
    status("P1 done");
    return out;
}

/**
 * P1R: an OME-TIFF in a bucket read by the browser node over HTTP range
 * requests -- no node, no Python. Possible only where the bucket answers
 * CORS (the Orion bucket does; IDC's object reads do not, investigation §4.5).
 */
async function p1Remote({ url, poolSize = 4, count = 20, seed = 13 }) {
    const out = { url, env: await env() };
    const pool = new TiffPool(workerUrl("tiffWorker.js"), poolSize);
    const t0 = performance.now();
    const opened = await pool.open({ url, decoder: "native" });
    out.openMs = performance.now() - t0;
    out.openPerWorker = opened.timings;
    out.geometry = opened.geometry;
    const tiles = randomTiles(opened.geometry, count, seed);
    const latency = [];
    const worker = [];
    for (const t of tiles) {
        const s = performance.now();
        const r = await pool.tile(t.channel, t.level, t.x, t.y);
        latency.push(performance.now() - s);
        worker.push(r.timing);
    }
    out.tileMs = summary(latency);
    out.readMs = summary(worker.map((w) => w.readMs));
    out.decodeMs = summary(worker.map((w) => w.decodeMs));
    out.reads = summary(worker.map((w) => w.reads));
    out.bytes = summary(worker.map((w) => w.bytes));
    pool.terminate();
    return out;
}

// ---------------------------------------------------------------------------
// The viewer arms: the same renderer fed by the node (today's direct route)
// or by the browser node.

let current = null;

async function openArm({ arm, pane = "paneA", image, node, channels, poolSize = 4, labels = null }) {
    const client = new NodeClient(node.endpoint, node.token);
    let pool = null;
    let geometry;
    if (arm === "browser") {
        pool = new TiffPool(workerUrl("tiffWorker.js"), poolSize);
        geometry = (await pool.open({ file: pickedFile(image.file), decoder: "native" })).geometry;
    } else {
        // local.image_geometry's vocabulary: `levels` is a count and
        // `level_shapes` is [[height, width], ...].
        const g = await client.imageGeometry(node.imageId);
        geometry = {
            width: g.width, height: g.height, numChannels: g.num_channels, names: g.channel_names,
            levels: g.level_shapes.map(([h, w]) => ({ width: w, height: h })),
        };
    }
    const viewer = await createViewer({
        container: pane,
        geometry: { ...geometry, tileWidth: 1024, tileHeight: 1024 },
        channels,
        feed: arm,
        nodeTileUrl: (c, level, x, y) => client.tileUrl(node.imageId, c, level, x, y),
        browserTile: (c, level, x, y) => pool.tile(c, level, x, y),
        labels,
    });
    return { arm, viewer, pool, client, geometry };
}

async function geometryOf(image) {
    const pool = new TiffPool(workerUrl("tiffWorker.js"), 1);
    const { geometry } = await pool.open({ file: pickedFile(image.file) });
    pool.terminate();
    return geometry;
}

function closeArm(a) {
    if (!a) return;
    a.viewer.destroy();
    a.pool?.terminate();
}

/** Point the view at an image-pixel rectangle, instantly. */
function goTo(viewer, rect) {
    const item = viewer.world.getItemAt(0);
    const r = item.imageToViewportRectangle(rect.x, rect.y, rect.w, rect.h);
    viewer.viewport.fitBounds(r, true);
    viewer.forceRedraw();
}

/**
 * Somewhere with tissue, in full-resolution pixels: the brightest 9x9 box of
 * channel 0's coarsest level. A whole-slide image can be mostly background
 * (LSP11385 is two strips with nothing between them), and a comparison drawn
 * over background compares black with black.
 */
async function tissuePoint(image, geometry) {
    const pool = new TiffPool(workerUrl("tiffWorker.js"), 1);
    await pool.open({ file: pickedFile(image.file) });
    const level = geometry.levels.length - 1;
    const plane = await pool.level(0, level);
    pool.terminate();
    const v = new Uint16Array(plane.array.buffer, plane.array.byteOffset, plane.array.byteLength / 2);
    const { width, height } = plane;
    let best = -1, bx = 0, by = 0;
    const r = 4;
    for (let y = r; y < height - r; y += 2) {
        for (let x = r; x < width - r; x += 2) {
            let s = 0;
            for (let j = -r; j <= r; j += 1) for (let i = -r; i <= r; i += 1) s += v[(y + j) * width + x + i];
            if (s > best) { best = s; bx = x; by = y; }
        }
    }
    const scale = geometry.width / width;
    return { x: Math.round(bx * scale), y: Math.round(by * scale) };
}

/** A view of `w` x `h` image pixels centred on `p`, kept inside the image. */
function around(p, w, h, geometry) {
    return {
        x: Math.max(0, Math.min(geometry.width - w, Math.round(p.x - w / 2))),
        y: Math.max(0, Math.min(geometry.height - h, Math.round(p.y - h / 2))),
        w, h,
    };
}

/** A seeded tour: `steps` views at mixed zoom over the image. */
function tourSteps(geometry, steps, seed) {
    const rand = rng(seed);
    const out = [];
    for (let i = 0; i < steps; i += 1) {
        const scale = [1, 2, 4, 8, 16, 32][Math.floor(rand() * 6)];   // image px per screen px
        const w = Math.min(geometry.width, 720 * scale);
        const h = Math.min(geometry.height, 900 * scale);
        out.push({ x: Math.floor(rand() * Math.max(1, geometry.width - w)),
                   y: Math.floor(rand() * Math.max(1, geometry.height - h)), w, h, scale });
    }
    return out;
}

/** Frame intervals and draw cost while panning by `dx` of the view per frame. */
async function panFrames(viewer, { frames = 120, dx = 0.01 } = {}) {
    const drawer = viewer.drawer;
    const draws = [];
    const original = drawer.draw.bind(drawer);
    drawer.draw = (...args) => {
        const t0 = performance.now();
        const r = original(...args);
        draws.push(performance.now() - t0);
        return r;
    };
    const longTasks = [];
    let observer = null;
    try {
        observer = new PerformanceObserver((list) => { for (const e of list.getEntries()) longTasks.push(e.duration); });
        observer.observe({ type: "longtask" });
    } catch (e) { /* unsupported */ }
    const intervals = [];
    let last = await frame();
    const vp = viewer.viewport;
    for (let i = 0; i < frames; i += 1) {
        const bounds = vp.getBounds(true);
        vp.panBy(new OpenSeadragon.Point(bounds.width * dx * (i % 60 < 30 ? 1 : -1), 0), true);
        const t = await frame();
        intervals.push(t - last);
        last = t;
    }
    drawer.draw = original;
    observer?.disconnect();
    return { intervals: summary(intervals), over16: intervals.filter((v) => v > 16.7).length,
             over25: intervals.filter((v) => v > 25).length, frames,
             drawMs: summary(draws), longTasks: summary(longTasks) };
}

async function p1Viewer({ arm, image, node, channelCounts = [3, 7, 15], tour = 11, seed = 5,
                          channelRanges = null }) {
    const out = { arm, env: await env(), runs: [] };
    // Located once, before any timed block, from the local file (both arms
    // have it picked); the node arm's timings never include this read.
    const tissue = await tissuePoint(image, await geometryOf(image));
    out.tissue = tissue;
    for (const count of channelCounts) {
        status(`P1 viewer ${arm} ${count} ch`);
        const channels = [];
        for (let i = 0; i < count; i += 1) {
            const range = channelRanges?.[i] || [0, 0.2];
            channels.push({ index: i, color: PALETTE[i % PALETTE.length], range });
        }
        const t0 = performance.now();
        const a = await openArm({ arm, image, node, channels });
        const opened = performance.now() - t0;
        current = a;
        const homeFull = await waitFull(a.viewer.viewer, { timeoutMs: 120000 });
        const steps = tourSteps(a.geometry, tour, seed);
        const stepTimes = [];
        for (const step of steps) {
            goTo(a.viewer.viewer, step);
            stepTimes.push({ scale: step.scale, ...(await waitFull(a.viewer.viewer, { timeoutMs: 60000 })) });
        }
        // A mid-zoom pan over tissue, the measured-facts scenario.
        const mid = around(tissue, 720 * 4, 900 * 4, a.geometry);
        goTo(a.viewer.viewer, mid);
        await waitFull(a.viewer.viewer, { timeoutMs: 60000 });
        const pan = await panFrames(a.viewer.viewer);
        const afterPan = await waitFull(a.viewer.viewer, { timeoutMs: 60000 });
        out.runs.push({
            channels: count, openMs: opened, homeFull, steps: stepTimes,
            stepMs: summary(stepTimes.map((s) => s.ms)), allFull: stepTimes.every((s) => s.full),
            pan, afterPanFull: afterPan,
            browserTiles: a.viewer.tileLog.length ? summary(a.viewer.tileLog.map((t) => t.ms)) : null,
            jsHeap: performance.memory ? performance.memory.usedJSHeapSize : null,
        });
        closeArm(a);
        current = null;
        await sleep(200);
    }
    return out;
}

/** P0's picture check: both arms on one view, canvases compared. */
async function compareArms({ image, node, channels, view = null }) {
    const a = await openArm({ arm: "node", pane: "paneA", image, node, channels });
    const b = await openArm({ arm: "browser", pane: "paneB", image, node, channels });
    const W = a.geometry.width, H = a.geometry.height;
    const p = await tissuePoint(image, b.geometry);
    const views = view ? [view] : [
        { x: 0, y: 0, w: W, h: H },                      // home
        around(p, 720 * 8, 900 * 8, a.geometry),          // mid zoom, on tissue
        around(p, 720, 900, a.geometry),                  // native, on tissue
    ];
    const results = [];
    const compare = async () => {
        const pa = canvasPixels(a.viewer.viewer);
        const pb = canvasPixels(b.viewer.viewer);
        const width = a.viewer.viewer.drawer.canvas.width;
        let diff = 0, maxDelta = 0, nonBlack = 0;
        const box = [Infinity, Infinity, -1, -1];
        for (let i = 0; i < pa.length; i += 4) {
            const d = Math.max(Math.abs(pa[i] - pb[i]), Math.abs(pa[i + 1] - pb[i + 1]), Math.abs(pa[i + 2] - pb[i + 2]));
            if (d) {
                diff += 1;
                const p = i / 4, x = p % width, y = Math.floor(p / width);
                box[0] = Math.min(box[0], x); box[1] = Math.min(box[1], y);
                box[2] = Math.max(box[2], x); box[3] = Math.max(box[3], y);
            }
            if (d > maxDelta) maxDelta = d;
            if (pa[i] || pa[i + 1] || pa[i + 2]) nonBlack += 1;
        }
        // A 16 x 10 grid of mean luminance of the browser arm: what P8 compares
        // across engines, which rasterise the same tiles a little differently.
        const height = pb.length / 4 / width;
        const grid = new Array(160).fill(0), count = new Array(160).fill(0);
        for (let i = 0; i < pb.length; i += 4) {
            const p = i / 4, cell = Math.floor((Math.floor(p / width) * 10) / height) * 16 + Math.floor(((p % width) * 16) / width);
            grid[cell] += 0.2126 * pb[i] + 0.7152 * pb[i + 1] + 0.0722 * pb[i + 2];
            count[cell] += 1;
        }
        return { pixels: pa.length / 4, nonBlack, differing: diff, maxDelta, diffBox: diff ? box : null,
                 sameSize: pa.length === pb.length, shaA: await sha256(pa), shaB: await sha256(pb),
                 lumaGrid: grid.map((g, k) => Math.round((g / Math.max(1, count[k])) * 10) / 10) };
    };
    for (const r of views) {
        goTo(a.viewer.viewer, r);
        goTo(b.viewer.viewer, r);
        const [fa, fb] = await Promise.all([waitFull(a.viewer.viewer), waitFull(b.viewer.viewer)]);
        await frame();
        const now = await compare();
        // Again after a settle: a difference that goes away was a frame
        // caught mid-draw, not a different picture.
        await sleep(1500);
        a.viewer.viewer.forceRedraw();
        b.viewer.viewer.forceRedraw();
        await frame(); await frame();
        const settled = await compare();
        results.push({ rect: r, fullA: fa.full, fullB: fb.full, ...settled, atFull: { differing: now.differing, maxDelta: now.maxDelta, diffBox: now.diffBox } });
    }
    closeArm(a);
    closeArm(b);
    return results;
}

// ---------------------------------------------------------------------------
// P6: the product's own tour (tools/bench_remote/tour.mjs) on the browser node

/**
 * tourpage.js's completion test, on this viewer: the step is done once two
 * redraws have followed the move, the springs are at rest, and every item is
 * fully loaded at the level it wants -- held for two polls 5 ms apart. The
 * time is when that run began, as tourpage reports it.
 */
async function waitTourStep(viewer, t0, u0, ms = 120000) {
    const end = t0 + ms;
    let since = null;
    let polls = 0;
    while (performance.now() < end) {
        const vp = viewer.viewport;
        const still = [vp.zoomSpring, vp.centerSpringX, vp.centerSpringY].every((s) => !s || s.isAtTargetValue());
        const world = viewer.world;
        let full = world.getItemCount() > 0;
        for (let i = 0; i < world.getItemCount(); i += 1) if (!world.getItemAt(i).getFullyLoaded()) { full = false; break; }
        if (full && still && viewer.__updates >= u0 + 2) {
            since ??= performance.now();
            polls += 1;
            if (polls >= 2) return since - t0;
        } else {
            since = null;
            polls = 0;
        }
        await new Promise((r) => setTimeout(r, 5));
    }
    return null;
}

/**
 * The tour's steps, in its order (z steps left out: a 2-D image), on a pane of
 * the product viewer's size, fed by the browser node. `open` is from creating
 * the viewer (the file already picked) to full; the page's own load is
 * reported beside it.
 */
async function p6Tour({ image, channels, waypoints: W, size = [1060, 855], timeoutMs = 120000, poolSize = 4 }) {
    const pane = document.createElement("div");
    pane.id = `p6pane${Date.now()}`;
    pane.style.cssText = `position:fixed;left:0;top:0;width:${size[0]}px;height:${size[1]}px;background:#000;z-index:5`;
    document.body.appendChild(pane);
    const steps = [];
    const t0 = performance.now();
    const a = await openArm({ arm: "browser", pane: pane.id, image, node: { endpoint: "http://127.0.0.1:9", token: "" },
                              channels, poolSize });
    const viewer = a.viewer.viewer;
    // The product's main viewer keeps OpenSeadragon's springs (1.2 s, stiffness
    // 6.5; imageViewer.js sets neither); this page turns them off. The tour's
    // animated steps are only comparable with the product's.
    for (const spring of [viewer.viewport.zoomSpring, viewer.viewport.centerSpringX, viewer.viewport.centerSpringY]) {
        spring.animationTime = 1.2;
        spring.springStiffness = 6.5;
    }
    viewer.__updates = 0;
    viewer.addHandler("update-viewport", () => { viewer.__updates += 1; });
    steps.push({ step: "open", full: await waitTourStep(viewer, t0, 0, timeoutMs) });
    const item = () => viewer.world.getItemAt(0);
    const place = (ratio, at) => {
        viewer.viewport.zoomTo(item().imageToViewportZoom(ratio), null, true);
        viewer.viewport.panTo(item().imageToViewportCoordinates(at[0], at[1]), true);
        viewer.viewport.applyConstraints(true);
    };
    const step = async (into, name, move) => {
        const s = performance.now();
        const u0 = viewer.__updates;
        move();
        into.push({ step: name, full: await waitTourStep(viewer, s, u0, timeoutMs) });
    };
    const tour = async (into) => {
        await step(into, "zoom_L2_a", () => place(0.25, W.a));
        await step(into, "zoom_L0_a", () => place(1, W.a));
        await step(into, "pan_L0_b", () => place(1, W.b));
        await step(into, "home", () => viewer.viewport.goHome(true));
        await step(into, "revisit_L0_a", () => place(1, W.a));
        await step(into, "jump_L0_c", () => place(1, W.c));
        await step(into, "anim_zoom_L2", () => {
            viewer.viewport.panTo(item().imageToViewportCoordinates(W.b[0], W.b[1]), true);
            viewer.viewport.zoomTo(item().imageToViewportZoom(0.25));
        });
        await step(into, "anim_pan_L2", () => viewer.viewport.panTo(item().imageToViewportCoordinates(W.c[0], W.c[1])));
    };
    await tour(steps);
    // Revisit: the same tour again in the same viewer (tiles in its caches),
    // from the home view -- beside the product's C2.
    const revisit = [];
    await step(revisit, "open", () => viewer.viewport.goHome(true));
    await tour(revisit);
    const nav = performance.getEntriesByType("navigation")[0];
    const out = { steps, revisit, canvas: [viewer.drawer.canvas.width, viewer.drawer.canvas.height],
                  pageLoadMs: nav ? nav.loadEventEnd : null, jsHeap: performance.memory?.usedJSHeapSize ?? null };
    closeArm(a);
    pane.remove();
    return out;
}

window.POC = {
    env, p0, p1Tiles, p1Viewer, p1Remote, compareArms, probeU16Shader, p3, p3Ingest, p3Reopen, p2Viewer, p2bBuild, p2bIdentity, p2bReopen, buildPyramid, pyramidReopen, p7Identity, p4,
    p5Protocol, p5Reach, p5Isolation, p5Poll, p5Pair, p5Rnode, p5Lna, p5LnaStart, p5LnaPoll, p6Tour,
    // Building blocks for the driver's other POCs (p2, p3, p5 modules add theirs).
    lib: { NodeClient, TiffPool, createViewer, waitFull, summary, rng, decodePng, sha256, randomTiles,
           openArm, closeArm, goTo, panFrames, tourSteps, PALETTE, pickedFile, workerUrl, env },
};
status("ready");
