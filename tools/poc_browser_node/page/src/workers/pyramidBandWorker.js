/**
 * One of the pyramid builder's hands (workers/pyramidWorker.js holds the
 * rest): it opens the flat file itself, and answers two kinds of job.
 *
 *  {op: "open", file, kind, factors, tile, copyLevel0, compress}
 *      -> {opened}
 *  {op: "block", id, channel, y0, y1, x0, x1}
 *      one band of level 0: read and decoded, every derived level's part of
 *      it made (lib/reduce.js), and level 0 itself when it is being copied
 *      -> {id, parts: [{level, row, col, height, width, buffer}], timing}
 *  {op: "deflate", id, buffer}
 *      one assembled tile of a derived level -> {id, bytes, ms}
 *
 * Bands are independent (reduce.js says why), so any number of these run at
 * once, each on its own core.
 */
import { createTiffReader, throughStream } from "../lib/tiffReader.js";
import { derive } from "../lib/reduce.js";

const now = () => performance.now();
let reader = null;
let config = null;

async function deflate(bytes) {
    return new Uint8Array(await throughStream(bytes, new CompressionStream("deflate")));
}

async function block({ id, channel, y0, y1, x0, x1 }) {
    const { factors, kind, copyLevel0 } = config;
    const t0 = now();
    const { values, timing: read } = await reader.region({ channel, level: 0, x0, y0, x1, y1 });
    const tRead = now();
    const bh = y1 - y0;
    const bw = x1 - x0;
    const derived = derive(values, bh, bw, factors, kind);
    const tDerive = now();
    const parts = derived.map((d) => ({
        level: d.level, row: y0 / factors[d.level], col: x0 / factors[d.level],
        height: d.height, width: d.width, buffer: d.array.buffer,
    }));
    if (copyLevel0) {
        // Level 0 itself, for a source that cannot serve tiles: pasted into
        // tiles and deflated like every other level.
        const own = values.byteOffset === 0 && values.buffer.byteLength === values.byteLength ? values : values.slice();
        parts.unshift({ level: 0, row: y0, col: x0, height: bh, width: bw, buffer: own.buffer });
    }
    self.postMessage({
        id, channel, parts,
        timing: { readMs: tRead - t0, decodeMs: read.decodeMs, fileReadMs: read.readMs, bytesRead: read.bytes,
                  deriveMs: tDerive - tRead, totalMs: now() - t0 },
    }, parts.map((p) => p.buffer));
}

self.onmessage = async (event) => {
    const msg = event.data;
    try {
        if (msg.op === "open") {
            config = msg;
            reader = createTiffReader();
            const { geometry } = await reader.open({ file: msg.file, decoder: "native" });
            config.height = geometry.height;
            self.postMessage({ opened: true });
        } else if (msg.op === "block") {
            await block(msg);
        } else if (msg.op === "deflate") {
            const s = now();
            const bytes = await deflate(new Uint8Array(msg.buffer));
            self.postMessage({ id: msg.id, bytes, ms: now() - s }, [bytes.buffer]);
        }
    } catch (error) {
        self.postMessage({ id: msg.id, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
