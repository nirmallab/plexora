/**
 * The viewer's auto-contrast poll (viewerSidebar.js `pollGatingContrast`,
 * tests/test_gating_poll.py): a channel switched on while the poll sleeps
 * is asked for at once -- the poll is woken and its backoff starts again --
 * not at the end of a backoff grown to seconds. On a large slide, whose
 * first calibration is still running when channels are switched on, that
 * wait was most of what the final contrast took to land.
 *
 * The method is sliced out of the shipped file and run against stand-ins.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const sidebarSource = await readFile(path.join(here, "..", "..", "plexora/client/src/js/views/viewerSidebar.js"), "utf8");

/** One method's text, by brace matching that skips comments and strings. */
function extractMethod(text, name) {
    const signature = new RegExp(`\\n    (?:async )?${name}\\(`).exec(text);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
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

const context = createContext({
    window: { setTimeout }, Promise, Date, Math, Set, Array,
    GATING_POLL_FIRST_MS: 400, GATING_POLL_LIMIT_MS: 10000,
});
const SIDEBAR = runInContext(`({\n${extractMethod(sidebarSource, "pollGatingContrast")}\n})`, context);

const t0 = Date.now();
const asked = [];
const states = { computing: true };
const bar = Object.assign(Object.create(SIDEBAR), {
    gatingContrast: { channels: {}, calibrated: [] },
    on: ["DNA"],
    enabledChannelNames() { return this.on; },
    async fetchGatingContrast() { return { state: states.computing ? "computing" : "ready", channels: {}, calibrated: [] }; },
    async computeGatingContrast(names) { asked.push({ names, ms: Date.now() - t0 }); return { state: "computing" }; },
    syncGatingContrast() {},
});

const polling = bar.pollGatingContrast();
await new Promise((resolve) => setTimeout(resolve, 100));
// Switched on while the poll sleeps (its first wait is 400 ms here).
bar.on = ["DNA", "CD8"];
await bar.pollGatingContrast();
await new Promise((resolve) => setTimeout(resolve, 60));
states.computing = false;
await polling;

assert.deepEqual(asked.map((a) => a.names), [["DNA"], ["CD8"]]);
assert.ok(asked[1].ms < 300, `CD8 was asked for after ${asked[1].ms} ms: the poll was not woken`);
console.log("a channel switched on while the poll sleeps is asked for at once");
