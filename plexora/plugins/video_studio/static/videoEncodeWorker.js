/**
 * videoEncodeWorker.js - H.264 / VP9 encoding in the browser, off the main thread.
 *
 * A module worker (`new Worker(url, {type: "module"})`). The main thread
 * renders a frame, wraps the output canvas in a VideoFrame and transfers it
 * here; this hands it to mediabunny's VideoSampleSource, which drives the
 * browser's WebCodecs VideoEncoder and muxes the packets into MP4 or WebM.
 * Every frame is acknowledged once the encoder has taken it, and the main
 * thread keeps at most a few frames in flight, so a slow encoder slows the
 * render instead of piling decoded frames up in memory.
 *
 * Messages in:  {type: "start", settings} | {type: "frame", frame, timestampUs, index, keyFrame}
 *               | {type: "finish"} | {type: "cancel"}
 * Messages out: {type: "started", codec} | {type: "ack", index} | {type: "done", blob, bytes}
 *               | {type: "error", message}
 *
 * The muxer is vendor/mediabunny-1.60.0.mjs (MPL-2.0, unmodified; vendor/README.md). The
 * version is in the file name because a module import cannot carry the plugin's `?v=`.
 */
import {
    Output, Mp4OutputFormat, WebMOutputFormat, BufferTarget, StreamTarget, VideoSampleSource,
    VideoSample, canEncodeVideo,
} from "./vendor/mediabunny-1.60.0.mjs";

let output = null;
let source = null;
let chunks = null;
let started = false;
let fps = 30;

function fail(error) {
    self.postMessage({ type: "error", message: String(error?.message || error) });
}

async function start(settings) {
    fps = settings.fps;
    const codec = settings.format === "webm" ? "vp9" : "avc";
    const config = { width: settings.width, height: settings.height, bitrate: settings.bitrate };
    if (!(await canEncodeVideo(codec, config))) {
        throw new Error(`this browser cannot encode ${codec === "avc" ? "H.264" : "VP9"} at `
                        + `${settings.width}x${settings.height}`);
    }
    let target;
    if (settings.streaming) {
        // Fragmented: written front to back, so the parts arrive in order and
        // the file never has to be held whole while it is being built.
        chunks = [];
        target = new StreamTarget(new WritableStream({
            write(chunk) {
                chunks.push(chunk.data.slice());
            },
        }), { chunked: true });
    } else {
        target = new BufferTarget();
    }
    const format = settings.format === "webm"
        ? new WebMOutputFormat()
        : new Mp4OutputFormat({ fastStart: settings.streaming ? "fragmented" : "in-memory" });
    output = new Output({ format, target });
    source = new VideoSampleSource({
        codec,
        bitrate: settings.bitrate,
        keyFrameInterval: settings.keyFrameInterval || 2,
        latencyMode: "quality",
        bitrateMode: "variable",
        sizeChangeBehavior: "deny",
        ...(settings.alpha ? { alpha: "keep" } : {}),
    });
    output.addVideoTrack(source, { frameRate: fps });
    await output.start();
    started = true;
    self.postMessage({ type: "started", codec });
}

async function frame(message) {
    const sample = new VideoSample(message.frame, {
        timestamp: message.timestampUs / 1e6,
        duration: 1 / fps,
    });
    try {
        await source.add(sample, message.keyFrame ? { keyFrame: true } : undefined);
    } finally {
        sample.close();
        message.frame.close?.();
    }
    self.postMessage({ type: "ack", index: message.index });
}

async function finish() {
    source.close();
    await output.finalize();
    let blob;
    const type = output.format instanceof WebMOutputFormat ? "video/webm" : "video/mp4";
    if (chunks) {
        blob = new Blob(chunks, { type });
    } else {
        blob = new Blob([output.target.buffer], { type });
    }
    self.postMessage({ type: "done", blob, bytes: blob.size });
    output = null;
    source = null;
    chunks = null;
}

let queue = Promise.resolve();

self.onmessage = (event) => {
    const message = event.data || {};
    if (message.type === "cancel") {
        queue = queue.then(async () => {
            try {
                if (output && started) await output.cancel();
            } catch (error) {
                // cancelling a broken output is still cancelling
            }
            output = null;
            source = null;
            chunks = null;
            self.postMessage({ type: "cancelled" });
        });
        return;
    }
    queue = queue.then(async () => {
        try {
            if (message.type === "start") await start(message.settings);
            else if (message.type === "frame") await frame(message);
            else if (message.type === "finish") await finish();
        } catch (error) {
            message.frame?.close?.();
            fail(error);
        }
    });
};
