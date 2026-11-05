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
 *     PageUp/PageDown are also ordinary scrolling keys, and B/N are letters.
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
                activeTool = "", ok = true, strip = true, bridge = false } = {}) {
    const mount = wrapper ? element("div") : null;
    const documentListeners = {};
    const windowListeners = {};
    const navigated = [];
    const stashed = [];
    //: What the agent bridge was asked to see off: [href, go].
    const bridged = [];
    //: What the counter asked of the thumbnail grid, in order.
    const stripCalls = [];
    let stripOpen = false;
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
    context.addEventListener = (name, fn) => { (windowListeners[name] ||= []).push(fn); };
    context.flaskVariables = {
        datasource: here,
        datasources: known === null ? members : known,
    };
    context.PlexoraCarryOver = { stash: (to) => { stashed.push(to); return true; } };
    context.PlexoraToolLoader = { activeTool: () => activeTool };
    context.PlexoraRouter = { go: (href) => navigated.push(href) };
    if (strip) {
        context.PlexoraDatasetStrip = {
            open: (options) => { stripOpen = true; stripCalls.push(["open", options]); return true; },
            close: () => { stripOpen = false; stripCalls.push(["close"]); },
            update: (states) => { stripCalls.push(["update", states]); },
            isOpen: () => stripOpen,
        };
    }
    // services/agentBridge.js's navigate: it tells the server first, then
    // calls `go` -- here at once, as a bridge with no session does.
    if (bridge) {
        context.PlexoraAgentBridge = {
            navigate: async (href, go) => {
                bridged.push([href, go]);
                if (bridge === "fails") throw new Error("the server did not answer");
                go();
            },
        };
    }
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return {
        api: context.PlexoraDatasetNav,
        mount, navigated, stashed, documentListeners, stripCalls, bridged,
        setTyping: (tag) => { activeElement = tag ? { tagName: tag } : null; },
        setDialogOpen: (on) => { dialogOpen = on; },
        /** The agent panel passing on a dataset run's progress. */
        progress: (detail) => {
            for (const fn of windowListeners["plexora:agent-dataset-progress"] || []) fn({ detail });
        },
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
    assert.equal(next.title, "Next sample: c (N)");
    assert.equal(next.attributes["aria-label"], "Next sample: c (N)");
    assert.equal(next.disabled, false);
    const previous = find(t.mount, (n) => n.dataset.direction === "previous");
    assert.equal(previous.title, "Previous sample: a (B)");
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

await checkAsync("N walks forward", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    let prevented = false;
    t.api._onKeyDown({ key: "n", preventDefault: () => { prevented = true; } });
    assert.match(t.navigated[0], /\/c$/);
    assert.equal(prevented, true);
});

await checkAsync("B walks back", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.api._onKeyDown({ key: "b", preventDefault() {} });
    assert.match(t.navigated[0], /\/a$/);
});

await checkAsync("a capital B (Caps Lock) still walks", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.api._onKeyDown({ key: "B", preventDefault() {} });
    assert.match(t.navigated[0], /\/a$/);
});

await checkAsync("each button shows its key cap", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    const previous = find(t.mount, (n) => n.dataset.direction === "previous");
    const next = find(t.mount, (n) => n.dataset.direction === "next");
    const capOf = (b) => find(b, (n) => n.className === "dataset-nav-key");
    assert.equal(capOf(previous).textContent, "B");
    assert.equal(capOf(next).textContent, "N");
    // Outermost on each side.
    assert.equal(previous.children[0].className, "dataset-nav-key");
    assert.equal(next.children[next.children.length - 1].className, "dataset-nav-key");
});

await checkAsync("a modified N is somebody else's shortcut", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    for (const mod of ["metaKey", "ctrlKey", "altKey", "shiftKey"]) {
        t.api._onKeyDown({ key: "n", [mod]: true, preventDefault() {} });
    }
    assert.equal(t.navigated.length, 0);
});

await checkAsync("the letters stand down while somebody is typing", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.setTyping("INPUT");
    t.api._onKeyDown({ key: "n", preventDefault() {} });
    t.api._onKeyDown({ key: "b", preventDefault() {} });
    assert.equal(t.navigated.length, 0);
});

await checkAsync("the dataset is named, so a card can link back to it", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    assert.equal(t.api.datasetId(), "ds1");
    assert.equal(t.api.datasetName(), "Cohort");
});

const counterOf = (t) => find(t.mount, (n) => n.className === "dataset-nav-count");
const press = (node) => (node.listeners.click || []).forEach((fn) => fn({}));

await checkAsync("the counter is a button that says what it opens", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    const counter = counterOf(t);
    assert.equal(counter.tagName, "BUTTON");
    assert.equal(counter.type, "button");
    assert.equal(counter.attributes["aria-haspopup"], "true");
    assert.equal(counter.attributes["aria-expanded"], "false");
    assert.match(counter.title, /^Sample 2 of 3 in Cohort .* show all samples$/);
});

await checkAsync("pressing the counter opens the strip with this dataset's members", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    press(counterOf(t));
    assert.equal(t.stripCalls.length, 1);
    const [verb, options] = t.stripCalls[0];
    assert.equal(verb, "open");
    assert.deepEqual([...options.members], ["a", "b", "c"]);
    assert.equal(options.current, "b");
    assert.equal(options.label, "Cohort");
    assert.equal(options.anchor, counterOf(t));
    assert.equal(options.chip.className, "dataset-nav", "it hangs under the whole chip");
});

await checkAsync("pressing it again closes the strip", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    press(counterOf(t));
    press(counterOf(t));
    assert.deepEqual(t.stripCalls.map((c) => c[0]), ["open", "close"]);
    assert.equal(t.navigated.length, 0, "dismissing never navigates");
});

await checkAsync("picking a sample from the strip walks there", async () => {
    const t = boot({ datasets: COHORT, here: "a", activeTool: "gating" });
    await t.api._mount();
    press(counterOf(t));
    t.stripCalls[0][1].onPick("c");
    assert.deepEqual(JSON.parse(JSON.stringify(t.stashed)), ["c"], "the same carry-over as Next");
    assert.match(t.navigated[0], /\/c\?tool=gating$/);
});

await checkAsync("walking closes the strip before it carries the arrangement", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    press(counterOf(t));
    t.api._onKeyDown({ key: "n", preventDefault() {} });
    assert.deepEqual(t.stripCalls.map((c) => c[0]), ["open", "close"]);
    assert.match(t.navigated[0], /\/c$/);
});

await checkAsync("without the strip module the counter is harmless", async () => {
    const t = boot({ datasets: COHORT, here: "b", strip: false });
    await t.api._mount();
    press(counterOf(t));
    t.api.go("c");
    assert.equal(t.navigated.length, 1);
});

// -- an agent gating the dataset ----------------------------------------------

const RUN = (active, done = false) => ({
    session_id: "gs_d", done,
    dataset_progress: {
        dataset: "Cohort", reference_image: "a", order: ["a", "b", "c"],
        images: { a: { state: "gating", markers_done: 4, markers_total: 16 },
                  b: { state: "queued", markers_done: 0, markers_total: 16 },
                  c: { state: active > 1 ? "gating" : "done", markers_done: 2, markers_total: 16 } },
        counts: { images_active: active, images_total: 3 },
    },
});
// The chip drawn last: mount() runs on load and again in each check, and this
// stand-in's removeChild never sees a parentNode, so earlier ones stay behind.
const chipOf = (t) => t.mount.children[t.mount.children.length - 1];
const gatingOf = (t) => find(chipOf(t), (n) => n.className === "dataset-nav-gating");

await checkAsync("a dataset run's \"3 gating\" sits beside the counter, and clears when it is done", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    // Heard before /datasets answered: the chip, once drawn, says it.
    t.progress(RUN(2));
    await t.api._mount();
    const badge = gatingOf(t);
    const row = chipOf(t).children;
    const counter = find(chipOf(t), (n) => n.className === "dataset-nav-count");
    assert.equal(row.indexOf(badge), row.indexOf(counter) + 1, "right after the counter");
    assert.equal(badge.textContent, "2 gating");
    assert.equal(badge.hidden, false);
    assert.equal(badge.title, "An agent is gating 2 images in this dataset");
    t.progress(RUN(0));
    assert.equal(badge.hidden, true, "nothing being gated, nothing said");
    t.progress(RUN(1));
    assert.equal(badge.textContent, "1 gating");
    t.progress(RUN(1, true));
    assert.equal(badge.hidden, true, "the run is over");
    assert.equal(badge.textContent, "");
    assert.equal(t.api.gating(), null);
});

await checkAsync("the grid is handed the run's states, re-badged as it goes, and keeps Gating only", async () => {
    const t = boot({ datasets: COHORT, here: "b" });
    await t.api._mount();
    t.progress(RUN(2));
    const [verb, states] = t.stripCalls.at(-1);
    assert.equal(verb, "update");
    assert.equal(states.a.state, "gating");
    press(counterOf(t));
    const [, options] = t.stripCalls.at(-1);
    assert.equal(options.states.c.state, "gating");
    assert.equal(options.activeOnly, false);
    options.onFilter(true);
    press(counterOf(t));                                // closes
    press(counterOf(t));                                // and opens again
    assert.equal(t.stripCalls.at(-1)[1].activeOnly, true, "as the user left it");
    t.progress(RUN(1, true));
    assert.deepEqual({ ...t.stripCalls.at(-1)[1] }, {}, "done: every badge goes");
});

await checkAsync("walking goes through the agent bridge, so a mirrored run follows the tab", async () => {
    const t = boot({ datasets: COHORT, here: "b", bridge: true, activeTool: "gating" });
    await t.api._mount();
    t.api.go("c");
    assert.equal(t.bridged.length, 1);
    assert.match(t.bridged[0][0], /\/c\?tool=gating$/);
    assert.deepEqual(JSON.parse(JSON.stringify(t.stashed)), ["c"], "the same carry-over");
    assert.equal(t.navigated.length, 1, "the bridge's go is the router's");
    assert.match(t.navigated[0], /\/c\?tool=gating$/);
    t.bridged[0][1]();
    assert.equal(t.navigated.length, 1, "and it moves once");
    // A bridge that fails before it moves still lets the walk happen.
    const broken = boot({ datasets: COHORT, here: "b", bridge: "fails" });
    await broken.api._mount();
    broken.api.go("a");
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(broken.bridged.length, 1);
    assert.equal(broken.navigated.length, 1, "the router takes it");
    assert.match(broken.navigated[0], /\/a$/);
});

console.log(`\n${checks} checks passed`);
