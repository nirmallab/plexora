// frag.glsl rebuilds an HD tile's 16-bit value exactly: pixel parity with the
// arithmetic, in a real browser.
//
//   node tests/js/hd_u16_shader_parity.mjs [--headed] [--frag <file>] [--out results.json]
//
// A page is served from the repo through Playwright's router (no server): the
// shipped glRenderer.js (as a module, the way vendor.js exposes it), glInit.js,
// vert.glsl and frag.glsl. glInit's own renderer is built and fed rows of
// uint16 values the way the HD path feeds them (big-endian bytes, uploaded as
// RG8UI by loadArray), drawn white through u16_rg_range with no curve, and read
// back. The red byte must be round(255 * clamp((v / 65535 - lo) / (hi - lo)))
// within one step (float32 on the GPU against float64 here):
//
//   * a ramp k * 257 over the full window (high byte = low byte = k);
//   * a ramp across a tight window high in the range [0.50, 0.55], a bright
//     channel levelled tight, where a value short by its high byte is up to 11
//     levels dark;
//   * values whose high and low bytes differ (0xff00, 0x80ff, ...) in windows
//     around each;
//   * 4096 seeded random values in seeded random windows.
//
// --frag draws through another copy of the shader (to show the test fails on
// the old `r * 255 + g`). Headless Chromium renders WebGL with SwiftShader;
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
const fragIndex = argv.indexOf("--frag");
const PW = findPlaywright();
if (!PW) {
    console.log("SKIP playwright not found");
    process.exit(0);
}
const { chromium } = await import(PW);

const ORIGIN = "http://u16.test";
const FILES = {
    "/glRenderer.js": path.join(CLIENT, "js", "services", "glRenderer.js"),
    "/glInit.js": path.join(CLIENT, "js", "views", "glInit.js"),
    "/client/src/shaders/vert.glsl": path.join(CLIENT, "shaders", "vert.glsl"),
    "/client/src/shaders/frag.glsl": fragIndex >= 0 ? path.resolve(argv[fragIndex + 1])
        : path.join(CLIENT, "shaders", "frag.glsl"),
};
const PAGE = `<!doctype html><html><body>
<script>window.plexoraUrl = (p) => "/" + p;</script>
<script type="module">import { GLRenderer } from "/glRenderer.js"; window.GLRenderer = GLRenderer;
const s = document.createElement("script"); s.src = "/glInit.js"; document.body.appendChild(s);</script>
</body></html>`;

// Everything below runs in the page.
async function inPage() {
    while (!window.PlexoraGL) await new Promise((r) => setTimeout(r, 10));
    let resolveReady;
    const ready = new Promise((r) => { resolveReady = r; });
    let renderer = null;
    renderer = window.PlexoraGL.createGLRenderer({
        // ImageViewer.indexOfTexture's constant units, so every sampler sits on
        // a unit of its own.
        indexOfTexture: (label) => {
            const i = ["ids", "centers", "ranges", "pickings"].indexOf(label);
            return i > -1 ? i + renderer._otherOffset : -1;
        },
        // ImageViewer.selectTexture.
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
    const w = 256, h = 1;
    renderer.width = w;
    renderer.height = h;
    renderer.updateShape(w, h);
    await renderer.ready();
    await ready;
    const gl = renderer.gl;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const rendererName = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);

    let key = 0;
    /** Draw up to 256 values in one window; the red bytes read back and the
     *  bytes the arithmetic says. */
    function draw(values, range) {
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
        const tileKey = `t${key++}`;
        const source = { format: 16, getTileKey: () => tileKey };
        renderer.loadArray({ tiledImage: { source }, tile: { level: 0, x: 0, y: 0, _array: bytes, _format: "u16" } }, w, h);
        const px = new Uint8Array(w * h * 4);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
        const lo = Math.fround(range[0]), hi = Math.fround(range[1]);
        return values.map((v, k) => ({
            v, red: px[4 * k],
            want: Math.round(Math.min(1, Math.max(0, (v / 65535 - lo) / (hi - lo))) * 255),
        }));
    }
    function summarise(name, rows) {
        const errors = rows.map((r) => r.red - r.want);
        const worst = rows.reduce((a, r) => (Math.abs(r.red - r.want) > Math.abs(a.red - a.want) ? r : a), rows[0]);
        return { name, n: rows.length, maxAbs: Math.abs(worst.red - worst.want),
                 mean: errors.reduce((a, b) => a + b, 0) / errors.length, worst };
    }

    const cases = [];
    cases.push(summarise("ramp k*257 over the full window",
        draw(Array.from({ length: w }, (_, k) => k * 257), [0, 1])));
    const lo = 0.5, hi = 0.55;
    cases.push(summarise("ramp across a tight bright window [0.50, 0.55]",
        draw(Array.from({ length: w }, (_, k) => Math.round((lo + (hi - lo) * k / (w - 1)) * 65535)), [lo, hi])));
    const split = [];
    for (const v of [0xff00, 0x80ff, 0x7f80, 0xfe01, 0x0100, 0x00ff, 0xffff, 0x1234, 0xabcd]) {
        const c = v / 65535;
        split.push(...draw([v], [Math.max(0, c - 0.004), Math.min(1, c + 0.004)]));
    }
    cases.push(summarise("high and low bytes differ, windows around each", split));
    let seed = 20261105;
    const rand = () => { seed = (seed * 1103515245 + 12345) >>> 0; return seed / 4294967296; };
    const random = [];
    for (let block = 0; block < 16; block += 1) {
        const a = rand(), b = a + 0.002 + rand() * (1 - a);
        const values = Array.from({ length: w }, () => Math.floor(rand() * 65536));
        random.push(...draw(values, [a, Math.min(1, b)]));
    }
    cases.push(summarise("4096 random values in random windows", random));
    return { rendererName, cases };
}

const browser = await chromium.launch({
    headless: !headed,
    args: headed ? ["--use-angle=d3d11", "--enable-gpu"] : ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
let report;
try {
    const page = await browser.newPage();
    const messages = [];
    page.on("console", (m) => messages.push(m.text()));
    page.on("pageerror", (e) => messages.push(String(e)));
    await page.route(`${ORIGIN}/**`, (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === "/") return route.fulfill({ body: PAGE, contentType: "text/html" });
        const file = FILES[url.pathname];
        if (!file) return route.fulfill({ status: 404, body: "" });
        const type = file.endsWith(".js") ? "text/javascript" : "text/plain";
        return route.fulfill({ body: fs.readFileSync(file, "utf8"), contentType: type });
    });
    await page.goto(`${ORIGIN}/`);
    report = await page.evaluate(`(${inPage.toString()})()`);
    if (messages.length) console.log("page messages: " + messages.join(" | "));
} finally {
    await browser.close();
}

console.log(`renderer ${report.rendererName}`);
let failed = 0;
for (const c of report.cases) {
    const ok = c.maxAbs <= 1;
    if (!ok) failed += 1;
    console.log(`${ok ? "PASS" : "FAIL"} ${c.name} | n ${c.n} | max |error| ${c.maxAbs} | mean ${c.mean.toFixed(3)}`
        + (ok ? "" : ` | worst v=${c.worst.v} red ${c.worst.red} want ${c.worst.want}`));
}
if (outIndex >= 0) fs.writeFileSync(argv[outIndex + 1], JSON.stringify(report, null, 1));
console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
