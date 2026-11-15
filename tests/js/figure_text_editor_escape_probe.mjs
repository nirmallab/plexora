/**
 * Escape in the caption editor means the same thing in every browser.
 *
 * It used to call `cancel()`, which removes the editor element, and leave the
 * rest to the browser: Chromium fires `blur` on a focused element that is
 * removed, so the blur handler committed the caption; Firefox fires nothing,
 * so the typed text was discarded and a fresh text box was left on the page as
 * an empty ghost. Escape now ends the edit by blurring, which every engine
 * reports, so it commits exactly once whichever engine runs it.
 *
 * Run directly:
 *   node tests/js/figure_text_editor_escape_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["../../../client/src/js/services/viewerScene.js", "figureSchema.js",
                 "figureRichText.js", "figureTextEditor.js"];

const problems = [];
function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, parseInt, isNaN, RegExp,
    document: { readyState: "complete", activeElement: null,
                addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
});
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

/** Escape pressed in an open editor, in an engine that does (Chromium) or does
 *  not (Firefox) fire blur when a focused element is removed. */
function escapeIn(blurOnRemoval) {
    return runInContext(`(() => {
        const commits = [];
        const editor = new FigureTextEditor({
            overlayEl: null, canvas: { render() {}, surfaceEl: { querySelector: () => null } },
            state: { document: { annotations: { ann_1: { type: "text", text: "" } } } },
        });
        let focused = true;
        const el = {
            blur() { if (focused) { focused = false; editor.commit(); } },
            remove() { if (${blurOnRemoval} && focused) { focused = false; editor.commit(); } },
        };
        editor.overlayEl = { querySelectorAll: () => [el] };
        editor.el = el;
        editor.annotationId = "ann_1";
        // What commit does is not the question here, only whether it runs.
        editor.commit = () => { commits.push(editor.annotationId); editor.close(true); };
        editor.keyDown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
        return { commits: commits.length, open: editor.active };
    })()`, ctx);
}

check("Chromium: Escape commits the caption once", escapeIn(true), { commits: 1, open: false });
check("Firefox: Escape commits it too, rather than discarding it", escapeIn(false),
      { commits: 1, open: false });

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
