/**
 * Connecting to another machine, as the one dialog that shows it.
 *
 * What is pinned here is the part that is not obvious from reading the file,
 * and that a user only discovers at the worst moment -- fifteen minutes into a
 * queue, or three seconds into a password prompt:
 *
 *   1. **The steps are the server's states.** Five things happen, they take
 *      wildly different amounts of time, and the scheduler step is drawn only
 *      for a profile that actually waits in a queue. Inventing a step, or
 *      showing one that will never happen, is how a slow connection reads as
 *      a broken one.
 *   2. **A failure is drawn where it happened**, and the log stays on screen,
 *      because the actionable line is almost always in it.
 *   3. **A redraw does not eat a half-typed password.** The dialog re-renders
 *      every second and the box is inside it.
 *   4. **Masking follows the question**, not the fact that it is a question:
 *      a host-key fingerprint typed into a row of dots is unanswerable.
 *   5. **Closing is not cancelling.** The ssh belongs to the server; only one
 *      button here ends it, and it says so.
 *   6. **Connecting something already connecting is not an error.** Another
 *      field, or another tab, may have started it -- and watching it is what
 *      this dialog is for.
 *   7. **The terminal follows the output until the user reads it.**
 *
 * Run directly:  node tests/js/connection_modal_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/connectionModal.js");

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
        showModal() { element.open = true; },
        close() { element.open = false; element.dispatchEvent({ type: "close" }); },
        open: false,
    };
    return element;
}

/** Every element under `root`, depth first. */
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

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

// -- a remote state the test drives -----------------------------------------

const OPENING = ["connecting", "authenticating", "waiting_for_job",
                 "tunneling", "waiting_for_app"];

const posted = [];
let snapshot = null;
let deep = {};
let connectRejects = null;
let subscriber = null;

function entryFor(name) {
    return (snapshot.entries || []).find((e) => e.name === name) || null;
}

const RemotesStub = {
    KIND_VIEWER: "viewer",
    KIND_NODE: "node",
    OPENING,
    isOpening: (state) => OPENING.indexOf(state) >= 0,
    isSecret: (text) => !/\(yes\/no|fingerprint/i.test(String(text || "")),
    label: (state) => ({ idle: "Not connected", connected: "Connected",
                         failed: "Failed" }[state] || state),
    half: (entry, kind) => (kind === "node" ? entry.node : entry.viewer),
    entry: entryFor,
    snapshot: () => snapshot,
    focused: (name, kind) => deep[`${kind}:${name}`] || null,
    refresh: () => Promise.resolve(snapshot),
    subscribe: (cb, options) => {
        subscriber = { cb, options };
        cb(snapshot);
        return () => { subscriber = null; };
    },
    connect: (name, kind) => {
        posted.push({ action: "connect", name, kind });
        if (connectRejects) {
            const message = connectRejects;
            connectRejects = null;
            return Promise.reject(new Error(message));
        }
        return Promise.resolve({});
    },
    disconnect: (name, kind) => {
        posted.push({ action: "disconnect", name, kind });
        return Promise.resolve({});
    },
    answer: (name, kind, id, value) => {
        posted.push({ action: "answer", name, kind, id, value });
        return Promise.resolve({});
    },
};

/** Push a new state at whoever is subscribed. */
function say(next) {
    snapshot = next;
    if (subscriber) subscriber.cb(snapshot);
}

function profile(name, opts = {}) {
    const viewer = Object.assign({ state: "idle", phase: "", error: null,
                                   prompt: null, url: null, log: [] },
                                 opts.viewer || {});
    const node = Object.assign({ state: "idle", phase: "", error: null,
                                 prompt: null, node: null }, opts.node || {});
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        queued: Boolean(opts.queued), viewer, node,
        connected: viewer.state === "connected" || Boolean(node.node),
        opening: RemotesStub.isOpening(viewer.state)
                 || RemotesStub.isOpening(node.state),
        prompt: viewer.prompt || node.prompt || null,
    };
}

function world(entries) {
    return { loaded: true, error: null, remotes: [], places: [],
             entries: entries, clientNode: "", serverIsRemote: false,
             server: null, focus: {} };
}

// -- load the shipped file --------------------------------------------------

// -- the two routes the add-a-server flow talks to --------------------------

const fetched = [];
let recipeCatalogue = [
    { id: "hms-o2", label: "HMS O2", blurb: "Harvard's cluster.",
      ask: ["user", "walltime", "memory"], notes: ["Connect to the LOGIN node."],
      site: true, tested: true, unverified: false },
    { id: "ssh", label: "A plain SSH server", blurb: "Any host.",
      ask: ["user", "host"], notes: [], site: false, tested: false,
      unverified: false },
    { id: "aws", label: "An AWS EC2 instance", blurb: "An instance.",
      ask: ["user", "host"], notes: ["Untested by us."], site: true,
      tested: false, unverified: true },
];
let saveReply = null;

function fetchStub(url, options = {}) {
    fetched.push({ url, method: (options || {}).method || "GET",
                   body: (options || {}).body });
    if (url.indexOf("settings/recipes/") >= 0) {
        const answer = saveReply || { remote: { name: "o2" } };
        return Promise.resolve({
            ok: !answer.error, status: answer.error ? 400 : 200,
            json: () => Promise.resolve(answer),
        });
    }
    return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ recipes: recipeCatalogue }),
    });
}

const body = makeElement("body");
const context = {
    console,
    setTimeout,
    clearTimeout,
    Promise,
    JSON,
    encodeURIComponent,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: { createElement: makeElement, body },
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const Modal = context.window.PlexoraConnectionModal;

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

function dialogNow() {
    return body.children[body.children.length - 1];
}

function stepLabels(dialog) {
    return find(dialog, "connect-step").map((s) => ({
        label: one(s, "connect-step-label").textContent,
        status: ["done", "active", "failed", "pending"]
            .find((n) => s.classList.contains("is-" + n)),
    }));
}

async function main() {
    // -- 1. the steps come from the server's states ---------------------------
    snapshot = world([profile("hpc", { node: { state: "idle" } })]);
    posted.length = 0;
    let done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("opening for a profile connects it", posted.some(
        (p) => p.action === "connect" && p.name === "hpc" && p.kind === "node"));
    say(world([profile("hpc", { node: { state: "connecting" } })]));
    await settle();
    let dialog = dialogNow();
    check("a plain ssh host is not promised a scheduler wait",
          stepLabels(dialog).every((s) => s.label !== "Waiting for the scheduler"));
    check("the step being waited for is the one that is active",
          stepLabels(dialog)[0].status === "active");
    check("a data node's last step is not called starting Plexora",
          stepLabels(dialog).some((s) => s.label === "Starting the data node"));

    say(world([profile("hpc", { node: { state: "tunneling",
                                        phase: "Opening the tunnel…" } })]));
    await settle();
    dialog = dialogNow();
    const steps = stepLabels(dialog);
    check("...and everything before it is already done",
          steps[0].status === "done" && steps[1].status === "done");
    check("the server's own sentence is what is shown, not a translation",
          one(dialog, "connect-phase").textContent === "Opening the tunnel…");

    // -- 4/3. the question, and not eating the answer -------------------------
    say(world([profile("hpc", {
        node: { state: "authenticating",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    dialog = dialogNow();
    const secretBox = walk(one(dialog, "connect-prompt"))
        .find((n) => n.tagName === "INPUT");
    check("a password question is masked", secretBox.type === "password");
    check("...and the question is shown exactly as ssh asked it",
          one(dialog, "connect-prompt-text").textContent === "me@hpc's password:");

    secretBox.value = "half-typed";
    say(world([profile("hpc", {
        node: { state: "authenticating", phase: "still going",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    check("a redraw while the same question stands leaves the box alone",
          walk(one(dialogNow(), "connect-prompt"))
              .find((n) => n.tagName === "INPUT").value === "half-typed");

    posted.length = 0;
    buttonSaying(dialogNow(), "Send").click();
    await settle();
    check("sending hands the answer over and clears the box",
          posted.some((p) => p.action === "answer" && p.value === "half-typed")
          && walk(one(dialogNow(), "connect-prompt"))
              .find((n) => n.tagName === "INPUT").value === "");

    // -- 4. a host-key question is not a secret -------------------------------
    say(world([profile("hpc", {
        node: { state: "authenticating",
                prompt: { id: "p2",
                          text: "ED25519 key fingerprint is SHA256:abc.\n"
                                + "Are you sure you want to continue "
                                + "connecting (yes/no)?" } } })]));
    await settle();
    dialog = dialogNow();
    const openBox = walk(one(dialog, "connect-prompt"))
        .find((n) => n.tagName === "INPUT");
    check("a host-key question is answerable in the clear",
          openBox.type === "text");
    check("...with the two answers ssh accepts, as buttons",
          Boolean(buttonSaying(dialog, "Yes") && buttonSaying(dialog, "No")));
    posted.length = 0;
    buttonSaying(dialog, "Yes").click();
    await settle();
    check("...which send what ssh expects",
          posted.some((p) => p.action === "answer" && p.value === "yes"));

    // -- 7. the terminal follows until it is read -----------------------------
    deep = { "node:hpc": { state: "authenticating",
                           log: ["line one", "line two"] } };
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    dialog = dialogNow();
    let term = one(dialog, "connect-log-body");
    check("the log is the whole tail the focused fetch brought back",
          term.textContent === "line one\nline two");
    check("...pinned to the bottom", term.scrollTop === term.scrollHeight);

    term.scrollTop = 0;
    term.dispatchEvent({ type: "scroll" });
    deep = { "node:hpc": { state: "authenticating",
                           log: ["line one", "line two", "line three"] } };
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    term = one(dialogNow(), "connect-log-body");
    check("scrolling up to read stops it yanking itself back down",
          term.scrollTop === 0);

    // -- 5. closing is not cancelling -----------------------------------------
    dialog = dialogNow();
    posted.length = 0;
    check("while opening, the way out says it leaves the connection running",
          Boolean(buttonSaying(dialog, "Continue in background")));
    check("...and the button that ends it says that instead",
          Boolean(buttonSaying(dialog, "Stop connecting")));
    buttonSaying(dialog, "Continue in background").click();
    let outcome = await done;
    check("closing the window disconnects nothing",
          posted.length === 0 && outcome.connected === false);

    // -- 2. a failure is drawn where it happened ------------------------------
    deep = { "node:hpc": { state: "failed", log: ["Permission denied"] } };
    snapshot = world([profile("hpc", { node: { state: "connecting" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    say(world([profile("hpc", {
        node: { state: "failed",
                error: "That password was not accepted." } })]));
    await settle();
    dialog = dialogNow();
    const failed = stepLabels(dialog);
    check("the step that was running is the one marked failed",
          failed[1].status === "failed" && failed[0].status === "done");
    check("...and what went wrong is said in words",
          one(dialog, "connect-modal-error").textContent
          === "That password was not accepted.");
    check("...with the log still on screen, where the reason usually is",
          one(dialog, "connect-log-body").textContent === "Permission denied");
    check("a failure offers the two things that can help",
          Boolean(buttonSaying(dialog, "Try again")
                  && buttonSaying(dialog, "Edit connection")));

    posted.length = 0;
    buttonSaying(dialog, "Try again").click();
    await settle();
    check("trying again starts a new connection rather than a new dialog",
          posted.some((p) => p.action === "connect")
          && body.children.filter((c) => c.classList
                                          .contains("connect-modal")).length === 1);

    // -- connected resolves with what the caller needs ------------------------
    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } })]));
    outcome = await done;
    check("connecting resolves with the node a field has to address",
          outcome.connected === true && outcome.node === "hpc-data"
          && outcome.name === "hpc");

    // -- 6. joining a connection somebody else started ------------------------
    posted.length = 0;
    snapshot = world([profile("hpc", { node: { state: "waiting_for_job" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a connection already on its way is watched, not restarted",
          posted.every((p) => p.action !== "connect"));
    check("...and a queued profile is not what makes the scheduler step appear",
          stepLabels(dialogNow())
              .every((s) => s.label !== "Waiting for the scheduler"));
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;

    snapshot = world([profile("hpc", { queued: true,
                                       node: { state: "waiting_for_job" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a profile that waits in a queue gets the step that says so",
          stepLabels(dialogNow())
              .some((s) => s.label === "Waiting for the scheduler"
                           && s.status === "active"));
    buttonSaying(dialogNow(), "Stop connecting").click();
    outcome = await done;
    check("stopping ends the connection and the errand together",
          posted.some((p) => p.action === "disconnect")
          && outcome.connected === false);

    // -- already connected is an answer, not a second connection --------------
    posted.length = 0;
    snapshot = world([profile("hpc", { node: { state: "connected",
                                               node: "hpc-data" } })]);
    outcome = await Modal.open({ name: "hpc", kind: "node" });
    check("a machine already connected resolves immediately",
          outcome.connected === true && outcome.node === "hpc-data"
          && posted.every((p) => p.action !== "connect"));

    // -- with no name, it asks which machine ----------------------------------
    snapshot = world([profile("hpc", { node: { state: "idle" } }),
                      profile("o2", { node: { state: "connected",
                                              node: "o2-data" } })]);
    done = Modal.open({ kind: "node" });
    await settle();
    dialog = dialogNow();
    check("with no machine named, the dialog asks which",
          find(dialog, "connect-modal-row").length === 2);
    check("...saying which of them is already up",
          one(find(dialog, "connect-modal-row")[1], "connect-modal-chip")
              .textContent === "Connected");
    buttonSaying(find(dialog, "connect-modal-row")[1], "Use this").click();
    outcome = await done;
    check("choosing a connected machine takes it as the answer",
          outcome.connected === true && outcome.name === "o2"
          && outcome.node === "o2-data");

    // -- adding a server ------------------------------------------------------
    snapshot = world([]);
    fetched.length = 0;
    posted.length = 0;
    done = Modal.open({ kind: "node" });
    await settle();
    check("with nothing saved at all, the dialog still offers a way forward",
          Boolean(buttonSaying(dialogNow(), "Add a new server")));
    buttonSaying(dialogNow(), "Add a new server").click();
    await settle();
    dialog = dialogNow();
    check("adding a server starts from the machine you use",
          find(dialog, "connect-recipe").length === 3);
    check("...fetched rather than shipped in every page",
          fetched.some((f) => f.url === "/settings/recipes"));
    const badges = find(dialog, "connect-recipe-badge");
    check("a preset we have not connected with says so before it is chosen",
          badges.length === 1);
    check("...and a generic shape carries no badge to devalue that one",
          find(dialog, "connect-recipe")[1].textContent.indexOf("untested") < 0);

    // A poll arriving mid-form must not replace what somebody is typing.
    find(dialog, "connect-recipe")[0].click();
    await settle();
    dialog = dialogNow();
    let fields = find(dialog, "connect-field");
    check("a preset asks only for what genuinely differs",
          fields.length === 4);
    check("...and says what the site expects, in sentences",
          find(dialog, "connect-notes").length === 1);
    const boxes = fields.map((f) => walk(f).find((n) => n.tagName === "INPUT"));
    boxes[1].value = "aj";
    say(world([]));
    await settle();
    check("a poll while the form is open leaves what was typed alone",
          walk(find(dialogNow(), "connect-field")[1])
              .find((n) => n.tagName === "INPUT").value === "aj");

    fetched.length = 0;
    posted.length = 0;
    snapshot = world([profile("o2", { node: { state: "idle" } })]);
    buttonSaying(dialogNow(), "Save and connect").click();
    await settle();
    const saved = fetched.find((f) => f.method === "POST");
    check("saving goes to the server, which composes what a preset means",
          Boolean(saved) && saved.url === "/settings/recipes/hms-o2"
          && JSON.parse(saved.body).user === "aj");
    check("...and connecting follows without a second press",
          posted.some((p) => p.action === "connect" && p.name === "o2"));
    say(world([profile("o2", { node: { state: "connected",
                                       node: "o2-data" } })]));
    outcome = await done;
    check("...ending with the machine the field asked for",
          outcome.connected === true && outcome.node === "o2-data");

    // -- a refusal that is really "already running" ---------------------------
    posted.length = 0;
    connectRejects = "A connection to “hpc” is already open.";
    snapshot = world([profile("hpc", { node: { state: "connecting" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a refusal from a connection that IS opening is not shown as failure",
          one(dialogNow(), "connect-modal-error").hidden === true);
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall connection-modal checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
