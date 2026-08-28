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
        addEventListener(name, fn) {
            (node.listeners[name] = node.listeners[name] || []).push(fn);
        },
        click() {
            (node.listeners.click || []).forEach((fn) => fn({}));
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

function load({ status, routing = {}, unreachable = [], storage = makeStorage() }) {
    const fetched = [];
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
    const context = createContext(sandbox);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return { api: sandbox.window.PlexoraResourceStatus, body, fetched, routing };
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
    assert.equal(again.fetched.length, 0,
                 "a dismissed banner does not even ask again");
    console.log("ok - dismissing it is remembered for this tab");
}

// -- a project it cannot ask about is not a broken page ------------------

{
    const rig = load({ status: null });
    assert.equal(await rig.api.report("demo", rig.routing), null);
    console.log("ok - a status route that fails draws nothing and throws nothing");
}
