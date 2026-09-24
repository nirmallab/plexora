/**
 * The notice that explains a missing layer.
 *
 * A project whose cell table lives on a data node opens even when that node is
 * asleep -- deliberately, because the images are still there and refusing to
 * open them would turn a closed laptop lid into what looks like data loss. The
 * cost of that choice was a viewer that had quietly lost its cell colours with
 * nothing anywhere saying why.
 *
 * It was a strip across the top of the viewer, and it showed in a FRESH
 * session: the server's node map outlives a restart, so a project opened the
 * next morning warned about yesterday's tunnel. What is pinned here is when
 * NOT to speak:
 *
 *   - **Silence for an ordinary project.** Every project with its data on this
 *     machine reaches this code.
 *   - **"Disconnected" only about a machine that was up in this tab.** A node
 *     the project loaded with, or one remoteState saw come up, that is now
 *     missing gets a small warning notice in the corner, naming it, with
 *     Reconnect when a saved connection can do that.
 *   - **A node never seen up in this tab is not a disconnection.** When a
 *     saved connection can bring it back the project asks once, with a modal;
 *     otherwise nothing is drawn at all.
 *   - **A slow node is not a broken one.** A footnote on a notice that already
 *     exists, never a notice of its own.
 *   - **Dismissal sticks for the tab**, and a project that opens whole forgets
 *     it.
 *
 * Run in node against the shipped file: the Python suite renders templates and
 * `node --check` sees only syntax.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/resourceStatus.js");

// -- a DOM small enough to read ------------------------------------------

function makeElement(tag) {
    const node = {
        tagName: String(tag).toUpperCase(),
        className: "",
        children: [],
        parentNode: null,
        attributes: {},
        listeners: {},
        _text: "",
        get firstChild() {
            return this.children[0] || null;
        },
        setAttribute(name, value) {
            this.attributes[name] = value;
        },
        appendChild(child) {
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        insertBefore(child, before) {
            child.parentNode = node;
            const at = before ? node.children.indexOf(before) : node.children.length;
            node.children.splice(at < 0 ? node.children.length : at, 0, child);
            return child;
        },
        removeChild(child) {
            const at = node.children.indexOf(child);
            if (at >= 0) node.children.splice(at, 1);
            child.parentNode = null;
            return child;
        },
        append(...nodes) {
            nodes.forEach((child) => node.appendChild(child));
        },
        remove() {
            if (node.parentNode) node.parentNode.removeChild(node);
        },
        addEventListener(name, fn) {
            (node.listeners[name] = node.listeners[name] || []).push(fn);
        },
        click() {
            (node.listeners.click || []).forEach((fn) => fn({}));
        },
        // The three halves of <dialog> this file uses. `open` is what tells a
        // test which window is on screen.
        open: false,
        showModal() { node.open = true; },
        close() {
            node.open = false;
            (node.listeners.close || []).forEach((fn) => fn({}));
        },
        set textContent(value) {
            node._text = String(value);
            node.children.length = 0;
        },
        get textContent() {
            return node._text + node.children.map((c) => c.textContent).join("");
        },
    };
    return node;
}

function makeStorage() {
    const held = new Map();
    return {
        getItem: (key) => (held.has(key) ? held.get(key) : null),
        setItem: (key, value) => held.set(key, String(value)),
        removeItem: (key) => held.delete(key),
    };
}

/**
 * One page context. `status` is what the route answers until `rig.answer()`
 * changes it; `statuses` is a queue for a converting mask's polls.
 */
function load({ status, unreachable = [], storage = makeStorage(),
                connects = null, connected = true, statuses = null }) {
    const fetched = [];
    const opened = [];
    const reloaded = [];
    const queue = statuses ? statuses.slice() : [];
    let answer = status;
    const timers = [];
    const chip = [];
    const toasts = [];
    const listeners = {};
    const body = makeElement("div");
    const sandbox = {
        console,
        document: {
            body,
            createElement: makeElement,
            createTextNode: (text) => {
                const node = makeElement("span");
                node.textContent = text;
                return node;
            },
        },
        window: { sessionStorage: storage },
        plexoraUrl: (path) => "/base" + path,
        fetch: (url) => {
            fetched.push(url);
            const reply = url.includes("resource_status") && fetched.length > 1
                && queue.length ? queue.shift() : answer;
            return Promise.resolve({
                ok: reply !== null,
                json: () => Promise.resolve(reply),
            });
        },
        Promise,
        encodeURIComponent,
        Object,
        String,
    };
    sandbox.window.PlexoraRouting = { unreachable: () => unreachable };
    sandbox.window.fetch = sandbox.fetch;
    sandbox.PlexoraRouting = sandbox.window.PlexoraRouting;
    sandbox.window.location = { reload: () => reloaded.push(true), href: "" };
    sandbox.window.addEventListener = (name, fn) => {
        (listeners[name] = listeners[name] || []).push(fn);
    };
    // Timers a test ticks by hand, and a chip that records what it was told.
    sandbox.window.setInterval = (fn) => { timers.push(fn); return timers.length; };
    sandbox.window.clearInterval = (id) => { timers[id - 1] = null; };
    sandbox.window.PlexoraSegmentationWait = {
        start: (options) => chip.push(["start", options]),
        progress: (reading) => chip.push(["progress", reading]),
        ready: () => chip.push(["ready"]),
        failed: (error) => chip.push(["failed", error]),
    };
    // The corner notice, one at a time like the shipped one. `close(why)` is
    // the user pressing ×; `press(text)` is one of its buttons.
    sandbox.window.PlexoraToast = {
        show(options) {
            toasts.filter((t) => t.isLive()).forEach((t) => t.end("replaced"));
            let live = true;
            const toast = {
                options,
                text: [options.title, options.note, ...(options.lines || [])].join(" "),
                isLive: () => live,
                end(why) {
                    if (!live) return;
                    live = false;
                    options.onDismiss?.(why);
                },
                dismiss() { toast.end("caller"); },
                close() { toast.end("user"); },
                press(text) {
                    const action = (options.actions || []).find((a) => a.label.includes(text));
                    assert.ok(action, `no "${text}" on the notice`);
                    if (action.onSelect() !== false) toast.end("action");
                },
            };
            toasts.push(toast);
            return toast;
        },
    };
    // `connects: null` is a page that has not loaded the connection dialog at
    // all -- the right shape for one that cannot offer the button.
    if (connects !== null) {
        sandbox.window.PlexoraConnectionModal = {
            open: (options) => {
                opened.push(options);
                return Promise.resolve({ connected: connected });
            },
        };
    }
    const context = createContext(sandbox);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return { api: sandbox.window.PlexoraResourceStatus, body, fetched,
             opened, reloaded, chip, toasts,
             answer: (next) => { answer = next; },
             emit: (name, detail) => (listeners[name] || []).forEach((fn) => fn({ detail })),
             live: () => toasts.filter((t) => t.isLive()),
             tick: () => timers.filter(Boolean).forEach((fn) => fn()),
             running: () => timers.filter(Boolean).length,
             dialog: () => body.children.find(
                 (child) => child.tagName === "DIALOG") || null };
}

/** Let every already-resolved promise run: `report` fetches before it draws. */
const settle = () => new Promise((resolve) => {
    let left = 20;
    const step = () => (left-- > 0 ? Promise.resolve().then(step) : resolve());
    step();
});

function buttonSaying(root, matches) {
    const found = [];
    const walk = (node) => {
        if (node.tagName === "BUTTON" && matches(node.textContent)) {
            found.push(node);
        }
        (node.children || []).forEach(walk);
    };
    walk(root);
    return found[0] || null;
}

const WHOLE = { unavailable: {}, nodes: [] };
/** Routing that says the cell table is read from `node`. */
const readsFrom = (node, kind = "table") => ({ routes: { [kind]: { node, mode: "direct" } } });
const LOST_TABLE = { unavailable: { table: "node 'hpc' is not connected" }, nodes: ["hpc"] };
const LOST_IMAGE = {
    unavailable: { image: "data node 'o2' is not connected to this Plexora." },
    nodes: ["o2"],
    profiles: [{ node: "o2", profile: "HMS-O2" }],
};

/** A page that opened whole reading from `node`, and has since lost it. */
async function upThenLost(options, node, lost, kind = "table") {
    const rig = load({ status: WHOLE, ...options });
    await rig.api.report("demo", readsFrom(node, kind));
    rig.answer(lost);
    const notice = await rig.api.report("demo", readsFrom(node, kind));
    return { rig, notice };
}

// -- an ordinary project says nothing ------------------------------------

{
    const rig = load({ status: WHOLE });
    assert.equal(await rig.api.report("demo", {}), null);
    assert.equal(rig.body.children.length, 0);
    assert.equal(rig.toasts.length, 0);
    console.log("ok - a project with everything here draws nothing");
}

// -- a machine that was up in this tab, and is not now --------------------

{
    const { rig, notice } = await upThenLost({}, "hpc", LOST_TABLE);
    assert.ok(notice && notice.isLive(), "expected a notice");
    assert.equal(notice.options.tone, "warning");
    assert.equal(notice.options.timeout, 0, "it stays until dismissed");
    assert.ok(notice.text.includes("Remote server disconnected"), notice.text);
    assert.ok(notice.text.includes("hpc"), notice.text);
    assert.ok(notice.text.includes("cell table"), notice.text);
    assert.equal(rig.body.children.length, 0, "no strip, no dialog");
    console.log("ok - a node that was fine earlier in this tab and is now missing gets a warning notice naming it");
}

{
    const { rig, notice } = await upThenLost({ connects: [] }, "o2", LOST_IMAGE, "image");
    assert.ok(notice.options.actions.some((a) => a.label === "Reconnect “HMS-O2”"),
              JSON.stringify(notice.options.actions.map((a) => a.label)));
    assert.equal(rig.dialog(), null, "a notice, not the offer modal");
    notice.press("Reconnect");
    await settle();
    assert.equal(rig.opened.length, 1);
    assert.equal(rig.opened[0].name, "HMS-O2");
    assert.equal(rig.opened[0].kind, "node");
    console.log("ok - ...with Reconnect, which hands off to the one dialog that connects");
    assert.ok(rig.fetched.some((url) => url.includes("reload_datasource")),
              rig.fetched.join(" "));
    assert.equal(rig.reloaded.length, 1);
    console.log("ok - ...and the project is read again, then the page");
}

{
    const { rig, notice } = await upThenLost({ connects: [], connected: false },
                                             "o2", LOST_IMAGE, "image");
    notice.press("Reconnect");
    await settle();
    assert.equal(rig.reloaded.length, 0, "nothing reloads on a failed connect");
    assert.equal(rig.live().length, 1, "the notice is back, button and all");
    assert.ok(rig.live()[0].text.includes("o2"));
    assert.equal(rig.api.isDismissed("demo"), false, "pressing a button is not dismissing");
    console.log("ok - a Reconnect that does not connect puts the notice back");
}

{
    const { rig, notice } = await upThenLost({}, "hpc", LOST_TABLE);
    const again = await rig.api.report("demo", readsFrom("hpc"));
    assert.equal(again, notice, "the same notice, left where it is");
    assert.equal(rig.toasts.length, 1, "not animated in a second time");
    console.log("ok - a re-report saying the same thing does not raise it again");

    rig.answer(WHOLE);
    await rig.api.report("demo", readsFrom("hpc"));
    assert.equal(notice.isLive(), false);
    console.log("ok - the notice goes when the project is whole again");
}

{
    // Connected from the globe after the page loaded: remoteState's event is
    // what says it was up, before any report has seen it working.
    const rig = load({ status: LOST_TABLE });
    rig.emit("plexora:remote-nodes-changed", { changed: [{ name: "HPC", node: "hpc", up: true }] });
    const notice = await rig.api.report("demo", {});
    assert.ok(notice && notice.text.includes("hpc"));
    console.log("ok - a node seen up through remoteState counts as up in this tab");
}

{
    const { notice } = await upThenLost({}, "hpc", {
        ...LOST_TABLE,
        reconnect: "Reconnect with `plexora connect hpc` on the computer you started it from.",
    });
    assert.ok(notice.text.includes("plexora connect hpc"), notice.text);
    assert.equal(JSON.stringify(notice.options.actions.map((a) => a.label)),
                 JSON.stringify(["Open Settings"]));
    console.log("ok - with no saved connection, the notice names the command and offers Settings");
}

// -- slow is a footnote, never a notice ------------------------------------

{
    const rig = load({ status: WHOLE, unreachable: ["hpc-scratch"] });
    assert.equal(await rig.api.report("demo", readsFrom("hpc-scratch")), null);
    assert.equal(rig.toasts.length, 0);
    console.log("ok - a node reached only through the server raises nothing");
}

{
    const { notice } = await upThenLost({ unreachable: ["other"] }, "hpc", LOST_TABLE);
    assert.ok(notice.text.includes("relayed through this server"), notice.text);
    console.log("ok - a slow node is a footnote on a notice that already exists");
}

// -- dismissal sticks for the tab ----------------------------------------

{
    const storage = makeStorage();
    const { notice } = await upThenLost({ storage }, "hpc", LOST_TABLE);
    notice.close();
    const again = await upThenLost({ storage }, "hpc", LOST_TABLE);
    // The healthy first report of `upThenLost` is a project that opened
    // whole, which ends the conversation -- so dismiss once more and ask.
    again.notice.close();
    const rig = again.rig;
    assert.equal(await rig.api.report("demo", readsFrom("hpc")), null);
    assert.equal(rig.live().length, 0);
    console.log("ok - dismissing it is remembered for this tab");
}

// -- and stops being remembered once the project is whole ------------------
//
// The memories record an answer about a SITUATION, and were keyed on the
// project alone. Connect, work, disconnect, reopen -- one afternoon, not an
// edge case -- and the second break was met with the silence of an answer
// given about the first. So a project that opens whole ends the conversation
// about it, which means the route is asked even when a notice was dismissed.

{
    const storage = makeStorage();
    const { rig, notice } = await upThenLost({ storage }, "hpc", LOST_TABLE);
    notice.close();
    assert.equal(rig.api.isDismissed("demo"), true);
    rig.answer(WHOLE);
    assert.equal(await rig.api.report("demo", readsFrom("hpc")), null);
    assert.equal(rig.api.isDismissed("demo"), false,
                 "a project that opened whole is no longer dismissed");
    rig.answer(LOST_TABLE);
    assert.ok(await rig.api.report("demo", readsFrom("hpc")),
              "the next break is reported again");
    console.log("ok - a project that opens whole forgets both answers");
}

// -- a node never seen up in this tab -------------------------------------
//
// Typically one left on the map by a previous run of the server. It is not a
// disconnection -- nobody connected it this session -- so there is no notice.

{
    const rig = load({ status: LOST_TABLE });
    assert.equal(await rig.api.report("demo", readsFrom("hpc")), null);
    assert.equal(rig.toasts.length, 0, "no notice");
    assert.equal(rig.body.children.length, 0, "no strip, no dialog");
    assert.equal(rig.api._seenUp().length, 0, "missing is not seen up");
    console.log("ok - a node never seen up in this tab is not called disconnected");
}

{
    const rig = load({ status: LOST_IMAGE, connects: [] });
    const pending = rig.api.report("demo", readsFrom("o2", "image"));
    await settle();
    const dialog = rig.dialog();
    assert.ok(dialog && dialog.open, "expected a modal");
    const text = dialog.textContent;
    assert.ok(text.includes("HMS-O2"), text);
    assert.ok(text.includes("The image"), text);
    // The server's own words, not a category.
    assert.ok(text.includes("is not connected to this Plexora"), text);
    assert.equal(rig.toasts.length, 0, "a question, not a notice");
    buttonSaying(dialog, (t) => t.includes("Connect")).click();
    await pending;
    await settle();
    assert.equal(rig.opened.length, 1);
    assert.equal(rig.opened[0].name, "HMS-O2");
    assert.equal(dialog.open, false, "it closes before the other one opens");
    assert.equal(rig.reloaded.length, 1);
    console.log("ok - a connectable machine never seen up is asked about once, with a modal");
}

{
    const storage = makeStorage();
    const first = load({ status: LOST_IMAGE, connects: [], storage });
    const pending = first.api.report("demo", {});
    await settle();
    buttonSaying(first.dialog(), (t) => t.includes("Continue")).click();
    assert.equal(await pending, null);
    assert.equal(first.toasts.length, 0);
    assert.equal(first.body.children.length, 0, "no strip left behind");

    const again = load({ status: LOST_IMAGE, connects: [], storage });
    assert.equal(await again.api.report("demo", {}), null);
    assert.equal(again.dialog(), null, "asked once per tab, not per navigation");
    console.log("ok - declining the offer leaves no strip and no notice");
}

// -- the modal is asked again after the project has been whole -------------

{
    const storage = makeStorage();
    const first = load({ status: LOST_IMAGE, storage, connects: true, connected: false });
    const answered = first.api.report("demo", {});
    await settle();
    first.dialog().close();
    await answered;

    const healthy = load({ status: WHOLE, storage });
    await healthy.api.report("demo", {});

    const again = load({ status: LOST_IMAGE, storage, connects: true, connected: false });
    again.api.report("demo", {});
    await settle();
    assert.ok(again.dialog(), "asked again after the project came back whole");
    console.log("ok - the offer to connect returns once the situation has");
}

// -- a project it cannot ask about is not a broken page ------------------

{
    const rig = load({ status: null });
    assert.equal(await rig.api.report("demo", {}), null);
    assert.equal(rig.toasts.length, 0);
    console.log("ok - a status route that fails draws nothing and throws nothing");
}

// -- a cell mask a data node is converting ---------------------------------
//
// Nothing is missing: the node draws the unconverted mask meanwhile, slower.
// So no notice, a chip with the node's name and a percentage, and the layer
// redrawn from the pyramid, at a new tile version, when it lands.

const CONVERTING = {
    unavailable: {}, nodes: [],
    masks: [{ node: "hms-o2", id: "cell-ome-1", state: "preparing",
              progress: { stage: "converting", done: 3, total: 12 } }],
};

{
    const rig = load({
        status: CONVERTING,
        statuses: [
            { unavailable: {}, nodes: [], masks: [{ node: "hms-o2",
              id: "cell-ome-1", state: "preparing",
              progress: { stage: "converting", done: 9, total: 12 } }] },
            { unavailable: {}, nodes: [], masks: [{ node: "hms-o2",
              id: "cell-ome-1", state: "ready", version: "2-filled" }] },
        ],
    });
    const redrawn = [];
    rig.api.onMaskReady((version) => redrawn.push(version));
    assert.equal(await rig.api.report("demo", {}), null);
    assert.equal(rig.toasts.length, 0, "converting is not a notice");
    const [kind, options] = rig.chip[0];
    assert.equal(kind, "start");
    assert.equal(options.modal, false, "the chip only: the mask is on screen");
    assert.ok(options.label.includes("hms-o2"), options.label);
    assert.ok(rig.chip.some(([k, r]) => k === "progress" && r.progress === 25),
              JSON.stringify(rig.chip));
    console.log("ok - a converting mask is a chip naming the node, with a percentage");

    rig.tick();
    await settle();
    assert.ok(rig.chip.some(([k, r]) => k === "progress" && r.progress === 75));
    assert.equal(redrawn.length, 0);
    rig.tick();
    await settle();
    assert.deepEqual(rig.chip[rig.chip.length - 1], ["ready"]);
    assert.deepEqual(redrawn, ["2-filled"]);
    assert.equal(rig.running(), 0, "the poll stops once it is ready");
    console.log("ok - ...and the mask is redrawn at its new version when it lands");
}

{
    const rig = load({
        status: CONVERTING,
        statuses: [{ unavailable: {}, nodes: [], masks: [{ node: "hms-o2",
            id: "cell-ome-1", state: "error", error: "disk quota exceeded" }] }],
    });
    await rig.api.report("demo", {});
    rig.tick();
    await settle();
    const [kind, reason] = rig.chip[rig.chip.length - 1];
    assert.equal(kind, "failed");
    assert.ok(reason.includes("disk quota exceeded"), reason);
    assert.ok(reason.includes("unconverted mask"), reason);
    assert.equal(rig.running(), 0);
    console.log("ok - a conversion that fails again says the node's reason");
}

{
    const storage = makeStorage();
    const failed = {
        unavailable: {}, nodes: [],
        masks: [{ node: "hms-o2", id: "cell-ome-1", state: "error",
                  error: "cannot read cell.ome.tif",
                  warning: "/n/data is read-only for this account, so the "
                           + "cell-mask pyramid is kept in /n/mine on o2." }],
    };
    const rig = load({ status: failed, storage });
    assert.equal(await rig.api.report("demo", {}), null);
    assert.equal(rig.toasts.length, 1, "one notice for both sentences");
    const [note] = rig.toasts;
    assert.equal(note.options.timeout, 0);
    assert.ok(note.text.includes("read-only"), note.text);
    assert.ok(note.text.includes("cannot read cell.ome.tif"), note.text);
    assert.ok(note.text.includes("Showing the unconverted mask"), note.text);
    assert.equal(rig.body.children.length, 0, "no strip");
    assert.equal(rig.chip.length, 0, "nothing converting, no chip");
    note.close();

    const again = load({ status: failed, storage });
    await again.api.report("demo", {});
    assert.equal(again.toasts.length, 0, "dismissed for the tab");
    console.log("ok - a failure and a read-only note are one dismissible notice");
}
