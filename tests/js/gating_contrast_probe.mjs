/**
 * The gating calibration's brightness on the viewer's own channels
 * (viewerSidebar.js: syncGatingContrast, the gating button, provenance).
 *
 * Pinned here, on the methods as shipped (extracted and run, as
 * channel_auto_revert_probe.mjs does):
 *
 *   - a window the user set -- by hand, or saved before rows said who set
 *     them ("legacy") -- is never replaced; the button is offered instead;
 *   - the button puts the calibration's window AND curve on, says so
 *     (source "gating"), and a second press puts back exactly what was there;
 *   - a window the viewer levelled itself (auto) or a seeded default is
 *     replaced, and a later drag makes the slot the user's again, so a newer
 *     calibration leaves it alone;
 *   - a saved row carries who set the window and how it is drawn.
 *
 * Run directly:  node tests/js/gating_contrast_probe.mjs
 */

import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const require = createRequire(import.meta.url);
globalThis.PlexoraDisplayCurve = require(path.join(repoRoot, "plexora", "client", "src", "js",
                                                  "services", "displayCurve.js"));
globalThis.ChannelList = { events: { BRUSH_MOVE: "BRUSH_MOVE", CURVE_CHANGE: "CURVE_CHANGE" } };

function extractMethod(source, name) {
    const signature = new RegExp(`\\n    (?:async )?${name}\\(`).exec(source);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    let p = source.indexOf("(", start);
    for (let parens = 0; p < source.length; p++) {
        if (source[p] === "(") parens++;
        else if (source[p] === ")" && --parens === 0) break;
    }
    let i = source.indexOf("{", p);
    let depth = 0;
    let state = "code";
    for (; i < source.length; i++) {
        const c = source[i];
        const next = source[i + 1];
        if (state === "line") { if (c === "\n") state = "code"; continue; }
        if (state === "block") { if (c === "*" && next === "/") { state = "code"; i++; } continue; }
        if (c === "/" && next === "/") { state = "line"; i++; continue; }
        if (c === "/" && next === "*") { state = "block"; i++; continue; }
        if (c === '"' || c === "'" || c === "`") {
            const quote = c;
            for (i++; i < source.length; i++) {
                if (source[i] === "\\") { i++; continue; }
                if (source[i] === quote) break;
            }
            continue;
        }
        if (c === "{") depth++;
        else if (c === "}" && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(`unterminated body for ${name}`);
}

const METHODS = [
    "syncGatingContrast", "applyGatingContrast", "onSlotGatingClick", "syncSlotGatingButton",
    "gatingEntry", "showsGating", "byteStep", "rawToByteRangeFine", "setSlotCurve",
    "easeSlotRange", "setSlotRange", "normalizeRange", "toRawRangeForSlot", "updateSlotReadout",
    "persistChannelList",
];
const source = await readFile(path.join(repoRoot, "plexora/client/src/js/views/viewerSidebar.js"),
                              "utf8");
// The two module constants the methods read.
const constants = `const GATING_EASE_MS = 0; const RANGE_EASE_MS = 0;
const REPLACEABLE_SOURCES = new Set(["auto", "default", "gating"]);`;
const methods = new Function(`${constants}\nreturn {\n${METHODS.map((n) => extractMethod(source, n))
    .join(",\n")}\n};`)();

let passed = 0;
function ok(name) { passed += 1; console.log(`ok - ${name}`); }

function makeButton() {
    const classes = new Set();
    return {
        hidden: false, dataset: {}, title: "", innerHTML: "", attributes: {},
        classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
        setAttribute(name, value) { this.attributes[name] = String(value); },
    };
}

const ENTRY = { window: [120, 5200], curve: { kind: "asinh", cofactor: 60 }, chosen_by: "vision" };

function makeSidebar(slotState = {}) {
    const button = makeButton();
    const slot = {
        index: 0, name: "CD3", enabled: true, visible: true, expanded: false,
        range: [500, 9000], color: { r: 255, g: 214, b: 10 }, colorHex: "#ffd60a",
        userRangeChanged: false, autoLeveled: true, autoLeveling: false, curve: null,
        rangeSource: "auto", ...slotState,
    };
    const sidebar = {
        ...methods,
        slot, button, saves: 0, events: [],
        channelSlots: [slot],
        markerRangeOverrides: new Map(),
        markerCurves: new Map(),
        gatingContrast: { state: "ready", rev: "r1", channels: { CD3: { ...ENTRY } } },
        channelList: { image_channels: {} },
        channelSlotSliders: new Map(),
        dataLayer: { getFullChannelName: (name) => name, imageBitRange: [0, 65535] },
        eventHandler: { trigger: (name, payload) => sidebar.events.push([name, payload]) },
        slotRow: () => ({ querySelector: () => button }),
        isHdMode: () => true,
        quantWindow: () => ({ qmin: 0, qmax: 65535 }),
        validRawRange: (_name, range) => Array.isArray(range),
        getRawImageRange: () => [0, 65535],
        syncChannelSlider() {},
        syncSlotCurve() {},
        ensureHdForCurve() {},
        scheduleSaveChannels() { sidebar.saves += 1; },
    };
    return sidebar;
}

// 1. A legacy row (saved before rows said who set it) is the user's.
{
    const sidebar = makeSidebar({ rangeSource: "legacy", userRangeChanged: true });
    assert.equal(sidebar.syncGatingContrast(), 0);
    assert.deepEqual(sidebar.slot.range, [500, 9000]);
    assert.equal(sidebar.button.hidden, false, "the button is offered");
    assert.equal(sidebar.button.dataset.state, "apply");
    ok("a legacy window is kept and the gating contrast offered");

    // 2. The button applies window and curve, then puts them back.
    sidebar.onSlotGatingClick(0);
    assert.deepEqual(sidebar.slot.range, ENTRY.window);
    assert.deepEqual(sidebar.slot.curve, ENTRY.curve);
    assert.equal(sidebar.slot.rangeSource, "gating");
    assert.equal(sidebar.slot.displayRev, "r1");
    assert.ok(sidebar.saves >= 1, "saved");
    assert.equal(sidebar.button.dataset.state, "revert");
    assert.ok(sidebar.events.some(([name, p]) => name === "CURVE_CHANGE" && p.curve.cofactor === 60),
              "the canvas is told the curve");
    sidebar.onSlotGatingClick(0);
    assert.deepEqual(sidebar.slot.range, [500, 9000]);
    assert.equal(sidebar.slot.curve, null);
    assert.equal(sidebar.slot.rangeSource, "legacy");
    assert.equal(sidebar.slot.userRangeChanged, true);
    assert.equal(sidebar.button.dataset.state, "apply");
    ok("the button applies the window and curve, and a second press restores them");
}

// 3. An auto-levelled window is replaced; a drag makes it the user's.
{
    const sidebar = makeSidebar();
    assert.equal(sidebar.syncGatingContrast(), 1);
    assert.deepEqual(sidebar.slot.range, ENTRY.window);
    assert.equal(sidebar.slot.rangeSource, "gating");
    assert.equal(sidebar.button.hidden, true, "nothing to offer: it is on");
    ok("a window the viewer levelled itself takes the gating contrast");

    assert.equal(sidebar.syncGatingContrast(), 0, "already on: nothing moves");
    sidebar.gatingContrast = { rev: "r2", channels: { CD3: { ...ENTRY, window: [100, 4000] } } };
    assert.equal(sidebar.syncGatingContrast(), 1, "a newer calibration replaces its own");
    assert.deepEqual(sidebar.slot.range, [100, 4000]);
    sidebar.setSlotRange(0, [300, 3000], true);
    assert.equal(sidebar.slot.rangeSource, "user");
    sidebar.gatingContrast = { rev: "r3", channels: { CD3: { ...ENTRY, window: [50, 2000] } } };
    assert.equal(sidebar.syncGatingContrast(), 0);
    assert.deepEqual(sidebar.slot.range, [300, 3000]);
    ok("a drag makes the window the user's, and a newer calibration leaves it");
}

// 4. A seeded default is replaced; a channel off is not touched.
{
    const sidebar = makeSidebar({ rangeSource: "default", userRangeChanged: true });
    assert.equal(sidebar.syncGatingContrast(), 1);
    const off = makeSidebar({ enabled: false });
    assert.equal(off.syncGatingContrast(), 0);
    ok("a default row takes it; a channel switched off does not");
}

// 5. The saved row says who set the window and how it is drawn.
{
    const sidebar = makeSidebar();
    sidebar.syncGatingContrast();
    globalThis.imageChannelsIdx = { 0: "CD3" };
    globalThis.imageChannels = { CD3: 0 };
    let saved = null;
    sidebar.persist = true;
    sidebar.dataLayer.saveChannelList = (...args) => { saved = args; return Promise.resolve(); };
    sidebar.persistChannelList();
    const meta = saved[5];
    assert.deepEqual(meta.CD3, { source: "gating", curve: "asinh", cofactor: 60, display_rev: "r1" });
    const mine = makeSidebar({ rangeSource: "legacy", userRangeChanged: true });
    mine.persist = true;
    mine.dataLayer.saveChannelList = (...args) => { saved = args; return Promise.resolve(); };
    mine.persistChannelList();
    assert.deepEqual(saved[5].CD3, { source: "user", curve: "linear" });
    ok("a saved row carries its source and curve; a legacy one saves as the user's");
}

console.log(`${passed} checks passed`);
