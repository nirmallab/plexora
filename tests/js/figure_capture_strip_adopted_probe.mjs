/**
 * A capture adopted into a figure leaves the viewer's strip.
 *
 * Adopting takes a capture OUT of the bin on the server. When the viewer page
 * read the bin back after the round trip to the canvas, every capture taken in
 * this session ("fresh") that the answer did not list was kept, on the theory
 * that its write was still in flight -- so adopted captures stayed in the strip,
 * ticked and outlined, ready to be added to a figure a second time. A fresh
 * capture whose write has COMPLETED and that the bin no longer lists has left
 * the bin; only one still being written, or refused, is held.
 *
 * Run directly:
 *   node tests/js/figure_capture_strip_adopted_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

const problems = [];
function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, parseInt, isNaN, RegExp,
    document: { readyState: "complete", addEventListener() {}, removeEventListener() {},
                getElementById: () => null },
    window: { addEventListener() {}, removeEventListener() {},
              Plexora: { registerPlugin() {} } },
});
runInContext(readFileSync(join(STATIC, "figureSidebarController.js"), "utf8"), ctx,
             { filename: "figureSidebarController.js" });

const capture = (id, extra) => Object.assign({
    id, datasource: "demo", scene: { viewport: { x: 0, y: 0, w: 1, h: 1 } },
    source: null, caption: "", url: null, objectUrl: false,
    checked: true, fresh: true, unsaved: false }, extra);

ctx.__captures = [
    // Taken, written, then adopted into a figure: gone from the bin.
    capture("cap_adopted"),
    // Taken, its write still in flight: not in the bin's answer YET.
    capture("cap_writing"),
    // Taken, its write refused: this page is all that holds it.
    capture("cap_refused", { unsaved: true }),
    // Taken and still in the bin.
    capture("cap_kept"),
];

const kept = await runInContext(`(async () => {
    const page = Object.create(FigureBuilderSidebarController.prototype);
    page.datasource = "demo";
    page.captures = __captures;
    page.selected = null;
    page.render = () => {};
    page.fail = () => {};
    page.deselect = () => {};
    page.bin = { list: async () => [{ capture_id: "cap_kept", scene: {} }] };
    // What \`keep\` leaves behind once the bin has the row.
    for (const entry of page.captures) {
        if (entry.id === "cap_adopted" || entry.id === "cap_kept") entry.stored = true;
    }
    await page._readBin();
    return page.captures.map((entry) => entry.id);
})()`, ctx);

check("an adopted capture leaves the strip; one still being written or refused stays",
    kept, ["cap_writing", "cap_refused", "cap_kept"]);

// And \`keep\` is what marks a capture as written.
const source = readFileSync(join(STATIC, "figureSidebarController.js"), "utf8");
check("keep() marks a capture whose write landed",
    /capture\.stored = true;/.test(source), true);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
