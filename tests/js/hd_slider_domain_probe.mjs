/**
 * What the HD contrast slider uses as its upper bound.
 *
 * In HD mode the slider works in raw 16-bit units, so its domain has to reach
 * the brightest pixel the HD tiles actually contain. getRawImageRange() used to
 * take that ceiling from `image_max`, which the server derives from `zarray` --
 * the mean-pooled overview. Pooling dilutes single/few-pixel peaks, so
 * image_max lands far below the real maximum (observed: 1313 on a channel whose
 * full-resolution max was much higher). The slider could then not be moved
 * above 1313, and every raw value above it clamped to full brightness in
 * frag.glsl's range_clamp.
 *
 * The fix reads `qmax` instead -- the same packet's full-resolution ceiling.
 *
 * This probe extracts the real methods from viewerSidebar.js and runs them
 * against stand-ins, alongside the same methods with the fix taken back out --
 * so the before/after difference is measured on shipped code rather than on a
 * reimplementation that could agree with itself while the app is wrong.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const relPath = "plexora/client/src/js/views/viewerSidebar.js";

/**
 * Slice one method out of a class body by brace matching.
 *
 * Tracks line/block comments and quotes so a brace inside prose or a string
 * cannot end the match early -- these methods sit under long explanatory
 * comment blocks, which is exactly where a naive scanner goes wrong.
 */
function extractMethod(source, name) {
    const signature = new RegExp(`\\n    ${name}\\(`).exec(source);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    let i = source.indexOf("{", start);
    if (i < 0) throw new Error(`no body for ${name}`);

    let depth = 0;
    let state = "code";
    for (; i < source.length; i++) {
        const c = source[i];
        const next = source[i + 1];
        if (state === "line") {
            if (c === "\n") state = "code";
            continue;
        }
        if (state === "block") {
            if (c === "*" && next === "/") { state = "code"; i++; }
            continue;
        }
        if (c === "/" && next === "/") { state = "line"; i++; continue; }
        if (c === "/" && next === "*") { state = "block"; i++; continue; }
        if (c === '"' || c === "'" || c === "`") {
            const quoteChar = c;
            i++;
            for (; i < source.length; i++) {
                if (source[i] === "\\") { i++; continue; }
                if (source[i] === quoteChar) break;
            }
            continue;
        }
        if (c === "{") depth++;
        else if (c === "}") {
            depth--;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`unterminated body for ${name}`);
}

const METHODS = ["getRawImageRange", "getImageRange", "byteToRawRange"];

function buildSidebar(source) {
    const body = METHODS.map((name) => extractMethod(source, name)).join(",\n");
    // Object-method shorthand is the same grammar as a class method body, so
    // the extracted text drops straight in with no rewriting.
    const methods = new Function(`return {\n${body}\n};`)();
    return function sidebar({ desc = {}, hd = true } = {}) {
        return {
            ...methods,
            databaseDescription: { CD45: desc },
            dataLayer: {
                getFullChannelName: (n) => n,
                imageBitRange: [0, 65536],
            },
            isHdMode: () => hd,
        };
    };
}

const afterSource = await readFile(path.join(repoRoot, relPath), "utf8");

/**
 * The whole fix, as it appears in getRawImageRange. Removing it puts the
 * ceiling back on the pooled image_max, which is the bug.
 *
 * "before" used to be `git show HEAD:<file>`, and that worked exactly once. The
 * day the fix was committed, before and after became the same source and check
 * 1 below started failing -- a green test going red BECAUSE the code it guards
 * had shipped, which is the one failure mode a regression test must not have.
 * Reconstructing the old behaviour by mutating current source is what
 * tests/test_tool_cards.py does, for the same reason: it keeps working for as
 * long as the line exists, and says so plainly when it stops existing.
 */
const THE_FIX = "desc.qmax || ";
assert.ok(
    afterSource.split(THE_FIX).length === 2,
    `expected exactly one \`${THE_FIX}\` in ${relPath} to take back out -- `
    + "getRawImageRange's ceiling has been rewritten, so this probe no longer "
    + "reconstructs the pooled-max bug it exists to pin",
);
const beforeSource = afterSource.replace(THE_FIX, "");

const before = buildSidebar(beforeSource);
const after = buildSidebar(afterSource);

// A channel as it stands once get_image_channel_stats has landed. image_max is
// the pooled overview's maximum; qmax is the full-resolution maximum. The gap
// between them is the bug.
const REAL_STATS = { image_min: 1, image_max: 1313, qmin: 0, qmax: 17500 };

// -- 1. the reported symptom -------------------------------------------------
{
    const b = before({ desc: REAL_STATS }).getImageRange("CD45");
    const a = after({ desc: REAL_STATS }).getImageRange("CD45");
    assert.equal(b[1], 1313, "before: ceiling came from the pooled image_max");
    assert.equal(a[1], 17500, "after: ceiling comes from the full-res qmax");
    console.log(`the HD slider ceiling is the full-resolution max, not the pooled one (${b[1]} -> ${a[1]})`);
}

// -- 2. toggling HD can no longer strand the upper handle ---------------------
// A fully-open channel in default mode is [0, 255]; onHdModeChanged converts it
// with byteToRawRange, which maps byte 255 onto exactly qmax. If the slider's
// own maximum is below that, the handle lands outside its domain.
{
    const raw = after().byteToRawRange([0, 255], REAL_STATS);
    assert.equal(raw[1], 17500, "byte 255 maps onto qmax");

    const b = before({ desc: REAL_STATS }).getImageRange("CD45");
    const a = after({ desc: REAL_STATS }).getImageRange("CD45");
    assert.ok(raw[1] > b[1], "before: a fully-open channel exceeded the slider maximum");
    assert.ok(raw[1] <= a[1], "after: the converted range fits inside the slider domain");
    console.log("toggling HD on a fully-open channel keeps the upper handle inside the domain");
}

// -- 3. nothing changes before the stats arrive ------------------------------
// getImageRange is called at slot construction, long before the lazy
// get_image_channel_stats fetch resolves. Both versions must fall back to the
// generic bit range rather than gating activation on the fetch.
{
    const b = before({ desc: {} }).getImageRange("CD45");
    const a = after({ desc: {} }).getImageRange("CD45");
    assert.deepEqual(a, b, "unfetched channels behave identically");
    assert.deepEqual(a, [0, 65536], "and fall back to the generic 16-bit range");
    console.log("a channel whose stats have not been fetched is unchanged");
}

// -- 4. default (non-HD) mode is untouched -----------------------------------
{
    const b = before({ desc: REAL_STATS, hd: false }).getImageRange("CD45");
    const a = after({ desc: REAL_STATS, hd: false }).getImageRange("CD45");
    assert.deepEqual(a, b, "default mode is unaffected");
    assert.deepEqual(a, [0, 255], "and stays in the byte domain the WebP path quantizes into");
    console.log("default mode still uses the fixed [0, 255] byte domain");
}

// -- 5. a packet without qmax degrades to the old answer, not to 65536 -------
// hasChannelGMM entries carry qmax too, but guard the ordering anyway: losing
// the ceiling entirely would blow the slider domain out by ~50x.
{
    const partial = { image_min: 1, image_max: 1313 };
    const b = before({ desc: partial }).getImageRange("CD45");
    const a = after({ desc: partial }).getImageRange("CD45");
    assert.deepEqual(a, b, "without qmax the fallback is the previous behavior");
    assert.equal(a[1], 1313, "not the generic bit range");
    console.log("a packet with no qmax falls back to the previous ceiling");
}

// -- 6. an instance can pin the mode instead of asking the viewer ------------
// `isHdMode` reads the OSD viewer manager, which exists only on the viewer
// page. A ViewerSidebar mounted anywhere else -- Figure Builder's Quick Edit --
// therefore got "no" forever and its sliders were stuck in the byte domain,
// quantizing a 16-bit window to 256 steps on the way in and again on the way
// out. The override is what lets that instance say what it knows.
{
    const listeners = [];
    const context = createContext({
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, parseFloat, parseInt, isNaN, isFinite,
        document: { getElementById: () => null, querySelector: () => null },
        // No `__plexora`, which is the situation being described: there is no
        // viewer on the page a scoped sidebar is mounted on.
        window: {
            addEventListener: (type) => listeners.push(type),
            removeEventListener() {},
        },
    });
    context.globalThis = context;
    runInContext(afterSource, context, { filename: relPath });
    const Sidebar = runInContext("ViewerSidebar", context);

    const build = (options) => {
        listeners.length = 0;
        const sidebar = new Sidebar({}, [], {}, {}, {}, options);
        sidebar.databaseDescription = { CD45: REAL_STATS };
        sidebar.dataLayer = { getFullChannelName: (n) => n, imageBitRange: [0, 65536] };
        return { sidebar, listeners: [...listeners] };
    };

    const pinned = build({ hdMode: true });
    const asking = build(undefined);

    assert.equal(pinned.sidebar.isHdMode(), true, "a pinned instance answers for itself");
    assert.equal(asking.sidebar.isHdMode(), false,
        "an unpinned instance still asks the viewer, which is absent here");
    // A pinned mode cannot change, and the listener outlives the instance --
    // there is no removeEventListener call anywhere, and a scoped sidebar is
    // rebuilt every time its host reopens, so each one would leave another
    // remapper behind holding a reference to a dead widget.
    assert.ok(!pinned.listeners.includes("plexora:hd-mode-changed"),
        "a pinned instance does not listen for a mode change it cannot have");
    assert.ok(asking.listeners.includes("plexora:hd-mode-changed"),
        "an unpinned instance still does");

    // And the whole point: the slider domain follows. Spread first -- these
    // arrays are built inside the vm realm, so their prototype is not this
    // one's and a strict deep-equal compares that too.
    assert.deepEqual([...pinned.sidebar.getImageRange("CD45")], [1, 17500],
        "a pinned instance gets the raw domain");
    assert.deepEqual([...asking.sidebar.getImageRange("CD45")], [0, 255],
        "and an unpinned one off the viewer page is still stuck in bytes");
    console.log("an instance with no viewer to ask can pin HD mode for itself");
}

console.log("\nall HD slider domain checks passed");
