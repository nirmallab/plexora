/**
 * The browser node's whole-slide reader: a slide the user picked (MRXS with
 * its folder, NDPI, SVS, Sakura) read in this page by openslide-js -- OpenSlide
 * 4.0.0 compiled to WebAssembly (client/external/openslide-js-1.0.0-beta.5,
 * LGPL-2.1, loaded at run time as its own files; plan L2). Its pixels are
 * native OpenSlide's byte for byte (docs/internal/bench/browser_node/L2.md).
 *
 * openslide-js runs a pool of workers (one WebAssembly instance each, built
 * with threads, so the page must be cross-origin isolated, as the site is)
 * and one I/O broker that reads each byte of a picked file once for all of
 * them. One opened slide reads on one worker; a slide is opened here on each
 * worker of the pool, and its reads go round them.
 *
 * Native ES module, imported by services/browserSlides.js where a slide is
 * first wanted: the 9 MB of WebAssembly is fetched then, never at boot.
 *
 *   createSlideReader({workers}) -> {open(key, source), info(key), region(key, r), close(key), closeAll()}
 *     open    source: a File, or [{path, file}] relative to the folder holding
 *             the index file (an MRXS's .mrxs first) -> info
 *     info    {levels, dims: [[w, h]], downsamples, properties, associated}
 *     region  {level, x, y, w, h} (x, y in level 0) -> {width, height, data}:
 *             straight RGBA, as openslide-python's read_region gives it
 *   overWhite(rgba, n) -> RGB, composited as brightfield._OpenSlideSource does
 *   roundHalfEven(v)   -> Python's round()
 *   tileWindow(level, tx, ty, tw, th) -> the native read behind a tile, or null
 */
import { OpenSlide } from "/client/external/openslide-js-1.0.0-beta.5/index.js";

export { overWhite, roundHalfEven, tileWindow } from "./slideTiles.js";

export function createSlideReader({ workers = 4 } = {}) {
    let library = null;
    //: key -> a promise of {handles: [Slide], next, info}.
    const slides = new Map();

    function ready() {
        if (!library) library = OpenSlide.initialize({ workerCount: workers });
        return library;
    }

    async function opened(source) {
        const lib = await ready();
        const first = await lib.open(source);
        // The same slide on the other workers (the broker reads its bytes
        // once); one that will not open just leaves its worker out.
        const more = await Promise.all(Array.from({ length: workers - 1 },
                                                  () => lib.open(source).catch(() => null)));
        const info = {
            levels: first.levelCount,
            dims: first.levelDimensions.map((d) => [d.width, d.height]),
            downsamples: Array.from(first.levelDownsamples),
            properties: Object.fromEntries(first.properties),
            associated: Array.from(first.associatedImageNames),
        };
        return { handles: [first, ...more.filter(Boolean)], next: 0, info };
    }

    async function open(key, source) {
        if (!slides.has(key)) {
            const pending = opened(source);
            slides.set(key, pending);
            pending.catch(() => slides.delete(key));
        }
        return (await slides.get(key)).info;
    }

    async function held(key) {
        const entry = slides.get(key);
        if (!entry) throw new Error("this slide is not open in the page");
        return entry;
    }

    async function info(key) {
        return (await held(key)).info;
    }

    async function region(key, { level, x, y, w, h }) {
        const entry = await held(key);
        const handle = entry.handles[entry.next % entry.handles.length];
        entry.next += 1;
        const image = await handle.readRegion(x, y, level, w, h);
        return { width: image.width, height: image.height,
                 data: new Uint8Array(image.data.buffer, image.data.byteOffset, image.data.byteLength) };
    }

    async function close(key) {
        const entry = slides.get(key);
        slides.delete(key);
        if (!entry) return;
        const { handles } = await entry.catch(() => ({ handles: [] }));
        await Promise.all(handles.map((handle) => handle.close().catch(() => null)));
    }

    async function closeAll() {
        await Promise.all([...slides.keys()].map(close));
    }

    return { open, info, region, close, closeAll, has: (key) => slides.has(key) };
}
