/**
 * Previous and Next: which sample each one goes to, and when neither does.
 *
 * The decisions worth fencing, all of which fail quietly:
 *
 *   - THE WALK ORDER IS THE DATASET'S OWN. `Dataset.projects` is the order
 *     somebody put the samples in. The Samples page sorts by "last opened" by
 *     default, and every open rewrites that key -- so a walk built on it would
 *     reshuffle underneath the user, and Next twice could land back where it
 *     started.
 *   - A MEMBER THIS PLEXORA CANNOT OPEN IS NOT A NEIGHBOUR. A dataset holds
 *     names, and a name outlives what it names (an unmounted shared root, a
 *     sample deleted in another tab). Offering one is a walk into a 404.
 *   - THE KEYS MUST NOT FIRE WHILE SOMEBODY IS TYPING, or with a dialog open.
 *     PageUp/PageDown are also ordinary scrolling keys.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/datasetNav.js");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}
async function checkAsync(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(),
        className: "",
        dataset: {},
        children: [],
        disabled: false,
        title: "",
        textContent: "",
        attributes: {},
        listeners: {},
        appendChild(child) { this.children.push(child); return child; },
        removeChild(child) {
            this.children = this.children.filter((c) => c !== child);
        },
        setAttribute(name, value) { this.attributes[name] = value; },
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
    };
    node.parentNode = null;
    return node;
}

/** Walk a rendered tree for the first node matching a predicate. */
function find(node, predicate) {
    if (!node) return null;
    if (predicate(node)) return node;
    for (const child of node.children || []) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
}

/**
 * @param datasets what GET /datasets answers.
 * @param known the projects this page was told exist. Defaults to every member.
 * @param wrapper false for a page with no canvas -- Settings, the Samples
 *   list. The control must not mount there.
 */
function boot({ datasets = [], here = "b", known = null, wrapper = true,
                activeTool = "", ok = true } = {}) {
    const mount = wrapper ? element("div") : null;
    const documentListeners = {};
    const navigated = [];
    const stashed = [];
    let dialogOpen = false;
    let activeElement = null;

    const members = datasets.flatMap((d) => d.projects || []);
    const context = {
        console: { log() {}, error() {}, warn() {} },
        JSON, Date, Set, Object, Array, Number, String, Boolean,
        encodeURIComponent,
        document: {
            readyState: "complete",
            getElementById: (id) => (id === "openseadragon_wrapper" ? mount : null),
            querySelector: (sel) => (sel === "dialog[open]" && dialogOpen ? {} : null),
            addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
            createElement: element,
            get activeElement() { return activeElement; },
        },
        fetch: async () => ({
            ok,
            json: async () => ({ success: true, datasets }),
        }),
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
    };
    context.window = context;
    context.addEventListener = () => {};
    context.flaskVariables = {
        datasource: here,
        datasources: known === null ? members : known,
    };
    context.PlexoraCarryOver = { stash: (to) => { stashed.push(to); return true; } };
    context.PlexoraToolLoader = { activeTool: () => activeTool };
    context.PlexoraRouter = { go: (href) => navigated.push(href) };
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return {
        api: context.PlexoraDatasetNav,
        mount, navigated, stashed, documentListeners,
        setTyping: (tag) => { activeElement = tag ? { tagName: tag } : null; },
        setDialogOpen: (on) => { dialogOpen = on; },
    };
}

const COHORT = [{ id: "ds1", name: "Cohort", projects: ["a", "b", "c"] }];

console.log("dataset nav");

await checkAsync("the middle of a dataset has both neighbours", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    const place = t.api.place();
    assert.equal(place.previous, "a");
    assert.equal(place.next, "c");
    assert.equal(place.index, 1);
});

await checkAsync("the walk order is the dataset's own, not alphabetical", async () => {
    // Recorded out of order on purpose: if this ever starts sorting, the walk
    // stops matching the order somebody arranged their cohort in.
    const t = boot({
        datasets: [{ id: "d", name: "C", projects: ["zebra", "apple", "mango"] }],
        here: "apple",
    });
    await t.api._mount();
    assert.equal(t.api.place().previous, "zebra");
    assert.equal(t.api.place().next, "mango");
});

await checkAsync("the first sample has no Previous", async () => {
    const t = boot({ datasets: COHORT, here: "a" });
    await t.api._mount();
    assert.equal(t.api.place().previous, null);
    assert.equal(t.api.place().next, "b");
});

await checkAsync("the last sample has no Next", async () => {
    const t = boot({ datasets: COHORT, here: "c" });
    await t.api._mount();
    assert.equal(t.api.place().next, null);
});

await checkAsync("a member this Plexora cannot open is skipped, not offered", async () => {
    // "b" is in the dataset but not in this page's list of projects -- an
    // unmounted shared root, or a sample deleted from another tab. Walking to
    // it would be a 404.
    const t = boot({ datasets: COHORT, here: "a", known: ["a", "c"] });
    await t.api._mount();
    assert.equal(t.api.place().next, "c", "the walk steps over it");
    assert.equal(t.api.place().members.length, 2);
});

await checkAsync("a sample in no dataset gets no controls at all", async () => {
    const t = boot({ datasets: COHORT, here: "orphan", known: ["orphan"] });
    await t.api._mount();
    assert.equal(t.api.place(), null);
    assert.equal(t.mount.children.length, 0);
});

await checkAsync("a page with no canvas never mounts one", async () => {
    const t = boot({ datasets: COHORT, here: "b", wrapper: false });
    await t.api._mount();
    assert.equal(t.api.place(), null);
});

await checkAsync("a /datasets that will not answer is silent", async () => {
    const t = boot({ datasets: COHORT, here: "b", ok: false });
    await t.api._mount();
    assert.equal(t.api.place(), null, "no controls, and no error on screen");
});

await checkAsync("the counter says where in the dataset this sample is", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    const counter = find(t.mount, (n) => n.className === "dataset-nav-count");
    assert.equal(counter.textContent, "2 / 3");
});

await checkAsync("each button names the sample it goes to", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    const next = find(t.mount, (n) => n.dataset.direction === "next");
    // The name, not just a direction: "Next" alone makes the user click to
    // find out where they are going.
    assert.equal(next.title, "Next sample: c");
    assert.equal(next.attributes["aria-label"], "Next sample: c");
    assert.equal(next.disabled, false);
    const previous = find(t.mount, (n) => n.dataset.direction === "previous");
    assert.equal(previous.title, "Previous sample: a");
});

await checkAsync("at the end the button is disabled, not removed", async () => {
    // A control that disappears on the last sample makes the row jump and
    // leaves the user wondering whether they lost the feature or reached the
    // end. A greyed one says which.
    const t = boot({ datasets: COHORT, here: "c" });
    await t.api._mount();
    const next = find(t.mount, (n) => n.dataset.direction === "next");
    assert.equal(next.disabled, true);
    assert.match(next.title, /No next sample/);
});

await checkAsync("walking carries the arrangement and then navigates", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.api.go("c");
    assert.deepEqual(JSON.parse(JSON.stringify(t.stashed)), ["c"]);
    assert.equal(t.navigated.length, 1);
    assert.match(t.navigated[0], /\/c$/);
});

await checkAsync("the open tool rides in the URL so the server renders it", async () => {
    const t = boot({ datasets: COHORT, here: "b", activeTool: "gating" });
    await t.api._mount();
    t.api.go("c");
    assert.match(t.navigated[0], /\/c\?tool=gating$/);
});

await checkAsync("a second click during a navigation is ignored", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.api.go("c");
    t.api.go("a");
    assert.equal(t.navigated.length, 1, "the page is already leaving");
});

await checkAsync("PageDown walks forward", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    let prevented = false;
    t.api._onKeyDown({ key: "PageDown", preventDefault: () => { prevented = true; } });
    assert.equal(t.navigated.length, 1);
    assert.match(t.navigated[0], /\/c$/);
    assert.equal(prevented, true, "it handled the key, so it owns it");
});

await checkAsync("PageUp walks back", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.api._onKeyDown({ key: "PageUp", preventDefault() {} });
    assert.match(t.navigated[0], /\/a$/);
});

await checkAsync("at the end the key does nothing and keeps its scroll", async () => {
    const t = boot({ datasets: COHORT, here: "c" });
    await t.api._mount();
    let prevented = false;
    t.api._onKeyDown({ key: "PageDown", preventDefault: () => { prevented = true; } });
    assert.equal(t.navigated.length, 0);
    assert.equal(prevented, false, "an unhandled PageDown still scrolls");
});

await checkAsync("the keys stand down while somebody is typing", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
        t.setTyping(tag);
        t.api._onKeyDown({ key: "PageDown", preventDefault() {} });
        assert.equal(t.navigated.length, 0, tag + " should swallow it");
    }
});

await checkAsync("and while a dialog owns the window", async () => {
    // <dialog> traps focus but not keystrokes, so the typing guard above does
    // not catch a key pressed with a dialog button focused.
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.setDialogOpen(true);
    t.api._onKeyDown({ key: "PageDown", preventDefault() {} });
    assert.equal(t.navigated.length, 0);
});

await checkAsync("a modified PageDown is somebody else's shortcut", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    for (const mod of ["metaKey", "ctrlKey", "altKey", "shiftKey"]) {
        t.api._onKeyDown({ key: "PageDown", [mod]: true, preventDefault() {} });
    }
    assert.equal(t.navigated.length, 0);
});

await checkAsync("the dataset is named, so a card can link back to it", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    assert.equal(t.api.datasetId(), "ds1");
    assert.equal(t.api.datasetName(), "Cohort");
});

console.log(`\n${checks} checks passed`);
