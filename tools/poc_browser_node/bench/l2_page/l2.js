// L2's page: openslide-js (OpenSlide 4.0.0 in WebAssembly) opening the
// OpenSlide test slides the driver fetched, reading the regions openslide-python
// read (bench/truth_l2.py), and timing both. Served cross-origin isolated by
// tools/browser_build/serve.py, with the package at /osjs/ and the slides at /fx/.
//
//   window.l2.init({workers})            -> {ms, version, isolated}
//   window.l2.fetch(files)               -> {ms, bytes}: [{path, url}] into Files
//   window.l2.open({main, multi})        -> {ms, levels, dims, downsamples, properties, associated}
//   window.l2.read(regions, {keep})      -> [{i, ms, sha, bytes (base64 RGBA when keep)}]
//   window.l2.parallel(regions, n)       -> {ms, tiles}: n reads in flight at a time
import { OpenSlide } from "/osjs/dist/esm/index.js";

let os = null;
let files = [];
let slide = null;
let extra = [];

async function sha(buffer) {
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest).slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
}

function base64(bytes) {
    let text = "";
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) text += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    return btoa(text);
}

window.l2 = {
    async init({ workers = 4 } = {}) {
        const t0 = performance.now();
        os = await OpenSlide.initialize({ workerCount: workers });
        const ms = performance.now() - t0;
        return { ms, version: await os.getVersion(), isolated: self.crossOriginIsolated };
    },
    async fetch(list) {
        const t0 = performance.now();
        let bytes = 0;
        files = await Promise.all(list.map(async ({ path, url }) => {
            const blob = await (await fetch(url)).blob();
            bytes += blob.size;
            return { path, file: new File([blob], path.split("/").pop()) };
        }));
        return { ms: performance.now() - t0, bytes, count: files.length };
    },
    async open({ main, multi }) {
        if (slide) { await slide.close(); slide = null; }
        const t0 = performance.now();
        const source = multi ? files : files.find((f) => f.path === main).file;
        // A multi-file slide: the index file first, so the mount knows which one
        // to open (openslide-js takes the first entry ending in a known index
        // suffix; ordering it first makes that unambiguous).
        if (multi) source.sort((a, b) => (a.path === main ? -1 : b.path === main ? 1 : 0));
        let vendor = null;
        try { vendor = await os.detectVendor(source); } catch (error) { vendor = `error: ${error.message}`; }
        const t1 = performance.now();
        slide = await os.open(source);
        const ms = performance.now() - t1;
        return {
            ms, detectMs: t1 - t0, vendor,
            levels: slide.levelCount,
            dims: slide.levelDimensions.map((d) => [d.width, d.height]),
            downsamples: Array.from(slide.levelDownsamples),
            properties: Object.fromEntries(slide.properties),
            associated: Array.from(slide.associatedImageNames).sort(),
        };
    },
    async read(regions, { keep = true } = {}) {
        const out = [];
        for (const r of regions) {
            const t0 = performance.now();
            const image = await slide.readRegion(r.x, r.y, r.level, r.w, r.h);
            const ms = performance.now() - t0;
            const bytes = new Uint8Array(image.data.buffer, image.data.byteOffset, image.data.byteLength);
            out.push({ i: r.i, ms, w: image.width, h: image.height, sha: await sha(bytes),
                       bytes: keep ? base64(bytes) : null });
        }
        return out;
    },
    async parallel(regions, n) {
        const t0 = performance.now();
        let next = 0;
        const lanes = Array.from({ length: n }, async () => {
            while (next < regions.length) {
                const r = regions[next++];
                await slide.readRegion(r.x, r.y, r.level, r.w, r.h);
            }
        });
        await Promise.all(lanes);
        return { ms: performance.now() - t0, tiles: regions.length };
    },
    //: The slide opened `k` times more (openslide-js reads one opened slide
    //: on one worker; a reader wanting several workers opens it on each).
    async openMany(k, { main, multi }) {
        const t0 = performance.now();
        const source = multi ? files : files.find((f) => f.path === main).file;
        extra = await Promise.all(Array.from({ length: k }, () => os.open(source)));
        return { ms: performance.now() - t0, opened: extra.length };
    },
    async spread(regions) {
        const all = [slide, ...extra];
        const t0 = performance.now();
        const times = await Promise.all(regions.map(async (r, i) => {
            const t = performance.now();
            await all[i % all.length].readRegion(r.x, r.y, r.level, r.w, r.h);
            return performance.now() - t;
        }));
        return { ms: performance.now() - t0, tiles: regions.length, slides: all.length, each: times };
    },
    async close() {
        for (const s of extra) await s.close();
        extra = [];
        if (slide) await slide.close();
        slide = null;
        files = [];
    },
};
document.getElementById("state").textContent = "ready";
