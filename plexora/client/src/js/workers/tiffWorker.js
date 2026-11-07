/**
 * The browser node's TIFF worker: node/tiffReader.js behind messages, one
 * picked file per worker (services/browserNode.js runs several over a file).
 *
 * {id, op: "open"|"tile"|"level"|"region", ...args} -> {id, ok, result|error}.
 * A result's `array` is transferred, never copied. Bundled by webpack
 * (dist/tiffWorker_bundle.js): geotiff.js comes with it.
 */
import { createTiffReader } from "../node/tiffReader.js";

const reader = createTiffReader();

self.onmessage = async (event) => {
    const { id, op, ...args } = event.data;
    try {
        if (!["open", "tile", "level", "region"].includes(op)) throw new Error(`unknown op ${op}`);
        const result = await reader[op](args);
        const buffer = result?.array?.buffer || result?.values?.buffer;
        self.postMessage({ id, ok: true, result }, buffer ? [buffer] : []);
    } catch (error) {
        self.postMessage({ id, ok: false, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
