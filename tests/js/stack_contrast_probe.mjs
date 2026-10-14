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
 *
 * And one thing it does on purpose: a stack has ONE contrast per channel.
 * Every plane, projection and the 3-D view is drawn with the channel's own
 * level (`autoChannel`), and no view asks for statistics of its own -- that
 * cost a request and a server-side overview per projection, held its tiles
 * up to 1.5 s, and read the level again from disk for every cut.
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
    const signature = new RegExp(`\\n    (?:async )?${name}\\(`).exec(text);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    // The body's brace, not a destructured parameter's: `f({ name })`.
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

const METHODS = ["askForHdIfCoarse", "autoChannel"];
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
    const asked = [];
    const fits = [];
    const applied = [];
    const gmm = {};
    const sidebar = Object.assign(Object.create(methods), {
        root: scoped ? {} : document,
        idPrefix: "",
        hdModeOverride: undefined,
        isHdMode: () => hd,
        dataLayer: {
            getFullChannelName: (name) => `full:${name}`,
            // Any statistics request at all -- a view's would carry its token.
            getImageChannelStats: (...args) => { asked.push(args); return Promise.resolve(null); },
        },
        channelList: {
            hasChannelGMM: gmm,
            ensureChannelStats: () => Promise.resolve(),
            getAndDrawChannelGMM: (name) => {
                fits.push(name);
                gmm[name] = { vmin: 50, vmax: 70 };
                return Promise.resolve();
            },
        },
        quantWindow: () => IMARIS,
        applyAutoRange: (index, slot, range) => applied.push([index, [...range]]),
        channelSlots: [
            { index: 0, name: "tdTomato", enabled: true, userRangeChanged: false },
            { index: 1, name: "DAPI", enabled: true, userRangeChanged: true },
        ],
    });
    return { sidebar, events, asked, fits, applied };
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

// -- 4. a plane, a projection and the 3-D view share one contrast -------------
{
    const seen = {};
    for (const [label, state] of [["plane", PLANE], ["projection", SLAB], ["3-D view", VOLUME]]) {
        const { sidebar, asked, fits, applied } = page({ state });
        await sidebar.autoChannel(0);
        assert.deepEqual(asked, [], `the ${label} asked for statistics of its own`);
        assert.deepEqual(fits, ["tdTomato"], `the ${label} did not use the channel's fit`);
        // The channel's provisional level, then its fit -- the same everywhere.
        assert.deepEqual(applied, [[0, [42, 73]], [0, [50, 70]]],
                         `${label}: ${JSON.stringify(applied)}`);
        seen[label] = JSON.stringify(applied);
        // A channel the user set is left alone in every view.
        await sidebar.autoChannel(1);
        assert.equal(applied.length, 2, `the ${label} levelled a channel the user set`);
    }
    assert.equal(new Set(Object.values(seen)).size, 1);
    console.log("a plane, a projection and the 3-D view share the channel's one contrast, "
                + "with no request of their own");
}

// -- 5. nothing levels a channel again because the view changed ---------------
{
    const renderer = await readFile(
        path.join(repoRoot, "plexora/client/src/js/views/volumeRenderer.js"), "utf8");
    // Code, not prose: a comment may say why these are gone.
    const code = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.ok(!/registerSurface\(/.test(code(source)),
              "the sidebar holds navigation revisions for contrast again");
    for (const [name, text] of [["viewerSidebar.js", source], ["volumeRenderer.js", renderer]]) {
        for (const gone of ["plexora:volume-hint", "plexora:view-requested", "X-Plexora-Hint", "/hint?"]) {
            assert.ok(!code(text).includes(gone), `${name} still uses ${gone}`);
        }
    }
    console.log("nothing re-levels a channel when the view changes");
}
