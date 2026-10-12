/**
 * Video Studio's readiness barrier (videoReadiness.js) against stand-ins for
 * everything it asks.
 *
 *     node tests/js/video_studio_readiness_probe.mjs
 *
 * What it holds:
 *   - every signal blocks when it should and only then: the view revision,
 *     a stale tile token, an item not fully loaded, the image loader, the tile
 *     scheduler, a plane fade, a pending swap, the overlay, a coarse stand-in
 *     tile, an auto-levelling channel, the network, a plugin's wait; in 3-D the
 *     renderer and a provisional window (and the 2-D signals do not);
 *   - standby copies and invisible items never block;
 *   - `wait` resolves only after two clean polls in a row, rejects with a
 *     ReadinessTimeout naming what never cleared, and stops on abort;
 *   - the network tracker counts only the page's data requests.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

const listeners = {};
const context = createContext({
    console, Math, JSON, Promise, setTimeout, clearTimeout, Date, Array, Object, Number, String, Set, Error,
    DOMException: globalThis.DOMException,
    performance: globalThis.performance,
    document: { hidden: false },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    dispatchEvent(event) { for (const fn of listeners[event.type] || []) fn(event); return true; },
});
context.window = context;
context.globalThis = context;
for (const file of ["videoReadiness.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}
context.PlexoraVideoSceneState = { slots: (env) => env._slots || [] };
const R = context.PlexoraVideoReadiness;

function item({ loaded = true, opacity = 1, standby = false, coarse = false } = {}) {
    return { getFullyLoaded: () => loaded, getOpacity: () => opacity, source: { plexoraStandby: standby },
             _lastDrawn: coarse ? [{ tile: { _plexoraCoarse: true } }] : [{ tile: {} }] };
}

function env(over = {}) {
    const items = over.items || [item()];
    return {
        vs: { _applying: false, _frame: 0, lateCommits: () => 0, appliedSpec: () => "z3", spec: () => "z3", get: () => ({ layout: "single" }),
              ...(over.vs || {}) },
        sidebar: { channelSlots: over.slots || [{ name: "A", enabled: true, autoLeveling: false }] },
        iv: {
            viewer: { world: { getItemCount: () => items.length, getItemAt: (i) => items[i], needsDraw: () => Boolean(over.needsDraw) },
                      imageLoader: { jobsInProgress: over.jobs || 0, jobQueue: [] } },
            tileFetch: { idle: () => !over.fetching },
            viewerManagerVMain: { viewSwapPending: Boolean(over.swap) },
            overlaysSettled: () => !over.overlayBusy,
        },
        planeMotion: { fade: { idle: () => !over.fading } },
        renderer: { settled: () => !over.volumeBusy },
        volumeWindows: { peek: () => ({ exact: !over.provisional }) },
        network: { inFlight: over.network || 0 },
        _slots: [{ slot: { enabled: true }, fullName: "A" }],
        panes: null,
    };
}

check("all clear is ready", R.blockers(env()).length === 0, R.blockers(env()));
const cases = [
    ["view", { vs: { _applying: true } }],
    ["view-spec", { vs: { appliedSpec: () => "z2" } }],
    ["main-tiles", { items: [item({ loaded: false })] }],
    ["main-loader", { jobs: 2 }],
    ["main-draw", { needsDraw: true }],
    ["main-coarse", { items: [item({ coarse: true })] }],
    ["fetch", { fetching: true }],
    ["fade", { fading: true }],
    ["swap", { swap: true }],
    ["overlay", { overlayBusy: true }],
    ["levels", { slots: [{ name: "A", enabled: true, autoLeveling: true }] }],
    ["network", { network: 1 }],
];
for (const [signal, over] of cases) {
    const blocked = R.blockers(env(over));
    check(`${signal} blocks`, blocked.includes(signal), blocked);
}
check("a standby copy never blocks", R.blockers(env({ items: [item(), item({ loaded: false, standby: true })] })).length === 0);
check("an invisible item never blocks", R.blockers(env({ items: [item(), item({ loaded: false, opacity: 0 })] })).length === 0);
check("in 3-D the renderer blocks", R.blockers(env({ volumeBusy: true }), { volume: true }).includes("volume"));
check("in 3-D a provisional window blocks", R.blockers(env({ provisional: true }), { volume: true }).includes("window-provisional"));
check("in 3-D the 2-D tiles do not block", !R.blockers(env({ items: [item({ loaded: false })] }), { volume: true })
      .includes("main-tiles"));
check("in 3-D a stale tile token does not block", !R.blockers(env({ vs: { appliedSpec: () => "x" } }), { volume: true })
      .includes("view-spec"));

// wait(): two clean polls, timeout, abort, plugin waits.
const live = env({ jobs: 1 });
setTimeout(() => { live.iv.viewer.imageLoader.jobsInProgress = 0; }, 60);
const started = Date.now();
const result = await R.wait(live, { timeoutMs: 2000 });
check("wait resolves once clear", result.polls >= 3 && Date.now() - started >= 60, result);

let timeout = null;
try {
    await R.wait(env({ fading: true }), { timeoutMs: 80 });
} catch (error) {
    timeout = error;
}
check("a stuck signal times out naming it", timeout && timeout.name === "ReadinessTimeout" && timeout.blocked.includes("fade"),
      timeout && { name: timeout.name, blocked: timeout.blocked });

const controller = new AbortController();
setTimeout(() => controller.abort(), 40);
let aborted = null;
try {
    await R.wait(env({ fading: true }), { timeoutMs: 5000, signal: controller.signal });
} catch (error) {
    aborted = error;
}
check("abort stops the wait", aborted && aborted.name === "AbortError", aborted && aborted.name);

let release;
context.addEventListener("plexora:video-readiness", (event) => {
    event.detail.wait(new Promise((resolve) => { release = resolve; }));
});
const waiting = R.wait(env(), { timeoutMs: 2000 });
let settledEarly = false;
waiting.then(() => { settledEarly = true; });
await new Promise((resolve) => setTimeout(resolve, 80));
check("a plugin's wait holds the frame", !settledEarly);
release();
await waiting;
check("…until it resolves", true);
listeners["plexora:video-readiness"] = [];

// The network tracker: data requests only.
let hits = 0;
context.fetch = (url) => { hits += 1; return new Promise((resolve) => setTimeout(() => resolve({ ok: true, url }), 30)); };
const tracker = R.trackNetwork();
const a = context.fetch("/generated/data/demo/k/0/0_0.webp");
const b = context.fetch("/agent/v1/viewer/poll");
const c = context.fetch("/plugins/video_studio/api/state");
check("a data request is counted", tracker.tracker.inFlight === 1, tracker.tracker.inFlight);
await Promise.all([a, b, c]);
check("…until it ends", tracker.tracker.inFlight === 0);
tracker.stop();
check("every request still went out", hits === 3, hits);

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
