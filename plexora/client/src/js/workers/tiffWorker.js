/**
 * The browser node's TIFF worker: node/tiffReader.js behind messages, one
 * picked file per worker (services/browserNode.js runs several over a file;
 * workers/pyramidWorker.js runs several as a pyramid build's hands).
 *
 * {id, op: "open"|"tile"|"level"|"region"|"max", ...args} -> {id, ok, result|error}.
 * A result's `array` is transferred, never copied. Bundled by webpack
 * (dist/tiffWorker_bundle.js): geotiff.js comes with it.
 *
 * A pyramid build's two jobs (node/pyramid.js):
 *   {op: "block", channel, y0, y1, x0, x1, factors}
 *       one band of level 0, read and decoded, and every level's part of it
 *       (node/reduce.js) -> {parts: [{level, row, col, height, width, buffer}], timing}
 *   {op: "deflate", buffer}  one assembled tile -> {bytes}
 */
import { createTiffReader, throughStream } from "../node/tiffReader.js";
import { bandParts } from "../node/pyramid.js";

const reader = createTiffReader();

async function deflate({ buffer }) {
    return { bytes: new Uint8Array(await throughStream(new Uint8Array(buffer), new CompressionStream("deflate"))) };
}

const OPS = {
    open: (args) => reader.open(args),
    tile: (args) => reader.tile(args),
    level: (args) => reader.level(args),
    region: (args) => reader.region(args),
    max: (args) => reader.max(args),
    block: (args) => bandParts(reader, args),
    deflate,
};

function transfers(result) {
    if (result?.parts) return result.parts.map((part) => part.buffer);
    // An open's recorded slices (`record`): copies, nothing here reads them again.
    if (result?.seed) return result.seed.map((slice) => slice.buffer);
    const buffer = result?.array?.buffer || result?.values?.buffer || result?.bytes?.buffer;
    return buffer ? [buffer] : [];
}

self.onmessage = async (event) => {
    const { id, op, ...args } = event.data;
    try {
        if (!OPS[op]) throw new Error(`unknown op ${op}`);
        const result = await OPS[op](args);
        self.postMessage({ id, ok: true, result }, transfers(result));
    } catch (error) {
        self.postMessage({ id, ok: false, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
