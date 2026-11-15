/**
 * Walking a dataset (Previous / Next / a strip pick) as a page that LEAVES.
 *
 * views/datasetNav.js's go() is a full navigation. Two things have to hold
 * around it, and both fail without a word:
 *
 *   - WHAT THE USER JUST DID IS SAVED BEFORE THE PAGE GOES. The plane
 *     (services/viewState.js), the rotation (services/viewTransform.js) and
 *     the viewer preferences (services/viewerPrefs.js) each save on a 400 ms
 *     debounce. A navigation throws the pending timer away with the page, so
 *     an edit made just before N is pressed never reaches the server and the
 *     sample reopens as it was before the edit.
 *   - A WALK THAT DID NOT HAPPEN LEAVES THE WALK WORKING. go() refuses a
 *     second walk while one is "in flight". A leave the user cancels (Stay at
 *     ROI's beforeunload prompt) or a page brought back from the back/forward
 *     cache is still this page, and Next must work on it again.
 *
 * The real service files and the real datasetNav.js run together in one
 * node:vm context. Timers are a fake clock that only moves when a check moves
 * it: a navigation that happens never lets a pending debounce fire.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");
const read = (path) => readFileSync(join(JS, path), "utf8");
const SOURCES = [
    "services/inspectGeometry.js",
    "services/viewState.js",
    "services/viewTransform.js",
    "services/viewerPrefs.js",
    "views/datasetNav.js",
].map((path) => [path, read(path)]);

let checks = 0;
const failures = [];
//: Every check runs, so one failure does not hide what the others say.
async function checkAsync(label, fn) {
    try {
        await fn();
    } catch (error) {
        failures.push(label);
        console.log("  FAIL  " + label + "\n        " + (error && error.message));
        return;
    }
    checks += 1;
    console.log("  ok  " + label);
}

const VOLUME = { volumetric: true, depth: 40, frames: 1, width: 2000, height: 1500, default_view: "z20" };

/** Let promises (the bridge, a flush that awaits) run; the fake clock stays put. */
const ticks = async (n = 20) => {
    for (let i = 0; i < n; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

function boot({ here = "b" } = {}) {
    // -- a clock that only moves when told to --------------------------------
    let clock = 0;
    let nextId = 1;
    const timers = new Map();
    const setTimeoutFake = (fn, ms = 0) => {
        const id = nextId++;
        timers.set(id, { fn, due: clock + Math.max(0, Number(ms) || 0) });
        return id;
    };
    const clearTimeoutFake = (id) => { timers.delete(id); };
    const advance = (ms) => {
        const until = clock + ms;
        for (;;) {
            let first = null;
            for (const [id, timer] of timers) {
                if (timer.due <= until && (!first || timer.due < first[1].due)) first = [id, timer];
            }
            if (!first) break;
            timers.delete(first[0]);
            clock = first[1].due;
            first[1].fn();
        }
        clock = until;
    };

    // -- the window's own listeners -------------------------------------------
    const listeners = {};
    const dispatch = (name, event = {}) => {
        for (const fn of [...(listeners[name] || [])]) fn({ type: name, ...event });
    };

    // -- every request the page makes -----------------------------------------
    const requests = [];
    const log = [];
    const fetchFake = (address, options = {}) => {
        const method = (options.method || "GET").toUpperCase();
        const entry = { url: String(address), method, keepalive: Boolean(options.keepalive), settled: false };
        requests.push(entry);
        if (method !== "GET") log.push(["put", entry]);
        let body = null;
        if (method === "GET") {
            if (entry.url.includes("volume/" + here + "/info")) body = VOLUME;
            else if (entry.url.includes("view_state/")) body = {};
            else if (entry.url.includes("viewer_prefs")) body = {};
        }
        return Promise.resolve().then(() => {
            entry.settled = true;
            return { ok: body !== null || method !== "GET", json: async () => body || {} };
        });
    };

    const navigated = [];
    const context = {
        console: { log() {}, error() {}, warn() {}, info() {} },
        JSON, Date, Set, Map, Object, Array, Number, String, Boolean, Math, Promise,
        Error, URLSearchParams, encodeURIComponent,
        performance: { now: () => clock },
        setTimeout: setTimeoutFake,
        clearTimeout: clearTimeoutFake,
        requestAnimationFrame: (fn) => setTimeoutFake(fn, 16),
        cancelAnimationFrame: clearTimeoutFake,
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
        dispatchEvent: () => true,
        location: { search: "", href: "http://127.0.0.1/" + here },
        fetch: fetchFake,
        navigator: {},
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
        document: {
            readyState: "complete",
            // No canvas: mount() stands down, and go() needs none of it.
            getElementById: () => null,
            querySelector: () => null,
            addEventListener() {},
            removeEventListener() {},
            createElement: () => ({ appendChild() {}, setAttribute() {}, addEventListener() {} }),
            activeElement: null,
        },
    };
    context.window = context;
    context.globalThis = context;
    context.addEventListener = (name, fn) => { (listeners[name] ||= []).push(fn); };
    context.removeEventListener = (name, fn) => {
        listeners[name] = (listeners[name] || []).filter((f) => f !== fn);
    };
    context.flaskVariables = { datasource: here, datasources: ["a", "b", "c"] };
    context.PlexoraCarryOver = { stash: () => true };
    context.PlexoraToolLoader = { activeTool: () => "" };
    // The router hands a cross-project link to the browser (appRouter.go);
    // what the browser then does is up to each check.
    context.PlexoraRouter = {
        go: (href) => {
            navigated.push(href);
            log.push(["navigate", href]);
        },
    };
    createContext(context);
    for (const [path, source] of SOURCES) {
        runInContext(source, context, { filename: path });
    }
    return { context, navigated, requests, log, dispatch, advance, api: context.PlexoraDatasetNav };
}

/** A save that is safe from the page going: sent with keepalive, or sent and
 *  answered before the page was handed to the browser. */
function survives(log, entry) {
    if (entry.keepalive) return true;
    const at = log.findIndex((row) => row[0] === "put" && row[1] === entry);
    const leftAt = log.findIndex((row) => row[0] === "navigate");
    return at !== -1 && leftAt !== -1 && at < leftAt && entry.settled;
}

console.log("dataset walk, leaving the page");

await checkAsync("a walk saves the plane, rotation and viewer prefs that were still waiting", async () => {
    const t = boot({ here: "b" });
    const ctx = t.context;
    // main.js: `new PlexoraViewState(datasource)`, kept as __plexora.viewState;
    // imageViewer.js: `this.viewTransform = new PlexoraViewTransform(...)`,
    // the image viewer kept as __plexora.seaDragonViewer.
    const viewState = new ctx.PlexoraViewState("b");
    const viewport = { setFlip() {}, setRotation() {}, goHome() {} };
    const viewTransform = new ctx.PlexoraViewTransform({ viewport }, { datasource: "b" });
    ctx.__plexora = { viewState, seaDragonViewer: { viewTransform } };
    await viewState.load({ search: "" });
    assert.ok(viewState.enabled(), "the stand-in image is a stack");

    // The user's last edits, each well inside its 400 ms debounce.
    viewState.set({ z: 7 });
    viewTransform.set({ degrees: 90 });
    ctx.PlexoraViewerPrefs.set({ motion: "off" });
    t.advance(100);
    assert.equal(t.requests.filter((r) => r.method === "PUT").length, 0,
        "nothing saved yet: all three are still debouncing");

    // N pressed. The page then goes: pagehide, and no timer of this page
    // ever fires again.
    t.api.go("c");
    await ticks();
    assert.equal(t.navigated.length, 1, "the walk went");
    t.dispatch("pagehide", { persisted: false });
    await ticks();

    const put = (path) => t.requests.find((r) => r.method === "PUT" && r.url.includes(path));
    for (const [path, what] of [["view_state/b", "the plane (z 7)"],
                                ["view_transform/b", "the rotation (90 degrees)"],
                                ["viewer_prefs", "the viewer preference (motion off)"]]) {
        const entry = put(path);
        assert.ok(entry, `${what} was never sent: its debounced save died with the page `
            + `(PUTs sent: ${JSON.stringify(t.requests.filter((r) => r.method === "PUT").map((r) => r.url))})`);
        assert.ok(survives(t.log, entry),
            `${what} was sent as an ordinary request after the page was already leaving`);
    }
});

await checkAsync("a page left any other way (a link, Back, closing the tab) still sends what was waiting", async () => {
    const t = boot({ here: "b" });
    const ctx = t.context;
    const viewState = new ctx.PlexoraViewState("b");
    const viewport = { setFlip() {}, setRotation() {}, goHome() {} };
    const viewTransform = new ctx.PlexoraViewTransform({ viewport }, { datasource: "b" });
    // The channel list (viewerSidebar.js flushChannelSave) and the Layers
    // panel (layerManager.js flush) save on debounces of their own; each is
    // asked for what is waiting, with keepalive.
    const asked = [];
    ctx.__plexora = {
        viewState,
        seaDragonViewer: { viewTransform },
        viewerSidebar: { flushChannelSave: (options) => asked.push(["channels", options]) },
    };
    ctx.PlexoraLayerManager = { flush: (options) => asked.push(["layers", options]) };
    await viewState.load({ search: "" });

    viewState.set({ z: 7 });
    ctx.PlexoraViewerPrefs.set({ motion: "off" });
    t.advance(100);
    // No walk: the navbar, the browser's Back, or the tab closed. The page
    // goes, and its timers with it.
    t.dispatch("pagehide", { persisted: false });
    await ticks();

    const put = (path) => t.requests.find((r) => r.method === "PUT" && r.url.includes(path));
    for (const [path, what] of [["view_state/b", "the plane (z 7)"], ["viewer_prefs", "the viewer preference"]]) {
        const entry = put(path);
        assert.ok(entry, `${what} was never sent: only a dataset walk flushed the saves`);
        assert.ok(entry.keepalive, `${what} went without keepalive, as the page was going`);
    }
    for (const what of ["channels", "layers"]) {
        const call = asked.find((row) => row[0] === what);
        assert.ok(call, `the ${what} save still waiting was never asked for on pagehide`);
        assert.equal(call[1] && call[1].keepalive, true, `the ${what} save was asked for without keepalive`);
    }
});

await checkAsync("a walk the browser did not take (Stay at the leave prompt) leaves Next working", async () => {
    const t = boot({ here: "b" });
    t.api.go("c");
    await ticks();
    assert.equal(t.navigated.length, 1, "the first walk reached the router");
    // The browser asked "Leave site?" and the user stayed: no pagehide, the
    // page lives on. Give it all the time in the world.
    t.advance(120000);
    await ticks();
    t.api.go("a");
    await ticks();
    assert.equal(t.navigated.length, 2,
        "the second walk never reached the router: go() still thinks the page is leaving");
});

await checkAsync("a page back from the back/forward cache can walk again", async () => {
    const t = boot({ here: "b" });
    t.api.go("c");
    await ticks();
    assert.equal(t.navigated.length, 1);
    // It did go -- and Back brought this very page back from the bfcache.
    t.dispatch("pagehide", { persisted: true });
    t.dispatch("pageshow", { persisted: true });
    await ticks();
    t.api.go("a");
    await ticks();
    assert.equal(t.navigated.length, 2,
        "the restored page's Next is dead: go() still thinks the page is leaving");
});

if (failures.length) {
    console.log(`\n${failures.length} checks failed`);
    process.exit(1);
}
console.log(`\n${checks} checks passed`);
