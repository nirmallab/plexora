/**
 * The page's pyramid builder (client src/js/node/pyramid.js, tiffWriter.js,
 * reduce.js) run in Node with one in-process hand: tests/test_tiff_writer.py
 * hands it a flat TIFF and what Python would ask the page for, and compares
 * the file it writes with image_pyramid.pyramidize_image's.
 *
 * Run:  node tests/js/pyramid_probe.mjs SPEC.json
 *   SPEC: {source, out, dtype, channels, factors, tile, description, jobBytes?}
 * Prints {bytes, tiles, geometry, timings} as JSON.
 */

import { closeSync, fstatSync, openSync, readFileSync, readSync, writeSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const NODE = path.join(REPO, "plexora", "client", "src", "js", "node");
const { createTiffReader, throughStream } = await import(pathToFileURL(path.join(NODE, "tiffReader.js")).href);
const { bandParts, build, dtypeOf } = await import(pathToFileURL(path.join(NODE, "pyramid.js")).href);

/** geotiff.js's source over a file descriptor (Node's Blob truncates past 4 GiB). */
class FileSource {
    constructor(file) {
        this.fd = openSync(file, "r");
        this.size = fstatSync(this.fd).size;
        this.bytes = 0;
        this.reads = 0;
        this.readMs = 0;
    }

    get fileSize() {
        return this.size;
    }

    async fetch(slices) {
        return slices.map(({ offset, length }) => {
            const out = new Uint8Array(length);
            readSync(this.fd, out, 0, length, offset);
            this.reads += 1;
            this.bytes += length;
            return out.buffer;
        });
    }
}

const spec = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const reader = createTiffReader();
const { geometry } = await reader.open({ source: new FileSource(spec.source), decoder: "native" });
const found = dtypeOf(geometry);
if (found !== spec.dtype) throw new Error(`the source holds ${found}, not ${spec.dtype}`);

const out = openSync(spec.out, "w");
try {
    const result = await build({
        geometry, dtype: spec.dtype, channels: spec.channels, factors: spec.factors, tile: spec.tile,
        description: spec.description, jobBytes: spec.jobBytes || 16 << 20,
        hands: {
            count: 1,
            block: (i, job) => bandParts(reader, job),
            deflate: async (bytes) => new Uint8Array(await throughStream(bytes, new CompressionStream("deflate"))),
        },
        sink: { write: (bytes, at) => { writeSync(out, bytes, 0, bytes.byteLength, at); } },
    });
    console.log(JSON.stringify({ ...result, geometry: { width: geometry.width, height: geometry.height,
                                                        channels: geometry.numChannels, layout: geometry.layout,
                                                        tiled: geometry.fileTile.tiled } }));
} finally {
    closeSync(out);
}
