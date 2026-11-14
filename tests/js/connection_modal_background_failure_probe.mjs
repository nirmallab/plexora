/**
 * A connection that fails in the background comes back to SHOW the failure,
 * not to start another attempt.
 *
 * "Continue in background" (and Escape) hide a connection that is still
 * opening; the background watch brings the dialog back when the connection
 * needs somebody -- a question, or "a failure to read". Bringing it back must
 * not POST /connect again: that is a second ssh login (another password or
 * Duo push), a second srun job, or a second Google Cloud VM start nobody
 * asked for, and the new attempt's "connecting" replaces the failure within
 * one poll. Only Connect / Try again start an attempt.
 *
 * Run directly:  node tests/js/connection_modal_background_failure_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/connectionModal.js");
const TERMINAL = join(REPO, "plexora/client/src/js/services/logTerminal.js");
const FAILURE = join(REPO, "plexora/client/src/js/services/failureMessage.js");
const URLS = join(REPO, "plexora/client/src/js/services/passVariablesToFrontend.js");

// -- a DOM small enough to read (the shape connection_modal_probe.mjs uses) --

function makeElement(tag) {
    let own = "";
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        value: "",
        id: "",
        get textContent() {
            return own + element.children
                .map((child) => child.textContent).join("");
        },
        set textContent(value) {
            own = value === null || value === undefined ? "" : String(value);
            element.children.forEach((child) => { child.parentNode = null; });
            element.children = [];
        },
        hidden: false,
        disabled: false,
        style: {},
        children: [],
        parentNode: null,
        dataset: {},
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
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
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
            element.children = [];
            nodes.forEach((n) => element.appendChild(n));
        },
        remove() {
            const parent = element.parentNode;
            if (!parent) return;
            parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
            return true;
        },
        click() { element.dispatchEvent({ type: "click" }); },
        focus() { element.focused = true; },
        getBoundingClientRect() {
            return { left: 40, top: 300, right: 260, bottom: 332,
                     width: 220, height: 32 };
        },
        scrollIntoView() {},
        showModal() { element.open = true; },
        close() { element.open = false; element.dispatchEvent({ type: "close" }); },
        open: false,
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

function one(root, className) {
    return walk(root).filter((n) => n.classList.contains(className))[0] || null;
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

// -- a remote state the test drives ----------------------------------------

const OPENING = ["connecting", "authenticating", "installing",
                 "waiting_for_job", "tunneling", "waiting_for_app"];

const posted = [];
let snapshot = null;
const subscribers = new Set();

const RemotesStub = {
    KIND_VIEWER: "viewer",
    KIND_NODE: "node",
    OPENING,
    isOpening: (state) => OPENING.indexOf(state) >= 0,
    isSecret: () => true,
    promptChoices: () => [],
    label: (state) => ({ idle: "Not connected", connected: "Connected",
                         failed: "Failed" }[state] || state),
    half: (entry, kind) => (kind === "node" ? entry.node : entry.viewer),
    entry: (name) => (snapshot.entries || []).find((e) => e.name === name) || null,
    snapshot: () => snapshot,
    focused: () => null,
    refresh: () => Promise.resolve(snapshot),
    subscribe: (cb, options) => {
        const sub = { cb, options: options || {} };
        subscribers.add(sub);
        cb(snapshot);
        return () => { subscribers.delete(sub); };
    },
    // As the real server does: a POST /connect on a FAILED session starts a
    // fresh one (remote_sessions.start replaces a failed or exited record),
    // and the next poll reads "connecting".
    connect: (name, kind) => {
        posted.push({ action: "connect", name, kind });
        return Promise.resolve({});
    },
    disconnect: (name, kind) => {
        posted.push({ action: "disconnect", name, kind });
        return Promise.resolve({});
    },
    answer: () => Promise.resolve({}),
};

function say(next) {
    snapshot = next;
    Array.from(subscribers).forEach((sub) => {
        if (subscribers.has(sub)) sub.cb(snapshot);
    });
}

function profile(name, node) {
    const viewer = { state: "idle", phase: "", error: null, recovery: "",
                     prompt: null, url: null, log: [] };
    const half = Object.assign({ state: "idle", phase: "", error: null,
                                 recovery: "", prompt: null, node: null }, node);
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        queued: false, install: false, installEnv: null, viewer, node: half,
        connected: Boolean(half.node),
        opening: RemotesStub.isOpening(half.state),
        prompt: half.prompt || null,
    };
}

function world(entries) {
    return { loaded: true, error: null, remotes: [], places: [],
             entries: entries, clientNode: "", serverIsRemote: false,
             server: null, focus: {} };
}

const hpcAt = (node) => world([profile("hpc", node)]);

// -- load the shipped file -------------------------------------------------

const body = makeElement("body");
const context = {
    console, setTimeout, clearTimeout, Promise, JSON, encodeURIComponent,
    TypeError,
    fetch: () => Promise.resolve({ ok: true, status: 200,
                                   json: () => Promise.resolve({}) }),
    innerHeight: 800,
    innerWidth: 1200,
    document: { createElement: makeElement, body },
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
const toasts = [];
context.PlexoraToast = { show: (options) => { toasts.push(options); return {
    dismiss() {}, isLive: () => true, node: null }; } };
createContext(context);
runInContext(readFileSync(URLS, "utf-8"), context);
runInContext(readFileSync(TERMINAL, "utf-8"), context);
runInContext(readFileSync(FAILURE, "utf-8"), context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const Modal = context.window.PlexoraConnectionModal;

// -- checks ----------------------------------------------------------------

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

const isOpen = (d) => Boolean(d && d.open && d.classList.contains("connect-modal"));
const openDialogs = () => body.children.filter(isOpen);
const dialogNow = () => body.children[body.children.length - 1];
const connects = () => posted.filter((p) => p.action === "connect").length;

async function main() {
    // An attempt already on its way when the dialog opens: attached to, not
    // restarted, so no POST at all.
    snapshot = hpcAt({ state: "connecting" });
    let done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;
    check("the connection is watched in the background",
          Modal._background().join() === "node:hpc" && openDialogs().length === 0);
    const before = connects();

    // ...and it fails while nobody is looking.
    say(hpcAt({ state: "failed", error: "That password was not accepted." }));
    await settle();
    check("a failure in the background brings the dialog back",
          openDialogs().length === 1);
    check("bringing it back to show the failure does not start a new attempt",
          connects() === before);
    const err = one(dialogNow(), "connect-modal-error");
    check("...and the failure is what it shows, with Try again",
          Boolean(buttonSaying(dialogNow(), "Try again"))
          && err && err.hidden === false
          && err.textContent.indexOf("That password was not accepted.") >= 0);

    // Only the explicit button starts one.
    const tryAgain = buttonSaying(dialogNow(), "Try again");
    if (tryAgain) {
        const beforeTry = connects();
        tryAgain.click();
        await settle();
        check("Try again is what starts a new attempt", connects() === beforeTry + 1);
    }
    say(hpcAt({ state: "failed", error: "That password was not accepted." }));
    await settle();
    const closeButton = buttonSaying(dialogNow(), "Close");
    if (closeButton) closeButton.click();
    await settle();

    // The notice's Answer button, pressed after the connection has failed
    // in the meantime, likewise shows the failure rather than reconnecting.
    toasts.length = 0;
    snapshot = hpcAt({ state: "authenticating", prompt: { id: "p1", text: "Password:" } });
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;
    // The connection fails before the next poll reaches the background watch
    // (the state is set, not yet delivered), and the user presses Answer on
    // the notice: it is the notice that reopens the dialog.
    snapshot = hpcAt({ state: "failed", error: "Timed out waiting for an answer." });
    const answer = toasts.length && toasts[0].actions && toasts[0].actions[0];
    check("leaving with a question on screen leaves an Answer notice",
          Boolean(answer && answer.label === "Answer"));
    if (answer) {
        const beforeAnswer = connects();
        answer.onSelect();
        await settle();
        check("Answer on a connection that has since failed does not start a new attempt",
              connects() === beforeAnswer);
    }

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall background-failure checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
