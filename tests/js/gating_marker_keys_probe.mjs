/**
 * Z and X in Thresholding: previous and next marker, from the keyboard.
 *
 * What is worth pinning is when the keys stand down, because each of those
 * failures is silent and lands somewhere else: an "x" typed into the marker
 * search that also steps the marker, a Z meant for ROI (whose undo is mod+Z)
 * moving the gate, a key at the end of the list swallowed for nothing.
 *
 * The real methods, called with `.call()` against a hand-built `this` -- the
 * same arrangement as gating_consistency_probe.mjs.
 *
 * Run directly: `node tests/js/gating_marker_keys_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "gating", "static",
                    "gatingSidebarController.js");

let activeElement = null;
let dialogOpen = false;
let activeTool = "gating";
const listeners = { keydown: [] };
const sandbox = {
    console,
    document: {
        get activeElement() { return activeElement; },
        querySelector: (sel) => (sel === "dialog[open]" && dialogOpen ? {} : null),
        addEventListener: (name, fn) => listeners[name].push(fn),
        removeEventListener: (name, fn) => {
            listeners[name] = listeners[name].filter((f) => f !== fn);
        },
    },
};
sandbox.window = sandbox;
sandbox.clearTimeout = () => {};
sandbox.PlexoraToolLoader = { activeTool: () => activeTool };
createContext(sandbox);
runInContext(
    readFileSync(SOURCE, "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;",
    sandbox);
const proto = sandbox.GatingSidebarController.prototype;

function state(selected = "CD4", markers = ["CD3", "CD4", "CD8"]) {
    const picked = [];
    const self = {
        _keysArmed: true,
        gateMarker: selected,
        gateMarkerChangeTimer: null,
        getGateMarkerNames: () => [...markers],
        setGateMarker(name, options) { picked.push([name, options]); this.gateMarker = name; },
    };
    for (const name of ["acceptsKeys", "stepMarker", "onMarkerKey", "armKeys", "disarmKeys",
                        "setDefaultGateMarker"]) {
        self[name] = proto[name];
    }
    self._onKeyDown = (event) => self.onMarkerKey(event);
    return { self, picked };
}

function press(self, key, extra = {}) {
    let prevented = false;
    self.onMarkerKey({ key, preventDefault: () => { prevented = true; }, ...extra });
    return prevented;
}

function reset() { activeElement = null; dialogOpen = false; activeTool = "gating"; }

const passed = [];
function check(label, fn) {
    reset();
    fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

check("X steps to the next marker, as a pick from the list would", () => {
    const { self, picked } = state();
    assert.equal(press(self, "x"), true);
    assert.equal(picked.length, 1);
    assert.equal(picked[0][0], "CD8");
    assert.equal(picked[0][1], undefined, "the dropdown's own call: no options");
});

check("Z steps to the previous marker", () => {
    const { self, picked } = state();
    press(self, "z");
    assert.equal(picked[0][0], "CD3");
});

check("Caps Lock still steps", () => {
    const { self, picked } = state();
    press(self, "X");
    assert.equal(picked[0][0], "CD8");
});

check("at either end the key does nothing and is not swallowed", () => {
    const last = state("CD8");
    assert.equal(press(last.self, "x"), false);
    const first = state("CD3");
    assert.equal(press(first.self, "z"), false);
    assert.equal(last.picked.length + first.picked.length, 0);
});

check("with nothing selected X picks the first marker", () => {
    const { self, picked } = state(null);
    press(self, "x");
    assert.equal(picked[0][0], "CD3");
    const again = state(null);
    assert.equal(press(again.self, "z"), false);
});

check("typing into the marker search is typing", () => {
    for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
        activeElement = { tagName };
        const { self, picked } = state();
        assert.equal(press(self, "x"), false);
        assert.equal(picked.length, 0, tagName);
    }
});

check("a dialog owns the window", () => {
    dialogOpen = true;
    const { self, picked } = state();
    press(self, "x");
    assert.equal(picked.length, 0);
});

check("the keys belong to the selected tool only", () => {
    activeTool = "roi";
    const { self, picked } = state();
    press(self, "z");
    assert.equal(picked.length, 0);
});

check("a modified Z is somebody else's shortcut", () => {
    const { self, picked } = state();
    for (const mod of ["metaKey", "ctrlKey", "altKey", "shiftKey"]) {
        assert.equal(press(self, "z", { [mod]: true }), false);
    }
    assert.equal(picked.length, 0);
});

check("put away, the panel stops listening", () => {
    const { self, picked } = state();
    self._keysArmed = false;
    press(self, "x");
    assert.equal(picked.length, 0);
});

check("arming is idempotent and disarming removes the listener", () => {
    const { self } = state();
    self._keysArmed = false;
    self.armKeys();
    self.armKeys();
    assert.equal(listeners.keydown.length, 1, "onShow runs on every reopen");
    self.disarmKeys();
    assert.equal(listeners.keydown.length, 0);
});

check("a fresh sample's default marker leaves carried channels alone", () => {
    const { self, picked } = state(null);
    sandbox.PlexoraCarryOver = { current: () => ({ components: { channels: { entries: [{ name: "CD8" }] } } }) };
    self.setDefaultGateMarker();
    assert.equal(picked[0][0], "CD4", "the second marker, as before");
    assert.equal(picked[0][1].syncSlot, false, "slot 1 holds the carried channel");
    delete sandbox.PlexoraCarryOver;
    const plain = state(null);
    plain.self.setDefaultGateMarker();
    assert.equal(plain.picked[0][1].syncSlot, true, "an ordinary open still mirrors it");
});

console.log(`all checks passed (${passed.length})`);
