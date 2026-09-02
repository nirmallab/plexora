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

/**
 * The icon control named `label`.
 *
 * Edit and Delete carry no text at all now -- they are a pencil and a bin in
 * the card's head -- so the only name they have is the one on `aria-label`,
 * which is also the only name a screen reader has. Asking for them by it is
 * therefore the same question a user's assistive technology asks.
 */
function iconSaying(root, label) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON"
               && n.getAttribute("aria-label") === label) || null;
}

/**
 * The button somebody can actually press.
 *
 * The VM buttons are built once and shown or hidden per card, so "is it in the
 * DOM" is not the question -- every card has all of them. Asking whether one
 * is OFFERED is the only version of the question with an answer.
 */
function offered(root, text) {
    const found = buttonSaying(root, text);
    return found && !found.hidden ? found : null;
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
    //: What Compute Engine says the machine is doing. Asked for on demand and
    //: deliberately never from the poll -- a round trip per cloud profile per
    //: second is not a status display, it is a bill -- so the count of these
    //: calls is itself worth pinning.
    vmStatus: (name) => {
        posted.push({ action: "vmStatus", name });
        return Promise.resolve({ vm: "plexora-" + name, status: vmState,
                                 vm_source: vmSource, bucket: "tonsil-images" });
    },
    vmStart: (name) => {
        posted.push({ action: "vmStart", name });
        return Promise.resolve({ ok: true, status: "STAGING",
                                 message: "Starting plexora-" + name + "." });
    },
    vmStop: (name) => {
        posted.push({ action: "vmStop", name });
        return Promise.resolve({ ok: true, status: "STOPPING",
                                 message: "Stopping plexora-" + name + "." });
    },
    vmDelete: (name) => {
        posted.push({ action: "vmDelete", name });
        return Promise.resolve({ ok: true, status: "missing",
                                 message: "Deleted plexora-" + name + "." });
    },
};

//: Rebound by the checks that need a different machine.
let vmState = "RUNNING";
let vmSource = "plexora";

//: Every question the page put to the user, and what it was told. Deleting a
//: server asks now -- for a rented machine it always did, and for every other
//: kind it does because the control became a bin icon eight pixels from the
//: pencil.
let confirms = [];
let confirmAnswer = true;

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
        //: Two or three words naming the kind of machine, decided server-side
        //: from the recipe that composed the profile. Carried per entry the
        //: way remoteState's own merge carries it.
        description: (opts.saved || {}).description || "",
        connected: Boolean(node.node), opening: OPENING.indexOf(node.state) >= 0,
        prompt: node.prompt || null,
        // Carried per entry the way remoteState's own merge does it. Null for
        // every profile that names a machine instead of renting one.
        gcloud: opts.gcloud || null,
    };
}

//: A profile whose machine is rented rather than owned.
const CLOUD = { vm_name: "plexora-gcp", vm_source: "plexora",
                bucket: "tonsil-images", region: "us-east1",
                machine_type: "e2-highmem-16", provisioning_model: "spot",
                on_exit: "stop" };

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
    confirm: (text) => { confirms.push(String(text)); return confirmAnswer; },
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
//: The cards the dialog would hand back. Two is enough to prove the page
//: draws what it is given and calls back with the one that was clicked.
const CATALOGUE = [{ id: "hms-o2", label: "HMS O2" },
                   { id: "ssh", label: "A plain SSH server" }];
let gridPicks = [];
context.PlexoraConnectionModal = {
    open: (options) => {
        modalOpens.push(options);
        return Promise.resolve({ connected: false });
    },
    // The real one fetches the catalogue and builds the same cards the dialog
    // shows. What matters here is that the page asks for them, puts them where
    // its form used to be, and opens the right preset when one is chosen.
    recipeGrid: (onPick) => {
        const grid = makeElement("div");
        grid.className = "connect-recipes";
        CATALOGUE.forEach((recipe) => {
            const card = makeElement("button");
            card.className = "connect-recipe";
            card.textContent = recipe.label;
            card.addEventListener("click", () => {
                gridPicks.push(recipe.id);
                onPick(recipe);
            });
            grid.appendChild(card);
        });
        return Promise.resolve(grid);
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
    snapshot = world([profile("hpc", {
        node: { state: "idle" },
        saved: { description: "Slurm compute cluster" } })]);
    context.pageInit();
    await settle();

    check("a saved server gets a card, named",
          cards().length === 1 && textOf(cards()[0]).indexOf("hpc") >= 0);
    // What kind of machine, not what its address is. A card is a status board
    // for somebody who set this up months ago; `me@hpc` answers a question
    // they are not asking, and the form behind the pencil is where it is
    // asked and where it is read back.
    check("...saying what kind of machine it is, in words",
          textOf(cards()[0]).indexOf("Slurm compute cluster") >= 0);
    // Not on a tooltip either. A hover that shows configuration is still
    // configuration on the card; it is just configuration nobody can find.
    // The tooltip repeats the description, which is clamped to two lines and
    // may therefore be cut.
    check("...with the address nowhere on the card, tooltip included",
          textOf(cards()[0]).indexOf("me@hpc") < 0
          && one(cards()[0], "settings-remote-description").title
             === "Slurm compute cluster");
    // The state is a dot. Its word is the name a screen reader reads and the
    // tooltip a mouse finds -- the same word, from the same map, as the one
    // the connection dialog and the globe use.
    check("...and its state as a dot that is still named",
          one(cards()[0], "settings-remote-dot").getAttribute("aria-label")
              === "Not connected"
          && one(cards()[0], "settings-remote-dot").classList
                 .contains("is-idle"));
    check("...and Connect is what it offers",
          Boolean(buttonSaying(cards()[0], "Connect")));
    // Two things you can do TO the server, as against the one thing you do
    // WITH it. Icons in the head, so a card at rest has one button on it.
    check("...with Edit and Delete as named icons above, not buttons below",
          Boolean(iconSaying(cards()[0], "Edit"))
          && Boolean(iconSaying(cards()[0], "Delete"))
          && buttonSaying(cards()[0], "Edit") === null
          && buttonSaying(cards()[0], "Forget") === null);

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
    // The card used to spell this out -- "Serving files to this Plexora as
    // 'hpc-data'." -- which is a sentence about the runtime on a card whose
    // job is to say which machine this is and whether it is up. The dot says
    // it is up, the button says what to do about that, and the node's own
    // name is next door in Data nodes, which is the section that answers
    // "what is reachable right now".
    check("a connected card says so with its dot, not with a sentence",
          one(cards()[0], "settings-remote-dot").getAttribute("aria-label")
              === "Connected"
          && textOf(cards()[0]).indexOf("Serving files") < 0);
    check("...and offers to end it",
          Boolean(buttonSaying(cards()[0], "Disconnect")));
    posted.length = 0;
    buttonSaying(cards()[0], "Disconnect").click();
    await settle();
    check("...which ends the data node, not somebody's viewer",
          posted.some((p) => p.action === "disconnect" && p.kind === "node"));

    // -- deleting a saved server ----------------------------------------------
    //
    // It asks first, whatever kind of machine it is. That was not worth doing
    // while this was a button in a row of buttons with the word "Forget" on
    // it; it is worth doing for a bin icon eight pixels from a pencil, with no
    // words on either of them.
    confirms = [];
    posted.length = 0;
    confirmAnswer = false;
    iconSaying(cards()[0], "Delete").click();
    await settle();
    check("deleting a server asks first, and says what is not being deleted",
          confirms.length === 1
          && confirms[0].indexOf("Nothing on the machine itself") >= 0);
    check("...and no means no",
          !posted.some((p) => p.action === "forget"));

    confirmAnswer = true;
    iconSaying(cards()[0], "Delete").click();
    await settle();
    check("...and yes drops the profile",
          posted.some((p) => p.action === "forget" && p.name === "hpc"));

    say(world([]));
    await settle();
    check("forgetting the last server leaves the empty note, not a stale card",
          cards().length === 1
          && textOf(cards()[0]).indexOf("No servers saved yet") >= 0);

    // -- adding a server ------------------------------------------------------
    //
    // There is no form here any more. Adding a server means answering
    // questions about somebody else's cluster, and those answers are
    // properties of the SITE -- so the page offers the sites, and the preset's
    // own form asks what genuinely differs. What used to be here drove twelve
    // boxes that asked seven of the same questions with a second set of
    // defaults to keep in step.
    const grid = elementFor("settings_remote_catalogue");
    check("the presets are on the page, where a second form used to be",
          find(grid, "connect-recipe").length === 2);
    check("...drawn by the dialog's own card, not a second copy of it",
          find(grid, "connect-recipes").length === 1);

    modalOpens = [];
    gridPicks = [];
    find(grid, "connect-recipe")[0].click();
    await settle();
    check("choosing one opens that preset's form, not the catalogue again",
          modalOpens.length === 1 && modalOpens[0].view === "recipe"
          && modalOpens[0].recipe === "hms-o2"
          && modalOpens[0].kind === "node");
    check("...and it is the card that was clicked",
          gridPicks.join(",") === "hms-o2");
    check("...with no profile, because this one does not exist yet",
          !modalOpens[0].remote);

    // -- editing a saved server -----------------------------------------------
    //
    // The same form, opened on the preset that composed the profile and handed
    // the profile to fill itself in from. Which preset that is was decided by
    // the server -- `recipes.for_remote` -- including for a profile written
    // before a profile recorded which preset made it.
    modalOpens = [];
    const saved = profile("o2", { node: { state: "idle" } });
    saved.saved = { recipe: "hms-o2", data_dir: "/n/data",
                    forwards: ["8642"], install: true,
                    install_env: "imaging" };
    saved.install = true;
    saved.installEnv = "imaging";
    say(world([saved]));
    await settle();
    // The install switch, the environment it writes to, and the data
    // directory are all on this profile and none of them is on its card. They
    // are settings, they were set on the form, and the form is one click away
    // -- a grid of cards is for picking a machine, not for auditing one.
    check("the settings a profile carries stay off its card",
          textOf(cards()[0]).indexOf("installs Plexora") < 0
          && textOf(cards()[0]).indexOf("imaging") < 0
          && textOf(cards()[0]).indexOf("/n/data") < 0);
    iconSaying(cards()[0], "Edit").click();
    await settle();
    check("editing a server opens the preset it was described in",
          modalOpens.length === 1 && modalOpens[0].view === "recipe"
          && modalOpens[0].recipe === "hms-o2");
    check("...carrying the whole profile, because that is what fills it in",
          modalOpens[0].remote && modalOpens[0].remote.data_dir === "/n/data"
          && modalOpens[0].remote.forwards.join(",") === "8642");

    // A profile the server could name no preset for cannot be left with a
    // button that does nothing. `ssh` fits any host and is what "no idea"
    // looks like -- the server says so too, and this is the belt to its
    // braces.
    modalOpens = [];
    say(world([profile("legacy", { node: { state: "idle" } })]));
    await settle();
    iconSaying(cards()[0], "Edit").click();
    await settle();
    check("...and a profile naming no preset still has a form to open",
          modalOpens.length === 1 && modalOpens[0].recipe === "ssh");

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

    // -- the machine a Google Cloud profile rents -----------------------------
    //
    // Stopping on disconnect is the default, so "stopped" is where one of
    // these profiles rests. A card that could only ever stop things was
    // describing half a lifecycle -- and describing it blind, because it never
    // asked what the machine was doing.
    posted.length = 0;
    vmState = "RUNNING";
    say(world([profile("gcp", { gcloud: CLOUD,
                                saved: { description: "Google Cloud VM" },
                                node: { state: "idle" } })]));
    await settle();
    let gcpCard = cards()[0];
    // What the machine is doing is on the buttons that change it, not on a
    // line of its own. WHICH of Start and Stop is offered already says which
    // way the machine is; the word is there for the tooltip and the screen
    // reader, and it is the same word from the same map as when it was a row.
    check("a rented machine's state is on the button that would change it",
          offered(gcpCard, "Stop VM").title === "This machine is running.");
    check("...and the card itself says only which machine this is",
          textOf(gcpCard).indexOf("Google Cloud VM") >= 0
          && textOf(gcpCard).indexOf("VM running") < 0
          && textOf(gcpCard).indexOf("stopped on exit") < 0);
    check("...with the bucket and the machine type nowhere on it",
          textOf(gcpCard).indexOf("e2-highmem-16") < 0
          && textOf(gcpCard).indexOf("tonsil-images") < 0
          && one(gcpCard, "settings-remote-description").title
                 .indexOf("gs://") < 0);
    check("...having asked Google exactly once to find out",
          posted.filter((p) => p.action === "vmStatus").length === 1);
    check("...and a running one is offered the button that ends the bill",
          Boolean(offered(gcpCard, "Stop VM"))
          && offered(gcpCard, "Start VM") === null);

    // The poll repaints this card every second. Asking Compute Engine on each
    // of those would be a gcloud subprocess per profile per second.
    say(world([profile("gcp", { gcloud: CLOUD, node: { state: "idle" } })]));
    await settle();
    say(world([profile("gcp", { gcloud: CLOUD, node: { state: "idle" } })]));
    await settle();
    check("...and never asks again just because the page repainted",
          posted.filter((p) => p.action === "vmStatus").length === 1);

    posted.length = 0;
    vmState = "TERMINATED";
    say(world([profile("stopped-one", { gcloud: CLOUD,
                                        node: { state: "idle" } })]));
    await settle();
    gcpCard = cards()[0];
    check("a stopped machine says so rather than saying nothing",
          offered(gcpCard, "Start VM").title === "This machine is stopped.");
    check("...and is offered Start instead of a Stop that would do nothing",
          Boolean(offered(gcpCard, "Start VM"))
          && offered(gcpCard, "Stop VM") === null);

    offered(gcpCard, "Start VM").click();
    await settle();
    check("starting it says so, and does not wait a minute to say it",
          posted.some((p) => p.action === "vmStart")
          && textOf(gcpCard).indexOf("Starting plexora-stopped-one") >= 0);
    check("...and the card shows where it is going, not where it was",
          offered(gcpCard, "Stop VM").title === "This machine is starting.");

    posted.length = 0;
    vmState = "missing";
    say(world([profile("no-vm", { gcloud: CLOUD, node: { state: "idle" } })]));
    await settle();
    gcpCard = cards()[0];
    check("a profile whose VM is gone is offered neither Start nor Stop",
          offered(gcpCard, "Start VM") === null
          && offered(gcpCard, "Stop VM") === null);
    check("...nor Delete, there being nothing left to delete",
          offered(gcpCard, "Delete VM…") === null);
    check("...and is still a perfectly good profile to connect",
          Boolean(buttonSaying(gcpCard, "Connect")));

    posted.length = 0;
    vmState = "RUNNING";
    vmSource = "existing";
    say(world([profile("byo", {
        gcloud: Object.assign({}, CLOUD, { vm_source: "existing",
                                           vm_name: "analysis-box" }),
        node: { state: "idle" } })]));
    await settle();
    gcpCard = cards()[0];
    check("a machine the user already runs can still be stopped by hand",
          Boolean(offered(gcpCard, "Stop VM")));
    check("...but is never offered a button that would delete it",
          offered(gcpCard, "Delete VM…") === null);
    vmSource = "plexora";

    // A connected session is proof the machine is up, and it is proof that
    // arrives without asking Google anything.
    posted.length = 0;
    vmState = "TERMINATED";
    say(world([profile("live", { gcloud: CLOUD,
                                 node: { state: "connected",
                                         node: "live-data" } })]));
    await settle();
    check("a connected session is itself proof the VM is running",
          offered(cards()[0], "Stop VM").title === "This machine is running.");

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall settings-remotes checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
