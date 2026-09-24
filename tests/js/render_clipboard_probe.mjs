/**
 * The Image card's clipboard: what a copy keeps and what a paste would do.
 *
 * Two things fail quietly here and are worth fencing. The storage: two slots
 * that must not overwrite each other, and a blocked sessionStorage that must
 * read as "nothing copied" rather than throw out of a menu click. And the two
 * planners, which decide every paste: a merged name list that holds the same
 * name twice is one the server refuses outright, and a rendering matched by
 * position when the names agree would paint CD3's window onto DAPI.
 *
 * Run directly: `node tests/js/render_clipboard_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/renderClipboard.js");

function fakeStorage() {
    const data = new Map();
    return {
        getItem: (key) => (data.has(key) ? data.get(key) : null),
        setItem: (key, value) => data.set(key, String(value)),
        removeItem: (key) => data.delete(key),
        data,
    };
}

function boot({ storage = fakeStorage(), throws = false } = {}) {
    const context = { JSON, Date, Math, Number, String, Array, Set, Map, Object, Boolean };
    context.window = context;
    if (throws) {
        Object.defineProperty(context, "sessionStorage", {
            get() { throw new Error("SecurityError"); },
        });
    } else {
        context.sessionStorage = storage;
    }
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return { clip: context.PlexoraRenderClipboard, storage };
}

/** Plain data out of the vm, so deepEqual compares values, not realms. */
const plain = (value) => JSON.parse(JSON.stringify(value));

const passed = [];
function check(label, fn) {
    fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

// ------------------------------------------------------------- storage

check("nothing is copied until something is", () => {
    const { clip } = boot();
    assert.equal(clip.hasNames(), false);
    assert.equal(clip.hasRendering(), false);
    assert.equal(clip.names(), null);
    assert.equal(clip.rendering(), null);
});

check("the two slots round-trip independently", () => {
    const { clip } = boot();
    assert.equal(clip.copyNames(["DAPI", "CD3"], "a"), true);
    assert.equal(clip.copyRendering({ opacity: 0.5, hd: true, slots: [{ name: "DAPI" }] }, "a"), true);
    assert.deepEqual(plain(clip.names().names), ["DAPI", "CD3"]);
    assert.equal(clip.rendering().opacity, 0.5);
    assert.equal(clip.rendering().hd, true);
    // Copying one again leaves the other where it was.
    clip.copyNames(["X"], "b");
    assert.equal(clip.hasRendering(), true);
    assert.deepEqual(plain(clip.names().names), ["X"]);
});

check("it survives a page load in the same tab", () => {
    const storage = fakeStorage();
    boot({ storage }).clip.copyNames(["DAPI"], "a");
    const again = boot({ storage }).clip;
    assert.deepEqual(plain(again.names().names), ["DAPI"]);
});

check("a document from another version reads as empty", () => {
    const storage = fakeStorage();
    storage.setItem("plexora:clipboard", JSON.stringify({ version: 99, names: { names: ["A"] } }));
    const { clip } = boot({ storage });
    assert.equal(clip.hasNames(), false);
    storage.setItem("plexora:clipboard", "{not json");
    assert.equal(boot({ storage }).clip.hasNames(), false);
});

check("blocked storage is nothing copied, not an exception", () => {
    const { clip } = boot({ throws: true });
    assert.equal(clip.hasNames(), false);
    assert.equal(clip.hasRendering(), false);
    assert.equal(clip.copyNames(["A"]), false);
    assert.equal(clip.copyRendering({ slots: [{ name: "A" }] }), false);
});

check("an empty copy is refused rather than stored", () => {
    const { clip } = boot();
    assert.equal(clip.copyNames([]), false);
    assert.equal(clip.copyRendering({ slots: [] }), false);
    assert.equal(clip.hasNames() || clip.hasRendering(), false);
});

// ---------------------------------------------------------- mergeNames

const merge = (a, b) => plain(boot().clip.mergeNames(a, b));

check("the same length renames every position", () => {
    const plan = merge(["DAPI", "CD3"], ["Channel_0", "Channel_1"]);
    assert.deepEqual(plan.names, ["DAPI", "CD3"]);
    assert.equal(plan.applied, 2);
    assert.equal(plan.total, 2);
    assert.equal(plan.changed, true);
});

check("a shorter copy renames the overlap and keeps the rest", () => {
    const plan = merge(["DAPI", "CD3"], ["c0", "c1", "c2"]);
    assert.deepEqual(plan.names, ["DAPI", "CD3", "c2"]);
    assert.equal(plan.applied, 2);
    assert.equal(plan.total, 3);
    assert.equal(plan.skipped, 0);
});

check("a longer copy uses what fits", () => {
    const plan = merge(["DAPI", "CD3", "CD8"], ["c0", "c1"]);
    assert.deepEqual(plan.names, ["DAPI", "CD3"]);
    assert.equal(plan.applied, 2);
    assert.equal(plan.total, 3);
});

check("an identical list changes nothing", () => {
    const plan = merge(["A", "B"], ["A", "B"]);
    assert.equal(plan.changed, false);
    assert.equal(plan.applied, 2);
});

check("a blank copied name keeps the image's own", () => {
    const plan = merge(["DAPI", "  "], ["c0", "c1"]);
    assert.deepEqual(plan.names, ["DAPI", "c1"]);
    assert.equal(plan.applied, 1);
    assert.equal(plan.skipped, 1);
});

check("a name the image keeps elsewhere is skipped", () => {
    const plan = merge(["A", "B", "C"], ["X", "Y", "Z", "A"]);
    assert.deepEqual(plan.names, ["X", "B", "C", "A"]);
    assert.equal(plan.applied, 2);
    assert.equal(plan.total, 4);
});

check("a repeated copied name is used once", () => {
    const plan = merge(["B", "B", "Z"], ["A", "B", "C"]);
    assert.deepEqual(plan.names, ["A", "B", "Z"]);
    assert.equal(plan.applied, 2);
    assert.equal(plan.total, 3);
});

check("a dropped rename that frees a collision is followed through", () => {
    // Dropping position 0's rename keeps "Q" there, which then collides
    // with position 2's candidate "Q" -- so that one drops too.
    const plan = merge(["C", "P", "Q"], ["Q", "B", "R", "C"]);
    assert.deepEqual(plan.names, ["Q", "P", "R", "C"]);
    assert.equal(new Set(plan.names).size, plan.names.length, "never a duplicate");
});

// -------------------------------------------------------- resolveSlots

const resolve = (slots, columns) => plain(boot().clip.resolveSlots(slots, columns));

check("slots match by name whatever order the image is in", () => {
    const plan = resolve(
        [{ index: 0, name: "DAPI", colorHex: "#0000ff", enabled: true, range: [10, 900] },
         { index: 1, name: "CD3", colorHex: "#00ff00", enabled: true, range: null }],
        ["CD3", "DAPI"]);
    assert.deepEqual(plan.entries, [
        { name: "DAPI", enabled: true, color: "#0000ff", range: [10, 900] },
        { name: "CD3", enabled: true, color: "#00ff00" },
    ]);
    assert.equal(plan.matched, 2);
});

check("a name the image lacks falls back to its position", () => {
    const plan = resolve([{ index: 1, name: "Channel_1", enabled: true }], ["DAPI", "CD3"]);
    assert.equal(plan.entries[0].name, "CD3");
});

check("a position past the image's channels is skipped", () => {
    const plan = resolve([{ index: 5, name: "Nope", enabled: true }], ["DAPI"]);
    assert.equal(plan.entries.length, 0);
    assert.equal(plan.skipped, 1);
});

check("a channel is used at most once", () => {
    const plan = resolve(
        [{ index: 0, name: "DAPI" }, { index: 0, name: "Other" }], ["DAPI", "CD3"]);
    assert.deepEqual(plan.entries.map((e) => e.name), ["DAPI"]);
    assert.equal(plan.skipped, 1);
});

check("a blank row is skipped and an off slot stays off", () => {
    const plan = resolve(
        [{ index: 0, name: "" }, { index: 1, name: "CD3", enabled: false, range: [1, 2] }],
        ["DAPI", "CD3"]);
    assert.deepEqual(plan.entries, [{ name: "CD3", enabled: false, range: [1, 2] }]);
    assert.equal(plan.skipped, 1);
});

check("a window that is not two numbers is dropped, not pasted", () => {
    const plan = resolve(
        [{ index: 0, name: "DAPI", range: [1, NaN] }, { index: 1, name: "CD3", range: [1] }],
        ["DAPI", "CD3"]);
    assert.equal("range" in plan.entries[0], false);
    assert.equal("range" in plan.entries[1], false);
});

console.log(`all checks passed (${passed.length})`);
