/**
 * The banner that explains a missing layer.
 *
 * A project whose cell table lives on a data node opens even when that node is
 * asleep -- deliberately, because the images are still there and refusing to
 * open them would turn a closed laptop lid into what looks like data loss. The
 * cost of that choice was a viewer that had quietly lost its cell colours with
 * nothing anywhere saying why.
 *
 * Four decisions are worth pinning, and each one is a way this could be worse
 * than saying nothing:
 *
 *   - **Silence for an ordinary project.** Every project with its data on this
 *     machine reaches this code, and a banner there would be a false alarm on
 *     the common path.
 *   - **The node's NAME is in the sentence.** That is the name in Settings and
 *     in the profile that reconnects it. "A data source was unavailable" is
 *     true and unactionable.
 *   - **A slow node is not a broken one.** Tiles relayed through the server
 *     are a hop slower and nothing is missing; that is a footnote on an
 *     existing banner, never a banner of its own.
 *   - **Dismissal sticks for the tab.** Someone who knows their node is off
 *     and is working on the images anyway must not be told again on every
 *     navigation inside the app.
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

function load({ status, routing = {}, unreachable = [], storage = makeStorage(),
                connects = null, connected = true }) {
    const fetched = [];
    const opened = [];
    const reloaded = [];
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
            return Promise.resolve({
                ok: status !== null,
                json: () => Promise.resolve(status),
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
    sandbox.window.location = { reload: () => reloaded.push(true) };
    // `connects: null` is a page that has not loaded the connection dialog at
    // all -- which is every page before this feature existed, and still the
    // right shape for one that cannot offer the button.
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
    return { api: sandbox.window.PlexoraResourceStatus, body, fetched, routing,
             opened, reloaded,
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

// -- an ordinary project says nothing ------------------------------------

{
    const rig = load({ status: { unavailable: {}, nodes: [] } });
    const banner = await rig.api.report("demo", rig.routing);
    assert.equal(banner, null);
    assert.equal(rig.body.children.length, 0);
    console.log("ok - a project with everything here draws no banner");
}

// -- a missing layer names the node --------------------------------------

{
    const rig = load({
        status: {
            unavailable: { table: "node 'hpc-scratch' did not answer" },
            nodes: ["hpc-scratch"],
        },
    });
    const banner = await rig.api.report("demo", rig.routing);
    assert.ok(banner, "expected a banner");
    const text = banner.textContent;
    assert.ok(text.includes("The cell table"), text);
    assert.ok(text.includes("hpc-scratch"), text);
    assert.ok(text.includes("Open Settings"), text);
    assert.equal(rig.body.children[0], banner, "banner goes at the top");
    console.log("ok - a missing layer names the node and points at Settings");
}

// -- slow is a footnote, never a banner ----------------------------------

{
    const rig = load({
        status: { unavailable: {}, nodes: [] },
        unreachable: ["hpc-scratch"],
    });
    assert.equal(await rig.api.report("demo", rig.routing), null);
    console.log("ok - a node reached only through the server raises no banner");
}

{
    const rig = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"] },
        unreachable: ["other"],
    });
    const banner = await rig.api.report("demo", rig.routing);
    assert.ok(banner.textContent.includes("relayed through this server"),
              banner.textContent);
    console.log("ok - a slow node is a footnote on a banner that already exists");
}

// -- dismissal sticks for the tab ----------------------------------------

{
    const storage = makeStorage();
    const first = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"] },
        storage,
    });
    const banner = await first.api.report("demo", first.routing);
    banner.children.find((child) => child.tagName === "BUTTON").click();
    assert.equal(banner.parentNode, null, "dismiss removes the banner");

    const again = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"] },
        storage,
    });
    assert.equal(await again.api.report("demo", again.routing), null);
    console.log("ok - dismissing it is remembered for this tab");
}

// -- and stops being remembered once the project is whole ------------------
//
// The memories record an answer about a SITUATION, and were keyed on the
// project alone. Connect, work, disconnect, reopen -- one afternoon, not an
// edge case -- and the second break was met with the silence of an answer
// given about the first. So a project that opens whole ends the conversation
// about it, which means the route is asked even when a banner was dismissed:
// "it is fine now" is the answer that clears the memory, and it cannot arrive
// if nobody asks.

{
    const storage = makeStorage();
    const broken = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"] },
        storage,
    });
    const banner = await broken.api.report("demo", broken.routing);
    banner.children.find((child) => child.tagName === "BUTTON").click();

    const healthy = load({ status: { unavailable: {}, nodes: [] }, storage });
    assert.equal(await healthy.api.report("demo", healthy.routing), null);
    assert.equal(healthy.fetched.length, 1,
                 "a dismissed project is still asked about");
    assert.equal(healthy.api.isDismissed("demo"), false,
                 "a project that opened whole is no longer dismissed");

    const twice = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"] },
        storage,
    });
    assert.ok(await twice.api.report("demo", twice.routing),
              "the next break is reported again");
    console.log("ok - a project that opens whole forgets both answers");
}

// -- the modal is asked again after the project has been whole -------------

{
    const storage = makeStorage();
    const first = load({
        status: {
            unavailable: { image: "no answer" },
            nodes: ["hpc-data"],
            profiles: [{ node: "hpc-data", profile: "hpc" }],
        },
        storage,
        connects: true,
        connected: false,
    });
    const answered = first.api.report("demo", first.routing);
    await settle();
    first.dialog().close();
    await answered;

    const healthy = load({ status: { unavailable: {}, nodes: [] }, storage });
    await healthy.api.report("demo", healthy.routing);

    const again = load({
        status: {
            unavailable: { image: "no answer" },
            nodes: ["hpc-data"],
            profiles: [{ node: "hpc-data", profile: "hpc" }],
        },
        storage,
        connects: true,
        connected: false,
    });
    again.api.report("demo", again.routing);
    await settle();
    assert.ok(again.dialog(), "asked again after the project came back whole");
    console.log("ok - the offer to connect returns once the situation has");
}

// -- a project it cannot ask about is not a broken page ------------------

{
    const rig = load({ status: null });
    assert.equal(await rig.api.report("demo", rig.routing), null);
    console.log("ok - a status route that fails draws nothing and throws nothing");
}

// -- a machine this Plexora can connect is a question, not a banner ---------
//
// The server says which: `profiles` names a saved connection THIS server could
// open. A machine that is one button away is a question with an answer, and a
// dismissible strip at the top of a viewer is not how you ask one.

const MISSING = {
    unavailable: { image: "data node 'o2' is not connected to this Plexora." },
    nodes: ["o2"],
    profiles: [{ node: "o2", profile: "HMS-O2" }],
};

{
    const rig = load({ status: MISSING, connects: [] });
    const pending = rig.api.report("demo", rig.routing);
    await settle();
    const dialog = rig.dialog();
    assert.ok(dialog && dialog.open, "expected a modal");
    const text = dialog.textContent;
    assert.ok(text.includes("HMS-O2"), text);
    assert.ok(text.includes("The image"), text);
    // The server's own words, not a category. "Connection refused" and "is not
    // connected to this Plexora" are different situations behind one button,
    // and only one of them is the user having pressed Disconnect.
    assert.ok(text.includes("is not connected to this Plexora"), text);
    assert.equal(rig.body.children.filter((c) => c.className
        && c.className.includes("resource-status-banner")).length, 0,
        "no banner behind the modal while it is being asked");
    console.log("ok - a connectable machine is asked about, not announced");

    buttonSaying(dialog, (t) => t.includes("Connect")).click();
    await pending;
    await settle();
    assert.equal(rig.opened.length, 1);
    assert.equal(rig.opened[0].name, "HMS-O2");
    assert.equal(rig.opened[0].kind, "node");
    assert.equal(dialog.open, false, "it closes before the other one opens");
    console.log("ok - Connect hands off to the one dialog that connects");
    assert.ok(rig.fetched.some((url) => url.includes("reload_datasource")),
              rig.fetched.join(" "));
    assert.equal(rig.reloaded.length, 1);
    console.log("ok - ...and the project is read again, then the page");
}

// A page reload is not enough on its own and that is the whole reason
// /reload_datasource exists: the server keys "which project is loaded" on the
// NAME, so a project that opened with its image missing keeps that shape until
// something asks for it again.

{
    const rig = load({ status: MISSING, connects: [], connected: false });
    const pending = rig.api.report("demo", rig.routing);
    await settle();
    buttonSaying(rig.dialog(), (t) => t.includes("Connect")).click();
    const banner = await pending;
    await settle();
    assert.equal(rig.reloaded.length, 0, "nothing reloads on a failed connect");
    assert.ok(banner, "the banner is what is left when connecting did not work");
    console.log("ok - a connection that did not happen leaves the note behind");
}

{
    const storage = makeStorage();
    const first = load({ status: MISSING, connects: [], storage });
    const pending = first.api.report("demo", first.routing);
    await settle();
    buttonSaying(first.dialog(), (t) => t.includes("Continue")).click();
    const banner = await pending;
    assert.ok(banner, "declining leaves the standing note");
    assert.ok(banner.textContent.includes("HMS-O2"), banner.textContent);
    console.log("ok - declining leaves a banner with the same button on it");

    const again = load({ status: MISSING, connects: [], storage });
    const second = await again.api.report("demo", again.routing);
    assert.equal(again.dialog(), null, "asked once per tab, not per navigation");
    assert.ok(second, "the banner still draws, being a note rather than a question");
    console.log("ok - the question is asked once, the note stays");
}

{
    // No connection dialog on the page at all: nothing to offer, so the banner
    // says what it always said -- run the command over there.
    const rig = load({
        status: { unavailable: { table: "no answer" }, nodes: ["hpc"],
                  reconnect: "Reconnect with `plexora connect hpc` on the "
                             + "computer you started it from." },
    });
    const banner = await rig.api.report("demo", rig.routing);
    assert.equal(rig.dialog(), null);
    assert.ok(banner.textContent.includes("plexora connect hpc"),
              banner.textContent);
    console.log("ok - a machine this server cannot reach still names the command");
}
