/**
 * The browser node's TIFF worker: lib/tiffReader.js behind messages.
 *
 * {id, op: "open"|"tile"|"level", ...args} -> {id, ok, result|error}. A result's
 * `array` is transferred, never copied.
 */
import { createTiffReader } from "../lib/tiffReader.js";

const reader = createTiffReader();

self.onmessage = async (event) => {
    const { id, op, ...args } = event.data;
    try {
        if (!["open", "tile", "level"].includes(op)) throw new Error(`unknown op ${op}`);
        const result = await reader[op](args);
        const transfer = result?.array?.buffer ? [result.array.buffer] : [];
        self.postMessage({ id, ok: true, result }, transfer);
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error?.stack || error) });
    }
};
