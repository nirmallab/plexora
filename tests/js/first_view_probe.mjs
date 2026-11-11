// services/firstView.js's reader of a picked TIFF (`_plane`), run in Node on
// files on disk: the smallest pyramid level's first plane, as the page reads
// it for the first view. Prints one JSON line per file: its size, layout, the
// reads it made, and a sha256 of its samples (little-endian), for
// tests/test_first_view.py to hold against tifffile's own reading.
//
//   node tests/js/first_view_probe.mjs FILE [FILE ...]   (from the repository root)
import { createHash } from "node:crypto";
import { closeSync, fstatSync, openSync, readFileSync, readSync } from "node:fs";
import vm from "node:vm";

class DiskFile {
    constructor(path) {
        this.fd = openSync(path, "r");
        this.size = fstatSync(this.fd).size;
        this.name = path.split(/[\\/]/).pop();
        this.lastModified = 0;
    }
    slice(from, to) {
        return {
            arrayBuffer: async () => {
                const n = Math.max(0, Math.min(to, this.size) - from);
                const buffer = Buffer.alloc(n);
                if (n) readSync(this.fd, buffer, 0, n, from);
                return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + n);
            },
        };
    }
}

const context = {
    console, performance, TextDecoder, DataView, Uint8Array, Uint16Array, Uint32Array, Int32Array, Map, Set,
    WeakMap, Promise, Math, Number, String, Array, Object, Error, BigInt, Blob, Response, DecompressionStream,
    setInterval, clearInterval, setTimeout, requestAnimationFrame: (fn) => setTimeout(fn, 0),
    document: { createElement: () => ({}), getElementById: () => null, head: { appendChild() {} }, body: null },
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(readFileSync("plexora/client/src/js/services/firstView.js", "utf8"), context);

for (const path of process.argv.slice(2)) {
    const file = new DiskFile(path);
    try {
        const plane = await context.PlexoraFirstView._plane(file);
        const bytes = plane.data instanceof context.Uint16Array || plane.data.BYTES_PER_ELEMENT === 2
            ? Buffer.from(new Uint8Array(new Uint16Array(plane.data).buffer))
            : Buffer.from(plane.data);
        console.log(JSON.stringify({ path, width: plane.width, height: plane.height, samples: plane.samples,
                                     bits: plane.data.BYTES_PER_ELEMENT * 8, layout: plane.layout,
                                     reads: plane.reads, offsets: plane.offsets.length,
                                     channels: plane.channels,
                                     sha256: createHash("sha256").update(bytes).digest("hex") }));
    } catch (error) {
        console.log(JSON.stringify({ path, error: String(error && error.message || error) }));
    } finally {
        closeSync(file.fd);
    }
}
