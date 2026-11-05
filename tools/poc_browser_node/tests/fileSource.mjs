// A geotiff.js source over a Node file handle, with the reader's counters.
// fs.openAsBlob reports sizes modulo 2^32 on this Node (22.17, Windows): a
// 66.7 GB TIFF came back as a 2.3 GB Blob, so tests read through a handle.
import { open, stat } from "node:fs/promises";

export async function fileSource(path) {
    const handle = await open(path, "r");
    const size = (await stat(path)).size;
    return {
        bytes: 0, reads: 0, readMs: 0,
        get fileSize() { return size; },
        async fetch(slices) {
            const t0 = performance.now();
            const out = await Promise.all(slices.map(async ({ offset, length }) => {
                const n = Math.max(0, Math.min(length, size - offset));
                const buffer = new Uint8Array(n);
                if (n) await handle.read(buffer, 0, n, offset);
                return buffer.buffer;
            }));
            this.readMs += performance.now() - t0;
            this.reads += slices.length;
            for (const s of slices) this.bytes += s.length;
            return out;
        },
        close: () => handle.close(),
    };
}
