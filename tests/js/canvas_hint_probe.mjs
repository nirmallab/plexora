/**
 * The quiet key hint at the bottom of the image: services/canvasHint.js.
 *
 * One hint for the page, shared by the ROI and QC tools:
 *
 *   - `show` mounts it once in the viewer's wrapper, as the caption's T hint
 *     is drawn (a <kbd> cap and a few words), never taking the pointer;
 *   - a second owner's `show` takes it over, and `hide` from anybody but the
 *     owner is ignored -- a tool switching off never takes away the hint the
 *     next tool has just put up;
 *   - with no viewer on the page there is nothing to show.
 *
 * Run directly: `node tests/js/canvas_hint_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "client", "src", "js", "services", "canvasHint.js");

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), className: "", children: [], parentNode: null,
        attributes: {}, hidden: false, textContent: "",
        setAttribute(key, value) { node.attributes[key] = String(value); },
        getAttribute(key) { return node.attributes[key] ?? null; },
        append(...kids) { for (const kid of kids) node.appendChild(kid); },
        appendChild(child) {
            child.parentNode?.children && (child.parentNode.children =
                child.parentNode.children.filter((c) => c !== child));
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
    };
    return node;
}

const has = (node, name) => String(node.className).split(/\s+/).includes(name);
const wrapper = element("div");
let wrapperPresent = true;
const document = {
    createElement: (tag) => element(tag),
    getElementById: (id) => (id === "openseadragon_wrapper" && wrapperPresent ? wrapper : null),
};
const window = {};
runInContext(readFileSync(SOURCE, "utf8"), createContext({ window, document, console }),
             { filename: "canvasHint.js" });
const Hint = window.PlexoraCanvasHint;
const hints = () => wrapper.children.filter((n) => has(n, "plx-canvas-hint"));

const failures = [];
function check(name, fn) {
    try {
        fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

const roi = { name: "roi" };
const qc = { name: "qc" };

check("show mounts one hint in the viewer's wrapper: a key cap and its words", () => {
    assert.equal(Hint.show(roi, { key: "Space", text: "Hold to pan" }), true);
    assert.equal(hints().length, 1);
    const [root] = hints();
    assert.ok(has(root, "viewer-overlay-hint"), "drawn as the caption's T hint is");
    assert.equal(root.hidden, false);
    assert.equal(root.getAttribute("aria-hidden"), "true");
    const [cap, words] = root.children;
    assert.equal(cap.tagName, "KBD");
    assert.equal(cap.textContent, "Space");
    assert.equal(words.textContent, "Hold to pan");
    assert.equal(Hint.isShown(roi), true);
});

check("a second owner takes it over, and the first one's hide leaves it up", () => {
    Hint.show(qc, { key: "Space", text: "Hold to pan" });
    assert.equal(hints().length, 1);
    Hint.hide(roi);
    assert.equal(hints()[0].hidden, false);
    assert.equal(Hint.isShown(qc), true);
    assert.equal(Hint.isShown(roi), false);
});

check("the owner's hide takes it off the image; shown again it is the same one", () => {
    Hint.hide(qc);
    assert.equal(hints()[0].hidden, true);
    assert.equal(Hint.isShown(), false);
    Hint.show(roi, {});
    assert.equal(hints().length, 1);
    assert.equal(hints()[0].hidden, false);
});

check("with no viewer on the page there is nothing to show", () => {
    Hint.hide(roi);
    wrapperPresent = false;
    assert.equal(Hint.show(roi, {}), false);
    wrapperPresent = true;
});

if (failures.length) process.exit(1);
