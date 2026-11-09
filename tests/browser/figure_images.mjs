// Figure Builder keeps its pictures across a render
// (plexora/plugins/figure_builder/tests/test_figure_builder_kept_images.py).
//
// The figure canvas and its tray are drawn from markup on every document
// change, selection and zoom step. Written with innerHTML, every render made a
// new <img> per panel: a request each, and a white panel until it decoded. On
// the website, where the page's own Python answers one request at a time, a
// drag left a page of white rectangles. FigureCanvas.replaceKeepingImages
// carries the pictures over; this drives it in a real Chrome, against a server
// that answers each picture slowly and counts the asks:
//
//   kept     the same markup again (a style changed): the same elements, no
//            request; a picture that failed is asked for again;
//   swap     a panel's src changes: the old picture stays up until the new one
//            has decoded, and the panel is never without a picture between;
//   burst    two renders while a swap is on its way: one request;
//   live     something writes the stand-in's src while a swap is on its way
//            (Quick Edit's live preview): that picture stays;
//   lazy     a lazy-loading tray picture still swaps.
//
//   node tests/browser/figure_images.mjs --out FILE [--headed]
import { createServer } from "node:http";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { REPO, launch } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const outPath = opt("--out", null);
const STATIC = path.join(REPO, "plexora", "plugins", "figure_builder", "static");
const DELAY_MS = 250;

/** A 4x4 RGB PNG of one colour. */
function png(r, g, b) {
    const chunk = (type, data) => {
        const length = Buffer.alloc(4);
        length.writeUInt32BE(data.length);
        const body = Buffer.concat([Buffer.from(type, "latin1"), data]);
        const crc = Buffer.alloc(4);
        crc.writeUInt32BE(zlib.crc32(body) >>> 0);
        return Buffer.concat([length, body, crc]);
    };
    const header = Buffer.alloc(13);
    header.writeUInt32BE(4, 0);
    header.writeUInt32BE(4, 4);
    header[8] = 8;
    header[9] = 2;
    const rows = Buffer.alloc(4 * 13);
    for (let y = 0; y < 4; y += 1) {
        for (let x = 0; x < 4; x += 1) rows.set([r, g, b], y * 13 + 1 + x * 3);
    }
    return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", header),
                          chunk("IDAT", zlib.deflateSync(rows)), chunk("IEND", Buffer.alloc(0))]);
}

const asked = {};
const HOST = `<!doctype html><meta charset="utf-8"><title>kept images</title>
<div id="surface"></div><script src="/static/figureCanvas.js"></script>`;
const server = createServer((request, response) => {
    const url = new URL(request.url, "http://x");
    if (url.pathname === "/") {
        response.writeHead(200, { "content-type": "text/html" });
        response.end(HOST);
        return;
    }
    if (url.pathname.startsWith("/static/")) {
        response.writeHead(200, { "content-type": "text/javascript" });
        response.end(readFileSync(path.join(STATIC, path.basename(url.pathname))));
        return;
    }
    if (url.pathname.startsWith("/img/")) {
        const key = url.pathname.slice(5) + url.search;
        asked[key] = (asked[key] || 0) + 1;
        setTimeout(() => {
            if (url.pathname === "/img/missing.png") {
                response.writeHead(404);
                response.end();
                return;
            }
            const v = Number(url.searchParams.get("v") || 1);
            response.writeHead(200, { "content-type": "image/png", "cache-control": "no-cache, must-revalidate" });
            response.end(png((v * 40) % 256, 80, 160));
        }, DELAY_MS);
        return;
    }
    response.writeHead(404);
    response.end();
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launch({ headed: args.includes("--headed") });
const out = { cases: {}, asked, failed: [] };

try {
    const page = await browser.context.newPage();
    await page.goto(`${origin}/`);
    await page.waitForFunction(() => typeof FigureCanvas === "function");
    out.cases = await page.evaluate(async (delay) => {
        const surface = document.getElementById("surface");
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
        const panel = (id, src, extra = "") => `<div class="fb-panel" data-panel-id="${id}">
            <img class="fb-panel-image" src="${src}" alt="" ${extra}
                 onerror="this.classList.add('fb-panel-image-missing')"></div>`;
        const markup = ({ b = 1, flip = false, lazy = false } = {}) =>
            panel("a", "/img/a.png?v=1", flip ? 'style="transform:scaleX(-1)"' : "")
            + panel("b", `/img/b.png?v=${b}`, lazy ? 'loading="lazy"' : "")
            + panel("m", "/img/missing.png");
        const img = (id) => surface.querySelector(`[data-panel-id="${id}"] img`);
        const settled = async () => {
            for (let i = 0; i < 100; i += 1) {
                if (Array.from(surface.querySelectorAll("img")).every((el) => el.complete)) return;
                await sleep(20);
            }
        };
        const cases = {};

        FigureCanvas.replaceKeepingImages(surface, markup());
        await settled();
        img("a").__tag = "a1";
        img("b").__tag = "b1";
        img("m").__tag = "m1";

        // kept
        FigureCanvas.replaceKeepingImages(surface, markup({ flip: true }));
        cases.kept = {
            sameA: img("a").__tag === "a1",
            sameB: img("b").__tag === "b1",
            flipApplied: img("a").getAttribute("style") === "transform:scaleX(-1)",
            brokenRetried: img("m").__tag !== "m1",
        };
        await settled();

        // swap: watch panel b on every frame until the new picture is up
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 2 }));
        const swap = { blankFrames: 0, oldShownFirst: img("b").__tag === "b1", frames: 0 };
        const started = performance.now();
        while (performance.now() - started < delay * 6) {
            const el = img("b");
            if (!el || !el.complete || !el.naturalWidth) swap.blankFrames += 1;
            swap.frames += 1;
            if (el && el.getAttribute("src").endsWith("v=2") && el.complete) break;
            await frame();
        }
        swap.swapped = img("b").getAttribute("src").endsWith("v=2");
        swap.waitedMs = Math.round(performance.now() - started);
        cases.swap = swap;

        // burst
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 3 }));
        await sleep(30);
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 3, flip: true }));
        await sleep(delay * 4);
        cases.burst = { swapped: img("b").getAttribute("src").endsWith("v=3") };

        // live
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 4 }));
        const live = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
        img("b").src = live;
        await sleep(delay * 4);
        cases.live = { kept: img("b").getAttribute("src") === live };

        // lazy
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 5 }));
        await sleep(delay * 4);
        FigureCanvas.replaceKeepingImages(surface, markup({ b: 6, lazy: true }));
        await sleep(delay * 4);
        cases.lazy = { swapped: img("b").getAttribute("src").endsWith("v=6"), complete: img("b").complete };
        return cases;
    }, DELAY_MS);
} catch (error) {
    out.failed.push(String(error && error.stack || error));
} finally {
    await browser.close();
    server.close();
}

const text = JSON.stringify(out, null, 1);
if (outPath) writeFileSync(outPath, text);
console.log(text);
