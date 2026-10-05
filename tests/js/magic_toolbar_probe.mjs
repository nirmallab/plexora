/**
 * Magic select's floating bar: services/magicToolbar.js.
 *
 * One bar for the page, shared by the ROI and QC tools, so what is worth
 * pinning is the sharing and the accessibility contract:
 *
 *   - it mounts once, in the viewer's wrapper, as a toolbar with three
 *     radios (Add / Remove / Box) and a close button;
 *   - the lit mode is the one aria-checked, and busy is aria-busy on the root;
 *   - a click on a mode or the × reports to the owner and never reaches the
 *     image under it;
 *   - a second tool's `show` takes the bar over -- the first tool's stale
 *     handle can neither repaint it nor hide it -- and `hide` only works for
 *     the owner that showed it;
 *   - nothing on it names the model.
 *
 * The real script in a vm realm with a hand-built document.
 *
 * Run directly: `node tests/js/magic_toolbar_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "client", "src", "js", "services", "magicToolbar.js");

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), className: "", children: [], parentNode: null,
        attributes: {}, dataset: {}, listeners: {}, hidden: false, title: "", type: "",
        setAttribute(key, value) { node.attributes[key] = String(value); },
        getAttribute(key) { return node.attributes[key] ?? null; },
        appendChild(child) {
            child.parentNode?.removeChild(child);
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        removeChild(child) {
            node.children = node.children.filter((c) => c !== child);
            child.parentNode = null;
            return child;
        },
        addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
        /** A click bubbling from here up: stops where a listener stops it. */
        click() {
            const event = { type: "click", stopped: false,
                            stopPropagation() { event.stopped = true; } };
            for (let n = node; n && !event.stopped; n = n.parentNode) {
                for (const fn of n.listeners.click || []) fn(event);
            }
            return event;
        },
    };
    return node;
}

function all(node) {
    return [node, ...node.children.flatMap(all)];
}

function has(node, name) {
    return String(node.className).split(/\s+/).includes(name);
}

const wrapper = element("div");
let wrapperPresent = true;
const imageClicks = [];
wrapper.addEventListener("click", () => imageClicks.push("image"));
const document = {
    createElement: (tag) => element(tag),
    getElementById: (id) => (id === "openseadragon_wrapper" && wrapperPresent ? wrapper : null),
    querySelector: () => null,
};
const window = {};
const context = createContext({ window, document, console });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "magicToolbar.js" });
const Bar = window.PlexoraMagicBar;

const bars = () => wrapper.children.filter((n) => has(n, "plx-magic-bar"));
const radios = (root) => all(root).filter((n) => n.getAttribute("role") === "radio");
const checked = (root) => radios(root).filter((n) => n.getAttribute("aria-checked") === "true")
    .map((n) => n.dataset.mode);
const closeButton = (root) => all(root).find((n) => has(n, "plx-magic-bar-close"));

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

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

const ownerA = { name: "roi" };
const ownerB = { name: "qc" };
const heardA = [];
const heardB = [];
let handleA = null;
let handleB = null;

check("MODES are add, remove, box and scribble", () => {
    same(Bar.MODES, ["add", "remove", "box", "scribble"]);
});

check("show mounts one toolbar in the viewer's wrapper, Add lit", () => {
    handleA = Bar.show({ owner: ownerA, onMode: (m) => heardA.push(["mode", m]),
                         onClose: () => heardA.push(["close"]) });
    assert.ok(handleA);
    assert.equal(bars().length, 1);
    const [root] = bars();
    assert.equal(root.tagName, "DIV");
    assert.equal(root.getAttribute("role"), "toolbar");
    assert.equal(root.hidden, false);
    same(radios(root).map((n) => n.dataset.mode), ["add", "remove", "box", "scribble"]);
    same(checked(root), ["add"]);
    assert.ok(closeButton(root));
    assert.equal(handleA.isShown(), true);
});

check("a mode or the close button reports to the owner and never reaches the image", () => {
    const [root] = bars();
    const remove = radios(root).find((n) => n.dataset.mode === "remove");
    assert.equal(remove.click().stopped, true);
    closeButton(root).click();
    same(heardA, [["mode", "remove"], ["close"]]);
    same(imageClicks, []);
});

check("setMode lights one radio; setBusy is aria-busy on the root", () => {
    const [root] = bars();
    handleA.setMode("box");
    same(checked(root), ["box"]);
    handleA.setBusy(true);
    assert.equal(root.getAttribute("aria-busy"), "true");
    handleA.setBusy(false);
    assert.equal(root.getAttribute("aria-busy"), "false");
});

check("a second owner's show takes the bar over rather than stacking", () => {
    handleB = Bar.show({ owner: ownerB, mode: "remove",
                         onMode: (m) => heardB.push(["mode", m]),
                         onClose: () => heardB.push(["close"]) });
    assert.equal(bars().length, 1);
    const [root] = bars();
    same(checked(root), ["remove"]);
    assert.equal(handleB.isShown(), true);
    assert.equal(handleA.isShown(), false);
    radios(root).find((n) => n.dataset.mode === "add").click();
    same(heardB, [["mode", "add"]]);
    same(heardA, [["mode", "remove"], ["close"]]);
});

check("...and the first owner's stale handle can neither repaint nor hide it", () => {
    const [root] = bars();
    handleA.setMode("box");
    handleA.setBusy(true);
    same(checked(root), ["remove"]);
    assert.equal(root.getAttribute("aria-busy"), "false");
    handleA.hide();
    Bar.hide(ownerA);
    assert.equal(bars().length, 1);
    assert.equal(handleB.isShown(), true);
});

check("hide by the owner takes it off the image", () => {
    const [root] = bars();
    handleB.hide();
    assert.equal(bars().length, 0);
    assert.equal(root.hidden, true);
    assert.equal(handleB.isShown(), false);
    radios(root)[0].click();
    same(heardB, [["mode", "add"]]);
});

check("shown again, it is the same one bar, back in the wrapper", () => {
    const again = Bar.show({ owner: ownerA, mode: "add" });
    assert.equal(bars().length, 1);
    assert.equal(again.isShown(), true);
    Bar.hide(ownerA);
    assert.equal(bars().length, 0);
});

check("with no viewer on the page there is no bar", () => {
    wrapperPresent = false;
    try {
        assert.equal(Bar.show({ owner: ownerA }), null);
    } finally {
        wrapperPresent = true;
    }
});

check("nothing on it names the model", () => {
    Bar.show({ owner: ownerA });
    const words = all(bars()[0]).flatMap((n) => [n.title, n.getAttribute("aria-label")])
        .filter(Boolean).join(" | ");
    assert.ok(words.includes("Magic select"), words);
    assert.ok(!words.includes("SAM") && !/segment anything/i.test(words), words);
    Bar.hide(ownerA);
});

process.exit(failures.length ? 1 : 0);
