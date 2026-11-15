/**
 * Reordering channel rows while one is auto-levelling or easing.
 *
 * Rows stay where they are and their CONTENTS move (applySlotOrder), but a
 * levelling or an ease in flight holds the slot OBJECT it started on. Copied
 * with the rest, `autoLeveling` / `rangeEase` landed on another row that no
 * run would ever clear: that row read as levelling forever, and Auto on it
 * did nothing. A moved row now leaves its in-flight state behind and its
 * work is taken up again at the new index; a row that did not move keeps it.
 *
 * applySlotOrder is extracted from viewerSidebar.js and run as written.
 *
 * Run directly:  node tests/js/slot_order_inflight_probe.mjs
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(
    path.join(here, "..", "..", "plexora/client/src/js/views/viewerSidebar.js"), "utf8");

/** One method's text, by brace matching that skips comments and strings. */
function extractMethod(name) {
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

const failures = [];
function check(label, ok, detail = "") {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}${ok || !detail ? "" : `  ${detail}`}`);
    if (!ok) failures.push(label);
}

globalThis.ChannelList = { events: { CHANNELS_CHANGE: "CHANNELS_CHANGE" } };
const methods = new Function(`return {\n${extractMethod("applySlotOrder")}\n};`)();

function sidebar(slots) {
    const calls = { level: [], range: [] };
    const self = Object.assign(Object.create(methods), {
        channelSlots: slots.map((slot, index) => ({ index, enabled: true, ...slot })),
        selectedSlot: 0,
        review: null,
        channelList: { selections: [] },
        eventHandler: { trigger() {} },
        el: () => null,
        slotRow: () => null,
        syncSlotDom() {},
        syncContrastBar() {},
        scheduleSaveChannels() {},
        syncChannelSlider() {},
        levelChannel(index) { calls.level.push(index); return Promise.resolve(); },
        setSlotRange(index, range) { calls.range.push([index, range]); },
    });
    return { self, calls };
}

{
    const target = [10, 200];
    const { self, calls } = sidebar([
        { name: "CD8", autoLeveling: true, levelTurn: 3 },
        { name: "CD4", rangeEase: {}, rangeEaseTarget: target, range: [0, 50] },
        { name: "DAPI", autoLeveling: true, levelTurn: 1 },
    ]);
    self.applySlotOrder([1, 0, 2]);
    const [first, second, third] = self.channelSlots;
    check("the rows' contents moved", first.name === "CD4" && second.name === "CD8");
    check("a levelling row moved elsewhere is not left 'levelling' at its new place",
        !second.autoLeveling, JSON.stringify(second));
    check("...and its levelling is taken up again there", calls.level.includes(1),
        JSON.stringify(calls.level));
    check("an easing row moved elsewhere carries no ease token", !first.rangeEase && !first.rangeEaseTarget,
        JSON.stringify(first));
    check("...and lands on the window the ease was heading to",
        calls.range.some(([index, range]) => index === 0 && range === target), JSON.stringify(calls.range));
    check("a row that did not move keeps its levelling in flight",
        third.autoLeveling === true && third.levelTurn === 1 && !calls.level.includes(2));
}

{
    // Already levelled, then re-fitting (a refit): the window it has stands;
    // nothing is started again.
    const { self, calls } = sidebar([
        { name: "CD8", autoLeveling: true, autoLeveled: true },
        { name: "CD4" },
    ]);
    self.applySlotOrder([1, 0]);
    check("a moved row that was already levelled is not levelled again",
        !self.channelSlots[1].autoLeveling && calls.level.length === 0, JSON.stringify(calls.level));
}

console.log(failures.length ? `\nFAILURES: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
