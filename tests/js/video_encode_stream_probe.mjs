/**
 * Video Studio's browser encoder: a streamed export must be the same file a
 * buffered one is.
 *
 *     node tests/js/video_encode_stream_probe.mjs
 *
 * Runs the real videoEncodeWorker.js on the real vendored mediabunny muxer,
 * with WebCodecs replaced by a fake encoder that emits fixed-size packets (so
 * no browser is needed and the bytes are deterministic). Each export is made
 * twice: once with `streaming: false` (BufferTarget, which honours every
 * seek) and once with `streaming: true` (the chunked StreamTarget). The WebM
 * muxer seeks back at the end to patch the Segment size, the Duration and the
 * SeekHead; once the output has grown past two 16 MiB chunks those patches
 * arrive as writes at earlier positions, and a sink that ignores `position`
 * appends them to the end of the file instead.
 *
 * What it holds:
 *   - a large streamed WebM (well over two chunks) is byte-identical to the
 *     buffered one;
 *   - a small streamed WebM (one chunk) is too;
 *   - a large streamed MP4 (fragmented, written front to back) still comes out
 *     whole: it starts with an ftyp box and its top-level boxes tile the file.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

// -- a fake WebCodecs ------------------------------------------------------------------

let packetBytes = 1024;

class FakeVideoFrame {
    constructor(source, init = {}) {
        const from = source instanceof FakeVideoFrame ? source : null;
        this.format = "RGBA";
        this.codedWidth = from ? from.codedWidth : init.codedWidth ?? 64;
        this.codedHeight = from ? from.codedHeight : init.codedHeight ?? 64;
        this.displayWidth = this.codedWidth;
        this.displayHeight = this.codedHeight;
        this.visibleRect = { x: 0, y: 0, width: this.codedWidth, height: this.codedHeight };
        this.timestamp = init.timestamp ?? (from ? from.timestamp : 0);
        this.duration = init.duration ?? (from ? from.duration : null);
        this.colorSpace = { primaries: "bt709", transfer: "bt709", matrix: "bt709", fullRange: false,
                            toJSON() { return {}; } };
    }
    clone() { return new FakeVideoFrame(this); }
    close() {}
    allocationSize() { return this.codedWidth * this.codedHeight * 4; }
    async copyTo() { return []; }
}

class FakeEncodedVideoChunk {
    constructor({ type, timestamp, duration, data }) {
        this.type = type;
        this.timestamp = timestamp;
        this.duration = duration;
        this._data = data;
        this.byteLength = data.byteLength;
    }
    copyTo(dest) { new Uint8Array(dest.buffer || dest, dest.byteOffset || 0).set(this._data); }
}

class FakeVideoEncoder {
    static async isConfigSupported(config) { return { supported: true, config }; }
    constructor({ output, error }) {
        this.output = output;
        this.error = error;
        this.state = "unconfigured";
        this.encodeQueueSize = 0;
        this.count = 0;
    }
    addEventListener() {}
    removeEventListener() {}
    configure(config) { this.config = config; this.state = "configured"; }
    encode(frame, options = {}) {
        const data = new Uint8Array(packetBytes);
        data.fill(this.count % 251);
        data[0] = 0x82;      // a VP9 frame marker; the muxer does not parse the payload
        const chunk = new FakeEncodedVideoChunk({
            type: options.keyFrame || this.count === 0 ? "key" : "delta",
            timestamp: frame.timestamp, duration: frame.duration ?? 33333, data,
        });
        const meta = this.count === 0
            ? { decoderConfig: { codec: this.config.codec, codedWidth: this.config.width,
                                 codedHeight: this.config.height,
                                 // an AVCDecoderConfigurationRecord with no parameter sets,
                                 // so the MP4 muxer need not find them in the payload
                                 ...(this.config.codec.startsWith("avc")
                                     ? { description: new Uint8Array([1, 0x42, 0, 0x1e, 0xff, 0xe0, 0]) }
                                     : {}) } }
            : {};
        this.count += 1;
        this.output(chunk, meta);
    }
    async flush() {}
    close() { this.state = "closed"; }
    reset() {}
}

globalThis.VideoFrame = FakeVideoFrame;
globalThis.EncodedVideoChunk = FakeEncodedVideoChunk;
globalThis.VideoEncoder = FakeVideoEncoder;

// -- the worker, loaded as a module in this realm -------------------------------------

const vendorUrl = pathToFileURL(join(STATIC, "vendor/mediabunny-1.60.0.mjs")).href;
const workerSource = readFileSync(join(STATIC, "videoEncodeWorker.js"), "utf-8")
    .replace("./vendor/mediabunny-1.60.0.mjs", vendorUrl);

const inbox = [];
let waiter = null;
globalThis.self = {
    postMessage(message) {
        inbox.push(message);
        if (waiter) { const w = waiter; waiter = null; w(); }
    },
};
await import(`data:text/javascript;base64,${Buffer.from(workerSource).toString("base64")}`);
const worker = globalThis.self;

async function next(type) {
    for (;;) {
        const i = inbox.findIndex((m) => m.type === type || m.type === "error");
        if (i >= 0) {
            const [m] = inbox.splice(i, 1);
            if (m.type === "error") throw new Error(m.message);
            return m;
        }
        await new Promise((resolve) => { waiter = resolve; });
    }
}

async function exportBytes({ format, streaming, frames, bytesPerFrame }) {
    packetBytes = bytesPerFrame;
    inbox.length = 0;
    const settings = { format, streaming, width: 64, height: 64, fps: 30, bitrate: 1e6,
                       keyFrameInterval: 2, frames };
    worker.onmessage({ data: { type: "start", settings } });
    await next("started");
    for (let index = 0; index < frames; index++) {
        const frame = new FakeVideoFrame(null, { codedWidth: 64, codedHeight: 64,
                                                 timestamp: Math.round(index * 1e6 / 30) });
        worker.onmessage({ data: { type: "frame", frame, index, keyFrame: index === 0,
                                   timestampUs: Math.round(index * 1e6 / 30) } });
        await next("ack");
    }
    worker.onmessage({ data: { type: "finish" } });
    const done = await next("done");
    return new Uint8Array(await done.blob.arrayBuffer());
}

function firstDifference(a, b) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i++) if (a[i] !== b[i]) return i;
    return a.length === b.length ? -1 : n;
}

// -- WebM ---------------------------------------------------------------------------------

for (const [label, frames, bytesPerFrame] of [["large", 300, 160 * 1024], ["small", 30, 4096]]) {
    try {
        const buffered = await exportBytes({ format: "webm", streaming: false, frames, bytesPerFrame });
        const streamed = await exportBytes({ format: "webm", streaming: true, frames, bytesPerFrame });
        const at = firstDifference(buffered, streamed);
        check(`a ${label} streamed WebM is byte-identical to the buffered one`, at === -1,
              { bufferedBytes: buffered.length, streamedBytes: streamed.length, firstDifferenceAt: at });
    } catch (error) {
        check(`a ${label} streamed WebM is byte-identical to the buffered one`, false, String(error?.stack || error));
    }
}

// -- MP4 (fragmented) ------------------------------------------------------------------

try {
    const mp4 = await exportBytes({ format: "mp4", streaming: true, frames: 300, bytesPerFrame: 160 * 1024 });
    const view = new DataView(mp4.buffer, mp4.byteOffset, mp4.byteLength);
    const types = [];
    let pos = 0;
    let tiled = true;
    while (pos < mp4.length) {
        if (pos + 8 > mp4.length) { tiled = false; break; }
        const size = view.getUint32(pos);
        types.push(String.fromCharCode(...mp4.subarray(pos + 4, pos + 8)));
        if (size < 8) { tiled = false; break; }
        pos += size;
    }
    check("a large streamed MP4 starts with ftyp and its boxes tile the file",
          types[0] === "ftyp" && tiled && pos === mp4.length,
          { first: types.slice(0, 4), tiled, end: pos, bytes: mp4.length });
} catch (error) {
    check("a large streamed MP4 starts with ftyp and its boxes tile the file", false, String(error?.stack || error));
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exit(failures.length ? 1 : 0);
