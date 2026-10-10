/**
 * How the viewer's sidebar sets contrast on a z-stack.
 *
 * Two things a real light-sheet stack (1937 planes, tissue under 1000, a few
 * saturated voxels at 65535) showed were wrong:
 *
 * - Fast 8-bit tiles map [0, the stack's brightest voxel] onto 256 levels, so
 *   the default contrast range got about two of them: a posterised picture.
 *   `askForHdIfCoarse` asks for HD (16-bit tiles) when that happens -- on a
 *   stack only, never on a flat image.
 * - A projection or the 3-D view kept the single plane's window, and a maximum
 *   over 1937 planes saturated it. `contrastView` says which view's statistics
 *   auto-contrast reads; `onVolumeHint` levels auto channels by the hints the
 *   3-D renderer received with the level it draws.
 *
 * The methods are sliced out of viewerSidebar.js and run against stand-ins, so
 * this measures the shipped code rather than a copy of it.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const source = await readFile(
    path.join(repoRoot, "plexora/client/src/js/views/viewerSidebar.js"), "utf8");

/** One method's text, by brace matching that skips comments and strings. */
function extractMethod(text, name) {
    const signature = new RegExp(`\\n    ${name}\\(`).exec(text);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    // The body's brace, not a destructured parameter's: `onVolumeHint({ name })`.
    let i = text.indexOf(") {", start) + 2;
    let depth = 0;
    let state = "code";
    for (; i < text.length; i++) {
        const c = text[i];
        const next = text[i + 1];
        if (state === "line") { if (c === "\n") state = "code"; continue; }
        if (state === "block") { if (c === "*" && next === "/") { state = "code"; i++; } continue; }
        if (c === "/" && next === "/") { state = "line"; i++; continue; }
        if (c === "/" && next === "*") { state = "block"; i++; continue; }
        if (c === '"' || c === "'" || c === "`") {
            const quote = c;
            for (i++; i < text.length; i++) {
                if (text[i] === "\\") { i++; continue; }
                if (text[i] === quote) break;
            }
            continue;
        }
        if (c === "{") depth++;
        else if (c === "}" && --depth === 0) return text.slice(start, i + 1);
    }
    throw new Error(`unterminated body for ${name}`);
}

const levels = Number(/ViewerSidebar\.MIN_DEFAULT_LEVELS = (\d+);/.exec(source)?.[1]);
assert.ok(levels > 0, "ViewerSidebar.MIN_DEFAULT_LEVELS is gone");

const METHODS = ["askForHdIfCoarse", "contrastView", "viewStats", "onVolumeHint"];
const body = METHODS.map((name) => extractMethod(source, name)).join(",\n");

/** A sidebar on a page whose view state is `state` (null: a flat image). */
function page({ state = null, hd = false, scoped = false } = {}) {
    const events = [];
    const document = {};
    const viewState = {
        frames: 1,
        enabled: () => state !== null,
        get: () => state,
        spec: (s) => (s.mode === "slab" ? `z${s.slab.z0}-${s.slab.z1}${s.slab.op}` : `z${s.z}`),
    };
    const context = createContext({
        document,
        window: { __plexora: { viewState }, dispatchEvent: (event) => events.push(event) },
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
        ViewerSidebar: { MIN_DEFAULT_LEVELS: levels },
        Promise,
    });
    const methods = runInContext(`({\n${body}\n})`, context);
    const levelled = [];
    const sidebar = Object.assign(Object.create(methods), {
        root: scoped ? {} : document,
        idPrefix: "",
        hdModeOverride: undefined,
        isHdMode: () => hd,
        _viewStats: new Map(),
        _volumeHints: new Map(),
        dataLayer: {
            getFullChannelName: (name) => `full:${name}`,
            getImageChannelStats: () => Promise.resolve(null),
        },
        channelSlots: [
            { name: "tdTomato", enabled: true, userRangeChanged: false },
            { name: "DAPI", enabled: true, userRangeChanged: true },
        ],
        autoChannel: (index) => levelled.push(index),
    });
    return { sidebar, events, levelled };
}

const PLANE = { mode: "plane", z: 968, slab: { z0: 0, z1: 1936, op: "max" } };
const SLAB = { ...PLANE, mode: "slab" };
const VOLUME = { ...PLANE, mode: "volume" };

// The light-sheet stack: default range 600-1138 of a 0-65535 window.
const LIGHT_SHEET = { qmin: 0, qmax: 65535, vmin_hint: 600, vmax_hint: 1138 };
// The Imaris stack: 8-bit data, default range 42-73 of 0-114.
const IMARIS = { qmin: 0, qmax: 114, vmin_hint: 42, vmax_hint: 73 };

// -- 1. a stack whose fast tiles cannot draw its default range asks for HD ----
{
    const { sidebar, events } = page({ state: PLANE });
    sidebar.askForHdIfCoarse(LIGHT_SHEET);
    assert.equal(events.length, 1);
    assert.equal(events[0].type, "plexora:hd-needed");
    assert.ok(events[0].detail.levels < 3, `got ${events[0].detail.levels}`);
    console.log("a stack whose default range spans two byte levels asks for HD");
}

// -- 2. one whose range spans plenty of levels does not ----------------------
{
    const { sidebar, events } = page({ state: PLANE });
    sidebar.askForHdIfCoarse(IMARIS);
    assert.equal(events.length, 0);
    console.log("a stack whose default range spans 69 byte levels stays on fast tiles");
}

// -- 3. never on a flat image, never twice, never for a scoped copy ----------
{
    for (const options of [{ state: null }, { state: PLANE, hd: true },
                           { state: PLANE, scoped: true }]) {
        const { sidebar, events } = page(options);
        sidebar.askForHdIfCoarse(LIGHT_SHEET);
        assert.equal(events.length, 0, JSON.stringify(options));
    }
    console.log("a flat image, an HD page and a scoped sidebar are left alone");
}

// -- 4. which view's statistics auto-contrast reads ---------------------------
{
    assert.equal(page({ state: PLANE }).sidebar.contrastView(), "");
    assert.equal(page({ state: SLAB }).sidebar.contrastView(), "z0-1936max");
    assert.equal(page({ state: VOLUME }).sidebar.contrastView(), "3d");
    assert.equal(page({ state: null }).sidebar.contrastView(), "");
    assert.equal(page({ state: SLAB, scoped: true }).sidebar.contrastView(), "");
    console.log("a plane reads the channel's statistics, a projection its own, the 3-D view its level's");
}

// -- 5. the 3-D view levels auto channels by its renderer's hints -------------
{
    const { sidebar, levelled } = page({ state: VOLUME });
    assert.equal(await sidebar.viewStats("tdTomato", "3d"), null);
    sidebar.onVolumeHint({ name: "full:tdTomato", hint: [976, 2616] });
    // Spread: the object was made in the probe's vm realm, with its own Object.
    assert.deepEqual({ ...(await sidebar.viewStats("tdTomato", "3d")) },
                     { vmin_hint: 976, vmax_hint: 2616 });
    assert.deepEqual(levelled, [0]);
    // A channel whose range the user set keeps it.
    sidebar.onVolumeHint({ name: "full:DAPI", hint: [10, 20] });
    assert.deepEqual(levelled, [0]);
    // Off the 3-D view the hint is kept for later and nothing moves.
    const plane = page({ state: PLANE });
    plane.sidebar.onVolumeHint({ name: "full:tdTomato", hint: [976, 2616] });
    assert.deepEqual(plane.levelled, []);
    console.log("the 3-D view's hints level only channels the user has not set, and only in 3-D");
}
