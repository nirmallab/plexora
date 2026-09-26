// The GPU cell layer draws what renderLabelTile draws: pixel parity in a real
// browser, over the cases that decide a pixel.
//
//   node tests/js/label_gpu_parity.mjs [--headed] [--out results.json]
//
// A page is served from the repo through Playwright's router (no server): the
// shipped glInit.js, labelTile.js and labelGpu.js, and the shipped vert.glsl and
// label.frag.glsl. In it, a WebGL2 renderer is set up the way GLRenderer sets
// one up (a quad in an ARRAY_BUFFER bound to attribute 0 of a "channel"
// program), and every case is drawn twice into a fresh 2D canvas: once through
// renderLabelTile and a globalAlpha blit (tileColorize's CPU branch), once
// through labelGpu.drawTile (its GPU branch). Compared twice:
//
//   * over transparent: ALPHA must be identical, pixel for pixel, for a layer
//     blitted at full opacity. Blitted at a fractional globalAlpha, the 2D
//     canvas composites a WebGL source and a 2D source through different
//     roundings (outside both renderers), so there it may differ by 1;
//   * over opaque black: RGB, which is then the premultiplied colour, within 1
//     (the two paths premultiply through different 8-bit roundings).
//
// After the GPU draws, a draw through the "channel" program checks the context
// was handed back intact (program, viewport, unpack flip).
//
// Headless Chromium renders WebGL with SwiftShader: this is the no-GPU case.
// --headed uses the machine's GPU.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const CLIENT = path.join(REPO, "plexora", "client", "src");

function findPlaywright() {
    if (process.env.PLAYWRIGHT_MODULE) return process.env.PLAYWRIGHT_MODULE;
    const local = path.join(REPO, "plexora", "client", "node_modules", "playwright", "index.mjs");
    if (fs.existsSync(local)) return pathToFileURL(local).href;
    const cache = path.join(os.homedir(), "AppData", "Local", "npm-cache", "_npx");
    for (const dir of fs.existsSync(cache) ? fs.readdirSync(cache) : []) {
        const entry = path.join(cache, dir, "node_modules", "playwright", "index.mjs");
        if (fs.existsSync(entry)) return pathToFileURL(entry).href;
    }
    return null;
}

const argv = process.argv.slice(2);
const headed = argv.includes("--headed");
const outIndex = argv.indexOf("--out");
const PW = findPlaywright();
if (!PW) {
    console.log("SKIP playwright not found");
    process.exit(0);
}
const { chromium } = await import(PW);

const ORIGIN = "http://parity.test";
const FILES = {
    "/glInit.js": path.join(CLIENT, "js", "views", "glInit.js"),
    "/labelTile.js": path.join(CLIENT, "js", "views", "labelTile.js"),
    "/labelGpu.js": path.join(CLIENT, "js", "views", "labelGpu.js"),
    "/vert.glsl": path.join(CLIENT, "shaders", "vert.glsl"),
    "/label.frag.glsl": path.join(CLIENT, "shaders", "label.frag.glsl"),
};
const PAGE = `<!doctype html><html><body>
<script src="/glInit.js"></script><script src="/labelTile.js"></script><script src="/labelGpu.js"></script>
</body></html>`;

// Everything below runs in the page.
async function inPage() {
    const T = window.PlexoraLabelTile;
    const G = window.PlexoraLabelGpu;

    // --- a renderer shaped like GLRenderer after init() -------------------
    const gl = document.createElement("canvas").getContext("webgl2");
    const renderer = {
        gl, width: 0, height: 0, buffer: gl.createBuffer(), program: null,
        updateShape(w, h) {
            this.width = w; this.height = h;
            gl.canvas.width = w; gl.canvas.height = h;
            gl.viewport(0, 0, w, h);
        },
    };
    renderer.updateShape(1024, 1024);
    const vs = await (await fetch("/vert.glsl")).text();
    const channelFs = `#version 300 es
precision highp float; in vec2 uv; out vec4 color;
void main() { color = vec4(uv, 0.25, 1.0); }`;
    const sh = (kind, src) => { const s = gl.createShader(kind); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const channel = gl.createProgram();
    gl.attachShader(channel, sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(channel, sh(gl.FRAGMENT_SHADER, channelFs));
    gl.linkProgram(channel);
    gl.useProgram(channel);
    renderer.program = channel;
    gl.bindBuffer(gl.ARRAY_BUFFER, renderer.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 1, 0, 0, 1, 1, 1, 0]), gl.STATIC_DRAW);
    const aUv = gl.getAttribLocation(channel, "a_uv");
    gl.enableVertexAttribArray(aUv);
    gl.vertexAttribPointer(aUv, 2, gl.FLOAT, false, 8, 0);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);

    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const rendererName = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    const gpu = G.createLabelGpu({ renderer, vShaderUrl: "/vert.glsl", fShaderUrl: "/label.frag.glsl", labelTile: T });
    await gpu.ready;
    if (!gpu.active) return { error: gpu.reason, rendererName };

    // --- synthetic label tiles ---------------------------------------------
    let seed = 12345;
    const rand = () => { seed = (seed * 1103515245 + 12345) >>> 0; return seed / 4294967296; };
    /** Cells of about `size` px with ragged edges, background holes, a
     *  checkerboard corner (diagonal-only contacts) and single-pixel cells;
     *  ids start at `base` so multi-byte ids are exercised. */
    function makeTile(w, h, size, base) {
        const ids = new Uint32Array(w * h);
        const cols = Math.ceil(w / size) + 2;
        const jitter = [];
        for (let i = 0; i < 4096; i += 1) jitter.push(Math.floor(rand() * Math.max(1, size / 2)));
        for (let y = 0; y < h; y += 1) {
            for (let x = 0; x < w; x += 1) {
                const gx = Math.floor((x + jitter[y % 4096]) / size);
                const gy = Math.floor((y + jitter[(x * 7) % 4096]) / size);
                let id = base + gy * cols + gx;
                if ((gx * 31 + gy * 17) % 11 === 0) id = 0;             // background
                if (x < 12 && y < 12) id = ((x + y) % 2) ? base + 5 : base + 9;  // checkerboard
                if (x === w - 3 && y % 5 === 0) id = base + 100000 + y;  // single pixels
                ids[y * w + x] = id;
            }
        }
        const bytes = new Uint8Array(w * h * 4);
        for (let p = 0; p < ids.length; p += 1) {
            const v = ids[p];
            bytes[p * 4] = v & 255; bytes[p * 4 + 1] = (v >>> 8) & 255; bytes[p * 4 + 2] = (v >>> 16) & 255;
        }
        let maxId = 0;
        for (const v of ids) if (v > maxId) maxId = v;
        return { _array: bytes, _labelWidth: w, _labelHeight: h, ids, maxId, cacheKey: `t${base}-${w}x${h}-${size}` };
    }

    function denseLut(maxId, { alphaZeroEvery = 0, alphaFrom = 40 } = {}) {
        const colors = new Uint8Array(4 * (maxId + 1));
        for (let id = 1; id <= maxId; id += 1) {
            const o = id * 4;
            colors[o] = (id * 37) & 255; colors[o + 1] = (id * 91) & 255; colors[o + 2] = (id * 53) & 255;
            colors[o + 3] = alphaZeroEvery && id % alphaZeroEvery === 0 ? 0 : alphaFrom + (id * 13) % (256 - alphaFrom);
        }
        return { colors, maxId };
    }
    function sparseLut(tile) {
        const map = new Map();
        for (const id of new Set(tile.ids)) {
            if (!id || id % 3 === 0) continue;       // a third of the cells undescribed
            map.set(id, [(id * 7) & 255, (id * 11) & 255, (id * 13) & 255, id % 4 === 0 ? 0 : 255]);
        }
        return { map };
    }
    function gateOf(tile, keep) {
        const mask = new Uint8Array(tile.maxId + 1);
        for (const id of new Set(tile.ids)) if (id && keep(id)) mask[id] = 1;
        return mask;
    }

    function canvas2d(w, h) {
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        const ctx = c.getContext("2d");
        ctx.imageSmoothingEnabled = false;
        return ctx;
    }

    function renderBoth(tile, layers, segmentationMode, background) {
        const w = tile._labelWidth;
        const h = tile._labelHeight;
        const cpu = canvas2d(w, h);
        const gpuCtx = canvas2d(w, h);
        for (const ctx of [cpu, gpuCtx]) {
            if (background) { ctx.fillStyle = "black"; ctx.fillRect(0, 0, w, h); }
        }
        const t0 = performance.now();
        for (const layer of layers) {
            const ctx = T.renderLabelTile(tile._array, w, h, layer, segmentationMode);
            cpu.globalAlpha = layer.opacity;
            cpu.drawImage(ctx.canvas, 0, 0, w, h);
        }
        const cpuMs = performance.now() - t0;
        tile._fillWeight = segmentationMode === "filled" ? T.fillWeightOf(tile._array, w, h) : undefined;
        const t1 = performance.now();
        for (const layer of layers) {
            gpuCtx.globalAlpha = layer.opacity;
            if (!gpu.drawTile(tile, layer, gpuCtx, w, h, segmentationMode)) throw new Error("GPU draw failed");
        }
        const a = cpu.getImageData(0, 0, w, h).data;
        const b = gpuCtx.getImageData(0, 0, w, h).data;
        const gpuMs = performance.now() - t1;
        return { a, b, cpuMs, gpuMs };
    }

    function compare(tile, layers, segmentationMode) {
        const over = renderBoth(tile, layers, segmentationMode, false);
        let alphaDiff = 0; let alphaMax = 0; let drawn = 0;
        for (let i = 3; i < over.a.length; i += 4) {
            const d = Math.abs(over.a[i] - over.b[i]);
            if (d) alphaDiff += 1;
            if (d > alphaMax) alphaMax = d;
            if (over.a[i]) drawn += 1;
        }
        const black = renderBoth(tile, layers, segmentationMode, true);
        let rgbDiff = 0; let rgbMax = 0;
        for (let i = 0; i < black.a.length; i += 4) {
            for (let k = 0; k < 3; k += 1) {
                const d = Math.abs(black.a[i + k] - black.b[i + k]);
                if (d > 1) rgbDiff += 1;
                if (d > rgbMax) rgbMax = d;
            }
        }
        return { alphaDiff, alphaMax, rgbDiff, rgbMax, drawn, pixels: tile._labelWidth * tile._labelHeight,
                 fill: tile._fillWeight ?? null, cpuMs: over.cpuMs, gpuMs: over.gpuMs };
    }

    // --- the matrix ----------------------------------------------------------
    const geometries = [
        ["big cells 512x512", makeTile(512, 512, 40, 70000)],
        ["6 px cells 512x512", makeTile(512, 512, 6, 1)],
        ["16 px cells 256x256, partial fill", makeTile(256, 256, 16, 300)],
        ["3 px cells 300x200 edge tile", makeTile(300, 200, 3, 8000000)],
    ];
    const results = [];
    for (const [geometry, tile] of geometries) {
        const lut = denseLut(tile.maxId, { alphaZeroEvery: 7 });
        const cases = [
            ["white, no gate", [{ name: "core", mode: "outlines", lut: null, opacity: 1 }]],
            ["gate mask", [{ name: "g", mode: "outlines", lut: null, opacity: 1,
                             gateMask: gateOf(tile, (id) => id % 2 === 0) }]],
            ["gate as an id Set", [{ name: "s", mode: "outlines", lut: null, opacity: 1,
                                    filterIds: new Set([...new Set(tile.ids)].filter((id) => id % 5 !== 0)) }]],
            ["dense colours, alpha-0 category", [{ name: "c", mode: "outlines", lut, opacity: 1 }]],
            ["sparse colours", [{ name: "m", mode: "outlines", lut: sparseLut(tile), opacity: 1 }]],
            ["filled mode, colours", [{ name: "f", mode: "filled", lut, opacity: 1 }]],
            ["one layer at opacity 0.45", [{ name: "o", mode: "outlines", lut, opacity: 0.45 }]],
            ["two layers stacked, opacities", [
                { name: "under", mode: "filled", lut, opacity: 0.45 },
                { name: "over", mode: "outlines", lut: null, opacity: 0.8,
                  gateMask: gateOf(tile, (id) => id % 3 === 1) }]],
        ];
        for (const segmentationMode of ["filled", "outlines"]) {
            for (const [name, layers] of cases) {
                const r = compare(tile, layers, segmentationMode);
                results.push({ geometry, case: name, segmentationMode,
                               fractionalOpacity: layers.some((l) => l.opacity < 1), ...r });
            }
        }
    }

    // The context is the channel program's again, and it draws.
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    const px = new Uint8Array(4);
    gl.readPixels(renderer.width - 1, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
    const handedBack = {
        program: gl.getParameter(gl.CURRENT_PROGRAM) === channel,
        viewport: Array.from(gl.getParameter(gl.VIEWPORT)).join(",") === `0,0,${renderer.width},${renderer.height}`,
        flipY: gl.getParameter(gl.UNPACK_FLIP_Y_WEBGL) === true,
        channelDraws: px[0] > 200 && px[1] < 20 && Math.abs(px[2] - 64) <= 1,
        glError: gl.getError(),
    };

    // Timing at the real tile size, one layer, outlines derived: the work a
    // gate tick does per tile on each path.
    const big = makeTile(1024, 1024, 8, 1);
    const layer = { name: "t", mode: "outlines", lut: denseLut(big.maxId), opacity: 1,
                    gateMask: gateOf(big, (id) => id % 2 === 0) };
    const reps = 5;
    const timing = { cpu: [], gpu: [] };
    const target = canvas2d(1024, 1024);
    big._fillWeight = T.fillWeightOf(big._array, 1024, 1024);
    for (let i = 0; i < reps; i += 1) {
        layer.gateMask = gateOf(big, (id) => (id + i) % 2 === 0);
        let t = performance.now();
        const ctx = T.renderLabelTile(big._array, 1024, 1024, layer, "filled");
        target.clearRect(0, 0, 1024, 1024);
        target.drawImage(ctx.canvas, 0, 0);
        target.getImageData(0, 0, 1, 1);
        timing.cpu.push(performance.now() - t);
        t = performance.now();
        target.clearRect(0, 0, 1024, 1024);
        gpu.drawTile(big, layer, target, 1024, 1024, "filled");
        target.getImageData(0, 0, 1, 1);   // forces the GPU work to finish
        timing.gpu.push(performance.now() - t);
    }
    return { rendererName, results, handedBack, timing, stats: gpu.stats };
}

const browser = await chromium.launch({
    headless: !headed,
    args: headed ? ["--ignore-gpu-blocklist"] : [],
});
let report;
try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") errors.push(m.text()); });
    await page.route(`${ORIGIN}/**`, (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === "/") return route.fulfill({ body: PAGE, contentType: "text/html" });
        const file = FILES[url.pathname];
        if (!file) return route.fulfill({ status: 404, body: "" });
        return route.fulfill({ body: fs.readFileSync(file, "utf8"),
                               contentType: file.endsWith(".js") ? "text/javascript" : "text/plain" });
    });
    await page.goto(`${ORIGIN}/`);
    report = await page.evaluate(`(${inPage.toString()})()`);
    report.headed = headed;
    report.errors = errors;
} finally {
    await browser.close();
}

if (outIndex >= 0) fs.writeFileSync(argv[outIndex + 1], JSON.stringify(report, null, 1));
if (report.error) {
    console.log(`FAIL the GPU path did not come up: ${report.error} (${report.rendererName})`);
    process.exit(1);
}
console.log(`renderer ${report.rendererName}`);
let failed = 0;
for (const r of report.results) {
    const ok = (r.fractionalOpacity ? r.alphaMax <= 1 : r.alphaDiff === 0) && r.rgbDiff === 0;
    if (!ok) failed += 1;
    console.log(`${ok ? "PASS" : "FAIL"} ${r.geometry} | ${r.segmentationMode} | ${r.case}`
        + `  (drawn ${r.drawn}/${r.pixels}, fill ${r.fill === null ? "-" : r.fill.toFixed(3)},`
        + ` alpha diffs ${r.alphaDiff} max ${r.alphaMax}, rgb>1 ${r.rgbDiff} max ${r.rgbMax})`);
}
const hb = report.handedBack;
const handed = hb.program && hb.viewport && hb.flipY && hb.channelDraws && hb.glError === 0;
if (!handed) failed += 1;
console.log(`${handed ? "PASS" : "FAIL"} the context is handed back to the channel program  ${JSON.stringify(hb)}`);
const med = (xs) => xs.slice().sort((a, b) => a - b)[Math.floor(xs.length / 2)];
console.log(`TIMING 1024x1024 tile, one layer: cpu ${med(report.timing.cpu).toFixed(1)} ms, `
    + `gpu ${med(report.timing.gpu).toFixed(1)} ms (median of ${report.timing.cpu.length})`);
if (report.errors.length) console.log(`page messages: ${report.errors.slice(0, 5).join(" | ")}`);
console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
