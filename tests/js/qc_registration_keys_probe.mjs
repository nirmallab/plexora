/**
 * The Registration Check in the QC panel: Z / X / F, the flicker, and the
 * channel slots it borrows.
 *
 * What is worth pinning is what stays quiet and what is put back, because
 * each failure lands somewhere else: a Z typed into a search box that also
 * steps the comparison, a key meant for gating answered by QC, a flicker that
 * touches the channels at all (it is zebra stripes on the overlay: no tile
 * refetched, no channel recoloured), a flicker left running over a hidden
 * panel, and channel slots 1-2 left holding the check's channels after it is
 * turned off.
 *
 * The real class from qcRegistration.js, in a vm with a hand-built document,
 * viewer and channel panel.
 *
 * Run directly: `node tests/js/qc_registration_keys_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "qc", "static", "qcRegistration.js");

let activeElement = null;
let dialogOpen = false;
let activeTool = "qc";
let hidden = false;
let focused = true;
const listeners = {};
const timers = new Map();
let nextTimer = 1;
const repaints = [];
const refetches = [];

function hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

const panel = {
    channelSlots: [
        { name: "CD3", enabled: true, visible: true, colorHex: "#ffffff", color: hexToRgb("#ffffff"),
          userColorChanged: false },
        { name: "CD8", enabled: true, visible: true, colorHex: "#00ff00", color: hexToRgb("#00ff00"),
          userColorChanged: true },
        { name: "CD20", enabled: true, visible: true, colorHex: "#0000ff", color: hexToRgb("#0000ff"),
          userColorChanged: false },
    ],
    setSlotMarker(i, name, options) {
        const slot = this.channelSlots[i];
        if (slot.name !== name || options.enable !== slot.enabled) refetches.push([i, name]);
        slot.name = name;
        if (options.enable !== undefined) slot.enabled = Boolean(options.enable);
    },
    setSlotEnabled(i, on) { this.channelSlots[i].enabled = on; refetches.push([i, on]); },
    setSlotColor(i, hex, user) {
        const slot = this.channelSlots[i];
        slot.colorHex = hex;
        slot.color = hexToRgb(hex);
        slot.userColorChanged = Boolean(user || slot.userColorChanged);
    },
    suspendPersistence() {},
    resumePersistence() {},
};

const sandbox = {
    console,
    document: {
        get activeElement() { return activeElement; },
        get hidden() { return hidden; },
        hasFocus: () => focused,
        querySelector: (sel) => (sel === "dialog[open]" && dialogOpen ? {} : null),
        getElementById: () => null,
        addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => {
            listeners[name] = (listeners[name] || []).filter((f) => f !== fn);
        },
    },
    setInterval: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
    clearInterval: (id) => timers.delete(id),
    setTimeout: () => 0,
    clearTimeout: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
};
sandbox.window = sandbox;
sandbox.PlexoraToolLoader = { activeTool: () => activeTool };
sandbox.__plexora = {
    viewerSidebar: panel,
    dataLayer: { getFullChannelName: (name) => `full:${name}` },
    seaDragonViewer: { updateChannelColors: (name, rgb) => repaints.push([name, { ...rgb }]) },
};
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8") + "\nglobalThis.QcRegistration = QcRegistration;",
             sandbox);

const calls = [];
const api = {
    registrationStep: async (direction) => { calls.push(["step", direction]); return { ok: false, data: {} }; },
    registrationSet: async (body) => { calls.push(["set", body]); return { ok: false, data: {} }; },
    registrationCompute: async () => ({ ok: false, data: {} }),
    registration: async () => ({ ok: false, data: {} }),
};
const host = { message: () => {} };
const reg = new sandbox.QcRegistration({ layers: {}, viewer: {} }, api, host);
reg.render = () => {};
reg.renderKeys = () => {};
reg.scheduleCompute = () => {};

function state(extra = {}) {
    return { active: true, status: "ready", reference: "DNA_1", comparison: "DNA_2",
             candidates: ["DNA_1", "DNA_2", "DNA_3"], flicker: true, flicker_ms: 350,
             overlay_visible: true, params: {},
             colors: { reference: { color: "#22e6e6", user_set: false },
                       comparison: { color: "#ff3fb3", user_set: false } }, ...extra };
}

function key(k, extra = {}) {
    const event = { key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
                    repeat: false, prevented: false, preventDefault() { this.prevented = true; },
                    stopPropagation() {}, ...extra };
    for (const fn of listeners.keydown || []) fn(event);
    return event;
}

function check(name, fn) {
    fn();
    console.log(`ok  ${name}`);
}

check("turning it on puts the pair in slots 1 and 2 and leaves slot 3 alone", () => {
    reg.armKeys();
    reg.adopt(state());
    assert.equal(panel.channelSlots[0].name, "DNA_1");
    assert.equal(panel.channelSlots[1].name, "DNA_2");
    assert.equal(panel.channelSlots[2].name, "CD20");
    assert.equal(panel.channelSlots[0].colorHex, "#22e6e6");
});

check("a slot the user coloured keeps its colour", () => {
    assert.equal(panel.channelSlots[1].colorHex, "#00ff00");
});

check("flicker runs by default and repaints only the overlay, never a channel", () => {
    assert.equal(timers.size, 1);
    const before = refetches.length;
    repaints.length = 0;
    let invalidated = 0;
    reg.overlay = { invalidate: () => { invalidated += 1; } };
    reg.baseRaster = { key: reg.pairKey() };
    const [{ fn }] = [...timers.values()];
    fn();
    fn();
    assert.equal(refetches.length, before);
    assert.equal(repaints.length, 0);
    assert.equal(invalidated, 2);
    assert.equal(reg._phase, 2);
});

check("Z and X step the comparison while QC is the tool", () => {
    calls.length = 0;
    assert.ok(key("x").prevented);
    assert.ok(key("Z").prevented);
    assert.equal(JSON.stringify(calls), JSON.stringify([["step", "next"], ["step", "prev"]]));
});

check("F toggles the flicker through the server state", () => {
    calls.length = 0;
    key("f");
    assert.equal(JSON.stringify(calls[0]), JSON.stringify(["set", { flicker: false }]));
});

check("the keys stand down while typing, in a dialog, or under another tool", () => {
    activeElement = { tagName: "INPUT" };
    assert.equal(key("x").prevented, false);
    activeElement = null;
    dialogOpen = true;
    assert.equal(key("x").prevented, false);
    dialogOpen = false;
    activeTool = "gating";
    assert.equal(key("x").prevented, false);
    activeTool = "qc";
    assert.equal(key("x", { ctrlKey: true }).prevented, false);
});

check("stopping the flicker stops the timer and leaves both channels as they were", () => {
    repaints.length = 0;
    reg.adopt(state({ flicker: false }));
    assert.equal(timers.size, 0);
    assert.equal(repaints.length, 0);
    assert.equal(panel.channelSlots[0].colorHex, "#22e6e6");
    assert.equal(panel.channelSlots[1].colorHex, "#00ff00");
});

check("a hidden tab, a lost focus, or a hidden panel stops the timer", () => {
    reg.adopt(state({ flicker: true }));
    assert.equal(timers.size, 1);
    hidden = true;
    reg.syncFlicker();
    assert.equal(timers.size, 0);
    hidden = false;
    focused = false;
    reg.syncFlicker();
    assert.equal(timers.size, 0);
    focused = true;
    reg.syncFlicker();
    assert.equal(timers.size, 1);
    reg.onHide();
    assert.equal(timers.size, 0);
    assert.equal(key("x").prevented, false);
    reg.onShow();
    assert.equal(timers.size, 1);
});

check("turning it off puts slots 1 and 2 back and stops everything", () => {
    reg.adopt(state({ active: false, status: "inactive" }));
    assert.equal(timers.size, 0);
    assert.equal(panel.channelSlots[0].name, "CD3");
    assert.equal(panel.channelSlots[1].name, "CD8");
    assert.equal(panel.channelSlots[0].colorHex, "#ffffff");
    assert.equal(panel.channelSlots[1].colorHex, "#00ff00");
    assert.equal(panel.channelSlots[2].name, "CD20");
    assert.equal(key("x").prevented, false);
});
