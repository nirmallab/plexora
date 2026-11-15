/**
 * Video Studio's server export, when the page goes away mid-export.
 *
 *     node tests/js/video_server_encoder_leave_probe.mjs
 *
 * Frames are pushed from the page; navigate or close the tab and none come
 * again, yet the server's job stayed `running` (ffmpeg open) until its 15
 * minute idle timeout -- and the agent's export_video refused that video
 * meanwhile. The encoder asks the server to cancel on `pagehide`, with a
 * keepalive request (the only kind a leaving page may still send), and stops
 * asking once the finish has been posted: from there the server completes the
 * file on its own, and leaving must not throw it away.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");

const listeners = new Map();
const fetches = [];
const window = {
    addEventListener: (name, fn) => {
        if (!listeners.has(name)) listeners.set(name, new Set());
        listeners.get(name).add(fn);
    },
    removeEventListener: (name, fn) => listeners.get(name)?.delete(fn),
    location: { protocol: "http:", hostname: "localhost" },
};
const fire = (name) => { for (const fn of [...(listeners.get(name) || [])]) fn({}); };
const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Promise, Error,
    Uint8Array, ArrayBuffer, DataView, Blob, DOMException, encodeURIComponent,
    setTimeout: (fn) => { fn(); return 1; }, clearTimeout: () => {},
    fetch: async (url, init = {}) => {
        fetches.push({ url, init });
        const status = String(url).endsWith("/exports") ? { export_id: "exp_aaaaaaaaaaaa", chunk_frames: 2 }
            : { status: "done", name: "clip.mp4" };
        return { ok: true, status: 200, json: async () => ({ success: true, ...status }),
                 headers: { get: () => null } };
    },
    window, document: { currentScript: null, querySelectorAll: () => [] },
});
ctx.window = window;
for (const name of ["videoProjectApi.js", "videoEncoders.js"]) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}
const api = window.PlexoraVideoProjectApi;
const { ServerEncoder } = window.PlexoraVideoEncoders;

const failures = [];
function check(what, condition, detail = "") {
    if (!condition) failures.push(`${what}${detail ? ` -- ${detail}` : ""}`);
    console.log(`${condition ? "PASS" : "FAIL"} ${what}${detail ? `  ${detail}` : ""}`);
}
const cancels = () => fetches.filter((f) => f.init.method === "DELETE");
const settings = { width: 4, height: 4, fps: 10, format: "mp4", quality: "standard", overlays: {} };

{
    const encoder = new ServerEncoder(settings, { videoId: "vid_aaaaaaaaaaaa", api, composer: null });
    await encoder.start();
    fire("pagehide");
    const sent = cancels();
    check("leaving mid-export asks the server to cancel it",
        sent.length === 1 && sent[0].url.includes("/exports/exp_aaaaaaaaaaaa"),
        JSON.stringify(sent.map((f) => f.url)));
    check("the cancel is a keepalive request", sent[0]?.init.keepalive === true);
}

{
    fetches.length = 0;
    const encoder = new ServerEncoder(settings, { videoId: "vid_aaaaaaaaaaaa", api, composer: null });
    await encoder.start();
    await encoder.finish({});
    fire("pagehide");
    check("leaving after the finish was posted cancels nothing", cancels().length === 0);
}

{
    fetches.length = 0;
    const encoder = new ServerEncoder(settings, { videoId: "vid_aaaaaaaaaaaa", api, composer: null });
    await encoder.start();
    await encoder.cancel();
    fire("pagehide");
    check("a cancelled export is not cancelled twice on leaving", cancels().length === 1);
}

if (failures.length) {
    console.log(`\n${failures.length} failure(s)`);
    process.exit(1);
}
console.log("\nall checks passed");
