/**
 * Auto threshold, and the marker changing while the fit is in flight.
 *
 * Auto awaits the server's fit. The marker can move under it in that time:
 * Z / X, the footer's Previous / Next and the dropdown all call
 * setGateMarker, and none of them waits for the fit. When the fit lands it
 * belongs to the marker Auto was pressed on, and nothing else: the marker on
 * screen now keeps the gate it has, gets no save, and Auto's pending Revert is
 * recorded (or not) for the marker it was pressed on.
 *
 * The real onAutoClick, autoGate, setGateRange, scheduleSaveGating,
 * stepMarker and CSVGatingList#getGatingGMM, called with `.call()` against a
 * hand-built `this` -- the same arrangement as gating_nudge_probe.mjs. The
 * server's answer is a promise resolved by hand, so the interleaving is fixed.
 *
 * Run directly: `node tests/js/gating_auto_marker_switch_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "gating", "static");

const timers = [];
const sandbox = {
    console,
    document: {
        getElementById: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        removeEventListener: () => {},
    },
};
sandbox.window = sandbox;
sandbox.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
sandbox.clearTimeout = () => {};
createContext(sandbox);
runInContext(
    readFileSync(join(STATIC, "csvGatingList.js"), "utf8")
    + "\n" + readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;"
    + "\nglobalThis.CSVGatingList = CSVGatingList;",
    sandbox);
const proto = sandbox.GatingSidebarController.prototype;
const listProto = sandbox.CSVGatingList.prototype;

const dataLayer = {
    getFullChannelName: (name) => name,
    gateDecimals: () => 1,
    gateValueDecimals: () => 1,
};

/**
 * The panel on marker A, with B gated by hand at [42.5, 100] and B's fit from
 * an earlier Auto press (since reverted) still cached at 10.
 */
function panel() {
    const pending = [];
    const gatingList = {
        columns: ["A", "B"],
        dataLayer,
        gating_channels: { A: [0, 100], B: [42.5, 100] },
        selections: {},
        hasGatingGMM: { B: { gate: 10 } },
        api: {
            // The server's fit: answered by hand, later.
            getGatingGMM: (channel) => new Promise((resolve) => pending.push({ channel, resolve })),
        },
        getGatingGMM: listProto.getGatingGMM,
    };
    const self = {
        gateMarker: "A",
        autoBusy: false,
        preAutoGates: new Map(),
        dataLayer,
        gatingList,
        sidebar: {
            databaseDescription: { A: { min: 0, max: 100 }, B: { min: 0, max: 100 } },
            isRestoring: () => false,
        },
        eventHandler: { trigger: () => {} },
        planes: null,
        gateSlider: null,
        pending,
        // The marker switch itself: what the real one does to the state read here.
        setGateMarker(name) { this.gateMarker = name; },
        updateGateThresholdLines() {},
        sizeGateFields() {},
        syncGateSlider() {},
        paintQcNote() {},
        persistGatingList() {},
    };
    for (const name of ["onAutoClick", "autoGate", "setGateRange", "normalizeGateRange",
                        "getGateRange", "scheduleSaveGating", "updateGateReadout",
                        "syncAutoButton", "revertGate", "stepMarker", "getGateMarkerNames"]) {
        self[name] = proto[name];
    }
    return self;
}

/** Let every queued microtask run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

const passed = [];
const failed = [];
/** Every check runs, so one failure does not hide the others. */
async function check(label, fn) {
    timers.length = 0;
    try {
        await fn();
    } catch (error) {
        failed.push(label);
        console.log(`FAIL ${label}\n    ${String(error.message).split("\n")[0]}`);
        return;
    }
    passed.push(label);
    console.log(`PASS ${label}`);
}

await check("the fit asked for is the marker Auto was pressed on", async () => {
    const self = panel();
    const run = self.onAutoClick();
    assert.equal(self.pending.length, 1);
    assert.equal(self.pending[0].channel, "A");
    self.pending[0].resolve({ gate: 30 });
    await run;
});

await check("X while the fit is in flight leaves the next marker's hand-set gate alone", async () => {
    const self = panel();
    const run = self.onAutoClick();
    assert.equal(self.autoBusy, true);
    assert.equal(self.stepMarker(1), true, "X moves to B while Auto runs on A");
    assert.equal(self.gateMarker, "B");
    self.pending[0].resolve({ gate: 30 });
    await run;
    await settle();
    assert.deepEqual([...self.gatingList.gating_channels.B], [42.5, 100],
        "B's hand-set gate was replaced by B's cached automatic one");
});

await check("X while the fit is in flight schedules no save of the next marker", async () => {
    const self = panel();
    const run = self.onAutoClick();
    self.stepMarker(1);
    self.pending[0].resolve({ gate: 30 });
    await run;
    await settle();
    const saves = timers.filter((timer) => timer.ms === 400);
    assert.equal(saves.length, 0,
        `a save was scheduled with B=${JSON.stringify(self.gatingList.gating_channels.B)}`);
});

await check("a gate that did change under Auto can always be reverted", async () => {
    const self = panel();
    const run = self.onAutoClick();
    self.stepMarker(1);
    self.pending[0].resolve({ gate: 30 });
    await run;
    await settle();
    for (const name of ["A", "B"]) {
        const now = self.gatingList.gating_channels[name];
        const start = name === "A" ? [0, 100] : [42.5, 100];
        if (now[0] !== start[0] || now[1] !== start[1]) {
            assert.ok(self.preAutoGates.has(name),
                `${name} went from ${JSON.stringify(start)} to ${JSON.stringify(now)} with no Revert`);
        }
    }
});

if (failed.length) {
    console.log(`${failed.length} checks failed`);
    process.exitCode = 1;
} else {
    console.log(`all checks passed (${passed.length})`);
}
