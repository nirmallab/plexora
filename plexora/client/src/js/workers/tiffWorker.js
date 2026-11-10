/**
 * The browser node's reader worker: node/tiffReader.js -- or, for an
 * OME-Zarr store, node/zarrReader.js -- behind messages, one picked file or
 * store per worker (services/browserNode.js runs several over a file;
 * workers/pyramidWorker.js runs several as a pyramid build's hands).
 *
 * {id, op: "open"|"tile"|"tiles"|"index"|"seed"|"level"|"region"|"max", ...args}
 * -> {id, ok, result|error}. A result's `array` (each of `tiles`') is
 * transferred, never copied. `tiles`, `index` and `seed` are a TIFF's only
 * (services/browserNode.js TiffPool: tiles side by side in one read, a level
 * parsed once a pool). Bundled by webpack
 * (dist/tiffWorker_bundle.js): geotiff.js comes with it.
 *
 * An `open` with `store` and `zarr` opens the OME-Zarr reader (`tile` then
 * reads it); a store the page holds (`store.page`, the website's picked
 * folder) is read by asking the page for each file:
 *   worker -> page  {ask, path}         the File at `path`, or null
 *   page -> worker  {answer, file}
 *
 * A pyramid build's two jobs (node/pyramid.js):
 *   {op: "block", channel, y0, y1, x0, x1, factors}
 *       one band of level 0, read and decoded, and every level's part of it
 *       (node/reduce.js) -> {parts: [{level, row, col, height, width, buffer}], timing}
 *   {op: "deflate", buffer}  one assembled tile -> {bytes}
 */
import { createTiffReader, throughStream } from "../node/tiffReader.js";
import { createZarrReader } from "../node/zarrReader.js";
import { bandParts } from "../node/pyramid.js";

const tiff = createTiffReader();
//: The page's answers to this worker's asks, by ask id.
const asks = new Map();
let nextAsk = 1;
function ask(path) {
    const id = nextAsk++;
    return new Promise((resolve) => {
        asks.set(id, resolve);
        self.postMessage({ ask: id, path });
    });
}
const zarr = createZarrReader({ ask });
//: Which reader `tile` reads: the one the last `open` opened.
let reader = tiff;

async function deflate({ buffer }) {
    return { bytes: new Uint8Array(await throughStream(new Uint8Array(buffer), new CompressionStream("deflate"))) };
}

const OPS = {
    open: (args) => {
        reader = args.store && args.zarr ? zarr : tiff;
        return reader.open(args);
    },
    tile: (args) => reader.tile(args),
    tiles: (args) => tiff.tiles(args),
    index: (args) => tiff.index(args),
    seed: (args) => tiff.seed(args),
    level: (args) => tiff.level(args),
    region: (args) => tiff.region(args),
    max: (args) => tiff.max(args),
    regions: (args) => tiff.regions(args),
    block: (args) => bandParts(tiff, args),
    deflate,
};

function transfers(result) {
    // Rectangles for Python: each its own buffer (once each: a list may not name one twice).
    if (result?.regions) return [...new Set(result.regions.map((one) => one.values?.buffer).filter(Boolean))];
    if (result?.parts) return result.parts.map((part) => part.buffer);
    if (result?.tiles) return [...new Set(result.tiles.map((one) => one.array?.buffer).filter(Boolean))];
    // An open's recorded reads (`record`): copies, nothing here reads them again.
    if (result?.seed) return result.seed.map((slice) => slice.buffer).filter(Boolean);
    const buffer = result?.array?.buffer || result?.values?.buffer || result?.bytes?.buffer;
    return buffer ? [buffer] : [];
}

self.onmessage = async (event) => {
    if (event.data && event.data.answer !== undefined) {
        const resolve = asks.get(event.data.answer);
        asks.delete(event.data.answer);
        if (resolve) resolve(event.data.file || null);
        return;
    }
    const { id, op, ...args } = event.data;
    try {
        if (!OPS[op]) throw new Error(`unknown op ${op}`);
        const result = await OPS[op](args);
        self.postMessage({ id, ok: true, result }, transfers(result));
    } catch (error) {
        self.postMessage({ id, ok: false, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
