/**
 * The Settings page's server cards, and the one thing they get wrong easily.
 *
 * A live connection updates once a second, for as long as fifteen minutes if
 * it is queued. Everything on the card that holds state the DOM owns rather
 * than the script has to survive that:
 *
 *   1. **The log keeps its place.** The cards used to be thrown away and
 *      rebuilt on every poll, so the pane was a NEW element once a second and
 *      started at the top once a second -- which is precisely when there is
 *      something in it worth reading. The pane now follows its own output
 *      until somebody scrolls up, and follows again when they come back down.
 *   2. **Opening a log asks for the whole of it.** The list payload carries
 *      the last eight lines; two hundred is the number that contains the thing
 *      that went wrong.
 *   3. **A poll does not eat a half-typed password**, and does not take the
 *      focus off a button somebody has tabbed to.
 *   4. **Connect means one thing.** A data node on the other machine, through
 *      the same dialog every other surface opens. This page used to run
 *      Plexora over there and tunnel the viewer back, which made Settings a
 *      place where the machine Plexora runs on could be redefined from inside
 *      the running app.
 *
 * Run directly:  node tests/js/settings_remotes_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/settingsPage.js");
// The real one: what is being pinned is that the card KEEPS the pane it was
// given, and a stub handing back a fresh element would pass that by accident.
const TERMINAL = join(REPO, "plexora/client/src/js/services/logTerminal.js");

// -- a DOM small enough to read ---------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        value: "",
        textContent: "",
        hidden: false,
        disabled: false,
        checked: false,
        open: false,
        href: "",
        children: [],
        parentNode: null,
        dataset: {},
        style: {},
        tabIndex: 0,
        scrollTop: 0,
        scrollHeight: 500,
        clientHeight: 100,
        focused: false,
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...n) => n.forEach((x) => classes.add(x)),
            remove: (...n) => n.forEach((x) => classes.delete(x)),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) {
            return attributes.has(name) ? attributes.get(name) : null;
        },
        removeAttribute(name) { attributes.delete(name); },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
        replaceChildren(...nodes) {
            element.children.forEach((c) => { c.parentNode = null; });
            element.children = [];
            nodes.forEach((n) => element.appendChild(n));
        },
        remove() {
            const parent = element.parentNode;
            if (!parent) return;
            parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener(type, handler) {
            listeners.set(type,
                          (listeners.get(type) || []).filter((h) => h !== handler));
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
            return true;
        },
        click() { element.dispatchEvent({ type: "click" }); },
        focus() { element.focused = true; },
    };
    return element;
}

function walk(root, out = []) {
    (root.children || []).forEach((child) => {
        out.push(child);
        walk(child, out);
    });
    return out;
}

function find(root, className) {
    return walk(root).filter((n) => n.classList.contains(className));
}

function one(root, className) {
    return find(root, className)[0] || null;
}

function textOf(root) {
    return [root.textContent].concat(walk(root).map((n) => n.textContent))
        .filter(Boolean).join(" ");
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

function logText(term) {
    return (term.children || []).map(
        (line) => (line.children || []).length
            ? line.children.map((part) => part.textContent).join(" ")
            : line.textContent).join("\n");
}

// -- the page's own elements, by id -----------------------------------------
//
// Every id resolves to a stub except the two that decide which sections start:
// the nodes panel (skipped, so its fetches stay out of this) and the rail
// (nothing here is about tabs).

const byId = new Map();

function elementFor(id) {
    if (id === "settings_panel_nodes" || id === "settings_rail") return null;
    if (!byId.has(id)) byId.set(id, makeElement("div"));
    return byId.get(id);
}

// -- the shared state the section reads -------------------------------------

const OPENING = ["connecting", "authenticating", "waiting_for_job",
                 "tunneling", "waiting_for_app"];
const posted = [];
let subscription = null;
let snapshot = null;
let deep = {};
let modalOpens = [];

const RemotesStub = {
    OPENING,
    KIND_VIEWER: "viewer",
    KIND_NODE: "node",
    isOpening: (state) => OPENING.indexOf(state) >= 0,
    label: (state) => ({ idle: "Not connected", connected: "Connected",
                         authenticating: "Needs your password",
                         waiting_for_job: "Queued",
                         failed: "Failed" }[state] || state),
    isSecret: (text) => !/yes\/no|fingerprint/i.test(String(text)),
    half: (entry, kind) => (kind === "node" ? entry.node : entry.viewer),
    snapshot: () => snapshot,
    focused: (name, kind) => deep[`${kind}:${name}`] || null,
    subscribe: (cb, options = {}) => {
        subscription = { cb, options, live: true };
        cb(snapshot);
        return () => { subscription.live = false; };
    },
    refresh: () => { posted.push({ action: "refresh" }); return Promise.resolve(snapshot); },
    disconnect: (name, kind) => {
        posted.push({ action: "disconnect", name, kind });
        return Promise.resolve({});
    },
    answer: (name, kind, id, value) => {
        posted.push({ action: "answer", name, kind, id, value });
        return Promise.resolve({});
    },
    forget: (name) => {
        posted.push({ action: "forget", name });
        return Promise.resolve({});
    },
    save: (body) => { posted.push({ action: "save", body }); return Promise.resolve({}); },
};

function say(next) {
    snapshot = next;
    if (subscription && subscription.live) subscription.cb(snapshot);
}

function profile(name, opts = {}) {
    const node = Object.assign({ state: "idle", phase: "", error: null,
                                 prompt: null, log: [], node: null },
                               opts.node || {});
    const viewer = Object.assign({ state: "idle", phase: "", error: null,
                                   prompt: null, url: null, log: [],
                                   dataNodes: [], nodeErrors: [] },
                                 opts.viewer || {});
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        queued: Boolean(opts.queued), viewer, node,
        connected: Boolean(node.node), opening: OPENING.indexOf(node.state) >= 0,
        prompt: node.prompt || null,
    };
}

function world(entries) {
    return {
        loaded: true, error: null, entries,
        remotes: entries.map((e) => ({ name: e.name, target: e.target,
                                       srun: e.queued ? "" : null })),
        places: [], clientNode: "", serverIsRemote: false, server: null,
        focus: {},
    };
}

// -- load the shipped file --------------------------------------------------

const context = {
    console,
    setTimeout,
    clearTimeout,
    Promise,
    JSON,
    Object,
    Array,
    Boolean,
    String,
    encodeURIComponent,
    fetch: () => Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve({}),
    }),
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: {
        createElement: makeElement,
        body: makeElement("body"),
        getElementById: elementFor,
        querySelectorAll: () => [],
        addEventListener: () => {},
    },
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
context.PlexoraConnectionModal = {
    open: (options) => {
        modalOpens.push(options);
        return Promise.resolve({ connected: false });
    },
};
context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };

createContext(context);
runInContext(readFileSync(TERMINAL, "utf-8"), context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

// -- checks -----------------------------------------------------------------

const failures = [];

function check(label, condition) {
    if (condition) {
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}`);
    }
}

const settle = () => new Promise((resolve) => {
    let left = 20;
    const step = () => (left-- > 0 ? Promise.resolve().then(step) : resolve());
    step();
});

const list = () => elementFor("settings_remotes_list");
const cards = () => list().children;

async function main() {
    snapshot = world([profile("hpc", { node: { state: "idle" } })]);
    context.pageInit();
    await settle();

    check("a saved server gets a card, named and stated",
          cards().length === 1
          && textOf(cards()[0]).indexOf("hpc") >= 0
          && textOf(cards()[0]).indexOf("Not connected") >= 0);
    check("...and Connect is what it offers",
          Boolean(buttonSaying(cards()[0], "Connect")));

    // -- 4. one kind of connection --------------------------------------------
    modalOpens = [];
    buttonSaying(cards()[0], "Connect").click();
    await settle();
    check("Connect opens the shared dialog, for a DATA NODE",
          modalOpens.length === 1 && modalOpens[0].name === "hpc"
          && modalOpens[0].kind === "node");
    check("...and nothing here offers to move Plexora itself",
          textOf(cards()[0]).indexOf("Open remote Plexora") < 0);

    // -- 1/3. the card is repainted, never rebuilt -----------------------------
    const wasCard = cards()[0];
    // The same button, relabelled -- not a new one in its place. Rebuilding
    // the row took the focus off whichever button somebody had tabbed to,
    // once a second, for the whole of a queued job.
    const wasToggle = one(wasCard, "settings-actions").children[0];
    say(world([profile("hpc", { node: { state: "connecting",
                                        phase: "Reaching the machine…" } })]));
    await settle();
    check("a poll repaints the card rather than replacing it",
          cards()[0] === wasCard);
    check("...including the button somebody may have tabbed to",
          one(cards()[0], "settings-actions").children[0] === wasToggle
          && wasToggle.textContent === "Disconnect");
    check("...and the server's own sentence is what it says it is doing",
          textOf(cards()[0]).indexOf("Reaching the machine…") >= 0);

    // A password box, mid-typing, through a poll.
    say(world([profile("hpc", {
        node: { state: "authenticating",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    const secret = one(cards()[0], "settings-remote-secret");
    check("a password question is masked", secret.type === "password");
    secret.value = "half-typed";
    say(world([profile("hpc", {
        node: { state: "authenticating", phase: "still going",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    check("a poll while the same question stands leaves the box alone",
          one(cards()[0], "settings-remote-secret") === secret
          && secret.value === "half-typed");

    posted.length = 0;
    buttonSaying(cards()[0], "Send").click();
    await settle();
    check("...and sending it answers the NODE's prompt, not a viewer's",
          posted.some((p) => p.action === "answer" && p.kind === "node"
                             && p.value === "half-typed"));

    // -- 1. the log behaves like a terminal ------------------------------------
    say(world([profile("hpc", {
        node: { state: "authenticating",
                log: ["opening", "  [ssh] Permission denied"] } })]));
    await settle();
    const term = one(cards()[0], "connect-log-body");
    check("the log is on the card, as a terminal",
          Boolean(term) && logText(term) === "opening\nssh Permission denied");
    check("...pinned to the bottom while nobody is reading it",
          term.scrollTop === term.scrollHeight);
    check("...with what the far machine said marked as its own",
          term.children[1].classList.contains("is-relayed"));

    // Halfway up, not the top: a rebuilt pane comes back at 0, which is where
    // somebody scrolled to the top would have been anyway. This is the check
    // that can tell those two apart.
    term.scrollTop = 220;
    term.dispatchEvent({ type: "scroll" });
    say(world([profile("hpc", {
        node: { state: "authenticating",
                log: ["opening", "  [ssh] Permission denied", "retrying"] } })]));
    await settle();
    check("the pane survives the poll rather than being replaced",
          one(cards()[0], "connect-log-body") === term);
    check("...and scrolling up to read stops it yanking itself back down",
          term.scrollTop === 220 && logText(term).indexOf("retrying") >= 0);

    term.scrollTop = term.scrollHeight;
    term.dispatchEvent({ type: "scroll" });
    say(world([profile("hpc", {
        node: { state: "authenticating",
                log: ["opening", "  [ssh] Permission denied", "retrying",
                      "connected"] } })]));
    await settle();
    check("...and coming back to the bottom sets it following again",
          term.scrollTop === term.scrollHeight);

    // -- 2. opening a log asks for the whole of it -----------------------------
    check("a closed log asks for no deep tail",
          subscription.options.focus().length === 0);
    posted.length = 0;
    const details = one(cards()[0], "settings-remote-log");
    details.open = true;
    details.dispatchEvent({ type: "toggle" });
    await settle();
    check("opening one asks for that connection's whole log",
          subscription.options.focus().length === 1
          && subscription.options.focus()[0].name === "hpc"
          && subscription.options.focus()[0].kind === "node");
    check("...and asks for it now, rather than at the next tick",
          posted.some((p) => p.action === "refresh"));

    deep = { "node:hpc": { log: ["far", "older", "lines", "from the tail"] } };
    say(world([profile("hpc", { node: { state: "authenticating",
                                        log: ["only the last few"] } })]));
    await settle();
    check("...and the deeper answer is what is drawn once it arrives",
          logText(one(cards()[0], "connect-log-body"))
          === "far\nolder\nlines\nfrom the tail");
    deep = {};

    // -- connected, and then gone ---------------------------------------------
    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } })]));
    await settle();
    check("a connected card says which node it put on the map",
          textOf(cards()[0]).indexOf("hpc-data") >= 0);
    check("...and offers to end it",
          Boolean(buttonSaying(cards()[0], "Disconnect")));
    posted.length = 0;
    buttonSaying(cards()[0], "Disconnect").click();
    await settle();
    check("...which ends the data node, not somebody's viewer",
          posted.some((p) => p.action === "disconnect" && p.kind === "node"));

    say(world([]));
    await settle();
    check("forgetting the last server leaves the empty note, not a stale card",
          cards().length === 1
          && textOf(cards()[0]).indexOf("No servers saved yet") >= 0);

    // -- the presets, from the page somebody is actually on --------------------
    modalOpens = [];
    elementFor("settings_remote_preset").click();
    await settle();
    check("the presets are reachable from the page that adds servers",
          modalOpens.length === 1 && modalOpens[0].view === "recipes"
          && modalOpens[0].kind === "node");

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall settings-remotes checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
