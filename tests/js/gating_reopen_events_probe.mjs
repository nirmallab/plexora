/**
 * Thresholding closed with its card's X and opened again leaves nothing of the
 * old instance listening on the page's event bus.
 *
 * Removing a tool runs `__plexora.deactivatePlugin`, which runs the cleanups
 * the plugin registered through `ctx.onCleanup` (main.js); reopening it runs
 * `activatePlugin`, which calls the definition's `bindEvents` again with a NEW
 * `moduleInstance`. Whatever `bindEvents` binds on the shared
 * SimpleEventHandler (which has no unbind) and does not hand to `onCleanup`
 * stays bound for the life of the page, and keeps acting: every gate commit
 * reruns the canvas gate once per earlier open, and Load Gates calls every
 * dead instance's `resetGatingList()`, which clicks rows in the NEW panel.
 *
 * The real gating definition (csvGatingList.js) and the real
 * SimpleEventHandler, run in a vm context; main.js's half -- a record whose
 * cleanups deactivate runs, and a context carrying `onCleanup` and the gate
 * callbacks -- is reproduced here as bindPluginEvents/deactivatePlugin build it.
 *
 * Run directly: `node tests/js/gating_reopen_events_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GATING = join(REPO, "plexora", "plugins", "gating", "static", "csvGatingList.js");
const EVENTS = join(REPO, "plexora", "client", "src", "js", "services", "simpleEventHandler.js");

const registered = new Map();
const windowListeners = new Map();
const fakeWindow = {
    Plexora: { registerPlugin: (definition) => registered.set(definition.name, definition) },
    addEventListener(name, fn) {
        if (!windowListeners.has(name)) windowListeners.set(name, new Set());
        windowListeners.get(name).add(fn);
    },
    removeEventListener(name, fn) {
        windowListeners.get(name)?.delete(fn);
    },
};
const sandbox = {
    window: fakeWindow,
    console,
    EventTarget,
    CustomEvent,
    Event,
};
sandbox.globalThis = sandbox;
createContext(sandbox);
runInContext(readFileSync(EVENTS, "utf-8") + "\nglobalThis.SimpleEventHandler = SimpleEventHandler;", sandbox,
             { filename: EVENTS });
runInContext(readFileSync(GATING, "utf-8") + "\nglobalThis.CSVGatingList = CSVGatingList;", sandbox,
             { filename: GATING });

const definition = registered.get("gating");
assert.ok(definition?.bindEvents, "csvGatingList.js registers the gating plugin with bindEvents");

const CoreEvents = { RESET_LISTS: "RESET_LISTS" };
const Events = sandbox.CSVGatingList.events;
const eventHandler = new sandbox.SimpleEventHandler(new EventTarget());

const counts = { repaint: 0, selection: 0, centroids: 0, segmentation: 0 };
const plugins = new Map();

function makeInstance(label) {
    return {
        label,
        resets: 0,
        resetGatingList() {
            this.resets += 1;
        },
        rebuildSliders() {},
    };
}

/** main.js's bindPluginEvents + pluginContext for one activation. */
function activate(label) {
    const record = { definition, instance: makeInstance(label), cleanups: [] };
    plugins.set(definition.name, record);
    definition.bindEvents({
        eventHandler,
        coreEvents: CoreEvents,
        onCleanup: (fn) => record.cleanups.push(fn),
        instance: record.instance,
        moduleInstance: record.instance,
        seaDragonViewer: { forceRepaint: () => { counts.repaint += 1; } },
        updateSeaDragonSelection: () => { counts.selection += 1; },
        updateCentroidsForGate: () => { counts.centroids += 1; },
        runSegmentationGate: () => { counts.segmentation += 1; },
    });
    return record.instance;
}

/** main.js's deactivatePlugin: run the cleanups, forget the record. */
function deactivate() {
    const record = plugins.get(definition.name);
    for (const fn of record.cleanups) fn();
    plugins.delete(definition.name);
}

function reset() {
    for (const key of Object.keys(counts)) counts[key] = 0;
}

let failed = 0;
function check(name, fn) {
    try {
        fn();
        console.log(`PASS ${name}`);
    } catch (error) {
        failed += 1;
        console.log(`FAIL ${name}\n  ${error.message.split("\n").join("\n  ")}`);
    }
}

// Open, close with the X, open again -- three times over.
const first = activate("first");
deactivate();
const second = activate("second");
deactivate();
const third = activate("third");
deactivate();
const live = activate("live");

check("the resize listener of a closed Thresholding is removed", () => {
    assert.equal(windowListeners.get("resize")?.size ?? 0, 1);
});

check("a gate commit after reopening runs the canvas gate once", () => {
    reset();
    eventHandler.trigger(Events.SELECTION_CHANGED, {});
    assert.deepEqual({ ...counts }, { repaint: 0, selection: 1, centroids: 1, segmentation: 1 },
                     "SELECTION_CHANGED handlers still bound from closed instances");
});

check("a brush tick after reopening runs the canvas gate once", () => {
    reset();
    eventHandler.trigger(Events.GATING_BRUSH_MOVE, {});
    assert.deepEqual({ ...counts }, { repaint: 0, selection: 1, centroids: 0, segmentation: 1 },
                     "GATING_BRUSH_MOVE handlers still bound from closed instances");
});

check("Load Gates resets only the open instance, never a closed one", () => {
    eventHandler.trigger(Events.RESET_GATINGLIST);
    assert.deepEqual(
        [first.resets, second.resets, third.resets, live.resets], [0, 0, 0, 1],
        "resetGatingList calls per instance [first, second, third, live]");
});

check("with Thresholding closed, a Cells-mode change reaches no gating handler", () => {
    deactivate();
    reset();
    eventHandler.trigger("SELECTION_CHANGED", {});
    assert.deepEqual({ ...counts }, { repaint: 0, selection: 0, centroids: 0, segmentation: 0 },
                     "SELECTION_CHANGED from viewerControls still reaches closed gating instances");
});

if (failed) {
    console.log(`${failed} check(s) failed`);
    process.exit(1);
}
console.log("all checks passed");
