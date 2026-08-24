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
 * This probe extracts the real methods from viewerSidebar.js AND from the same
 * file at git HEAD, then runs both against identical stand-ins, so the
 * before/after difference is measured on shipped code rather than on a
 * reimplementation that could agree with itself while the app is wrong.
 */

import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
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
const beforeSource = execFileSync("git", ["show", `HEAD:${relPath}`], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
});

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

console.log("\nall HD slider domain checks passed");
