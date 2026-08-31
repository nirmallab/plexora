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
    WARN_SECONDS: 600,
    //: The real one interpolates against the snapshot's age. Here every
    //: snapshot is "now", so what an entry stores IS what is left -- faithful
    //: for a probe that never advances a clock. The interpolation itself, and
    //: the formatter, are checked against the shipped file in
    //: tests/js/remote_state_probe.mjs.
    remaining: (entry) => {
        const left = entry && entry.node && entry.node.timeLeft;
        return (left === null || left === undefined) ? null : left;
    },
    duration: (seconds) => {
        const total = Math.max(0, Math.round(seconds));
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const secs = total % 60;
        const pad = (value) => (value < 10 ? "0" + value : String(value));
        return hours ? hours + ":" + pad(minutes) + ":" + pad(secs)
                     : minutes + ":" + pad(secs);
    },
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
                                 prompt: null, log: [], node: null,
                                 timeLeft: null, timeLimit: null },
                               opts.node || {});
    const viewer = Object.assign({ state: "idle", phase: "", error: null,
                                   prompt: null, url: null, log: [],
                                   dataNodes: [], nodeErrors: [] },
                                 opts.viewer || {});
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        saved: opts.saved || null,
        queued: Boolean(opts.queued), viewer, node,
        // Derived from the saved record, the way remoteState's own merge does
        // it -- so this stub cannot say something the real one would not.
        install: Boolean((opts.saved || {}).install),
        installEnv: (opts.saved || {}).install_env || null,
        connected: Boolean(node.node), opening: OPENING.indexOf(node.state) >= 0,
        prompt: node.prompt || null,
    };
}

function world(entries) {
    return {
        loaded: true, error: null, entries,
        remotes: entries.map((e) => Object.assign(
            { name: e.name, target: e.target,
              srun: e.queued ? "" : null,
              srun_parts: { walltime: "", cores: "", memory: "", extra: "" },
              remote_command: "plexora", data_dir: "", forwards: [] },
            e.saved || {})),
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

    // -- adding a server ------------------------------------------------------
    //
    // The form is three questions with everything a cluster needs one
    // disclosure down. What is pinned here is that the disclosure is not where
    // answers go to be forgotten: the numbers in it are visible before they
    // are sent, they come back out of a saved profile, and they reach the POST.

    // The template supplies these; the probe's stubs have to stand in for it.
    ["settings_remote_cores", "settings_remote_memory",
     "settings_remote_walltime", "settings_remote_srun"].forEach((id, index) => {
        elementFor(id).setAttribute(
            "data-default", ["16", "128G", "4:00:00", "-p interactive"][index]);
    });

    const job = elementFor("settings_remote_job");
    const useSrun = elementFor("settings_remote_use_srun");
    check("what a job asks for is out of the way until there IS a job",
          job.hidden === true);

    useSrun.checked = true;
    useSrun.dispatchEvent({ type: "change" });
    await settle();
    check("...and turning the scheduler on reveals it",
          job.hidden === false);
    check("...filled in, not left to the site's one core and two gigabytes",
          elementFor("settings_remote_cores").value === "16"
          && elementFor("settings_remote_memory").value === "128G"
          && elementFor("settings_remote_walltime").value === "4:00:00");
    check("...including the flags that have no box of their own",
          elementFor("settings_remote_srun").value === "-p interactive");

    elementFor("settings_remote_walltime").value = "8:00:00";
    useSrun.checked = false;
    useSrun.dispatchEvent({ type: "change" });
    useSrun.checked = true;
    useSrun.dispatchEvent({ type: "change" });
    await settle();
    check("...and a walltime somebody typed survives the switch going off",
          elementFor("settings_remote_walltime").value === "8:00:00");

    // -- extra ports, as a list rather than as lines in a textarea ------------
    elementFor("settings_remote_port").value = "8642";
    elementFor("settings_remote_port_add").click();
    elementFor("settings_remote_port").value = "8642";
    elementFor("settings_remote_port_add").click();
    elementFor("settings_remote_port").value = "9000";
    elementFor("settings_remote_port_add").click();
    await settle();
    const chips = find(elementFor("settings_remote_forwards"), "remote-chip");
    check("a port added twice is in the list once",
          chips.length === 2 && textOf(chips[0]).indexOf("8642") >= 0);
    check("...and each one can be taken back out",
          Boolean(one(chips[1], "remote-chip-drop")));
    one(chips[1], "remote-chip-drop").click();
    await settle();
    check("...which leaves the other alone",
          find(elementFor("settings_remote_forwards"), "remote-chip").length === 1);

    // -- what Save actually sends ---------------------------------------------
    posted.length = 0;
    elementFor("settings_remote_name").value = "hpc";
    elementFor("settings_remote_target").value = "me@login.cluster.edu";
    elementFor("settings_remote_save").click();
    await settle();
    const sent = (posted.find((p) => p.action === "save") || {}).body || {};
    check("Save sends the three resource boxes as three answers",
          sent.cores === "16" && sent.memory === "128G"
          && sent.walltime === "8:00:00");
    check("...the rest of the job line beside them, not spliced in here",
          sent.srun === "-p interactive" && sent.use_srun === true);
    check("...and the ports as a list",
          Array.isArray(sent.forwards) && sent.forwards.length === 1
          && sent.forwards[0] === "8642");
    // The one setting on this form that makes connecting WRITE to the far
    // machine, so an unanswered form has to send it as an explicit no.
    check("...and the install switch as an explicit no until it is turned on",
          sent.install === false);

    posted.length = 0;
    elementFor("settings_remote_install").checked = true;
    elementFor("settings_remote_save").click();
    await settle();
    check("...and as a yes once it is",
          ((posted.find((p) => p.action === "save") || {}).body || {})
              .install === true);

    // -- editing one back out of the store ------------------------------------
    say(world([profile("o2", {
        saved: { name: "o2", target: "ajn@o2.hms.harvard.edu",
                 srun: "-p gpu -t 2:00:00 -c 8 --mem 64G",
                 srun_parts: { walltime: "2:00:00", cores: "8", memory: "64G",
                               extra: "-p gpu" },
                 remote_command: "conda run -n imaging plexora",
                 data_dir: "/n/data", install: true, install_env: "imaging",
                 forwards: ["8888"] },
    })]));
    await settle();
    buttonSaying(cards()[0], "Edit").click();
    await settle();
    check("editing a saved server puts its job back in the boxes it came from",
          elementFor("settings_remote_cores").value === "8"
          && elementFor("settings_remote_memory").value === "64G"
          && elementFor("settings_remote_walltime").value === "2:00:00"
          && elementFor("settings_remote_srun").value === "-p gpu");
    check("...with the scheduler shown as on, and its box open",
          elementFor("settings_remote_use_srun").checked === true
          && elementFor("settings_remote_job").hidden === false
          && elementFor("settings_remote_advanced").open === true);
    check("...and the ports it forwards as chips, not as text",
          find(elementFor("settings_remote_forwards"), "remote-chip").length === 1);
    check("...and the directory it opens in, which is a plain field now",
          elementFor("settings_remote_data_dir").value === "/n/data");
    check("...and a server that installs on connect says so, in the open",
          elementFor("settings_remote_install").checked === true);
    check("...and said on the card too, before anybody presses Connect",
          textOf(cards()[0]).indexOf("installs Plexora in imaging") >= 0);

    // Clearing the form has to clear it: the switch is not a preference that
    // survives into the next server somebody adds.
    elementFor("settings_remote_reset").click();
    await settle();
    check("...and Add-a-server starts from off again",
          elementFor("settings_remote_install").checked === false);

    // -- the presets, from the page somebody is actually on -------------------
    //
    // A dialog, not a menu that fills this form in: a preset has things to say
    // that this form has nowhere to put -- an untested site's warning, and the
    // username a site preset cannot know and the dialog refuses to compose a
    // target without.
    modalOpens = [];
    elementFor("settings_remote_reset").click();
    await settle();
    elementFor("settings_remote_preset").click();
    await settle();
    check("the presets are reachable from the page that adds servers",
          modalOpens.length === 1 && modalOpens[0].view === "recipes"
          && modalOpens[0].kind === "node");

    // -- how long the job has left --------------------------------------------
    //
    // The card is repainted on every poll and this page polls once a second
    // while it is open, so the number moves without a timer of its own. It is
    // a meta line rather than a warning because that is what it is -- the job
    // is doing exactly what it was asked to -- until the last ten minutes.
    say(world([profile("gpu", { queued: true,
                                node: { state: "connected", node: "gpu-data",
                                        timeLeft: 7382, timeLimit: 14400 } })]));
    await settle();
    check("a connection inside a job says how long it has left",
          textOf(cards()[0]).indexOf("Time remaining 2:03:02") >= 0);

    say(world([profile("gpu", { queued: true,
                                node: { state: "connected", node: "gpu-data",
                                        timeLeft: 240, timeLimit: 14400 } })]));
    await settle();
    check("...marked once it is nearly up",
          find(cards()[0], "settings-remote-clock")[0]
              .classList.contains("is-urgent"));

    say(world([profile("plain", { node: { state: "connected",
                                          node: "plain-data" } })]));
    await settle();
    check("...and a connection with no walltime is told nothing about time",
          textOf(cards()[0]).indexOf("Time remaining") < 0);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall settings-remotes checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
