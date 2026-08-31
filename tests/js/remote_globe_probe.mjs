/**
 * The navbar's connection icon, and what it costs to have it there.
 *
 * It is on every page including the viewer, which is the whole difficulty:
 *
 *   1. **Closed and settled costs nothing.** It subscribes passively, so a
 *      connected session watched only by this icon polls at nothing. An icon
 *      that cost a request a second for the privilege of being grey would not
 *      be worth having on the viewer at all.
 *   2. **Opening it starts watching; closing it stops.** Including every
 *      listener it hangs on the document, or a viewer that has had the panel
 *      open once keeps handling clicks for a panel that is gone.
 *   3. **The panel goes through the portal**, or a fullscreen viewer's
 *      ::backdrop hides it where no z-index can reach.
 *   4. **It says which machine the picture is coming from** — the one thing
 *      here that the Settings page cannot say, matched on the NODE's name
 *      rather than the profile's.
 *   5. **Session state and health are different claims.** "Connected" is what
 *      Plexora did; "Healthy" is what the machine said when we asked just now.
 *      The gap between them is a slept laptop or an expired job, and it is the
 *      whole reason the second line exists.
 *   6. **It holds nothing identifying and nothing typeable.** No usernames, no
 *      addresses, no fields — this is a status board with a switch on it, and
 *      the way to configure a machine is a link to the page that does that.
 *
 * Run directly:  node tests/js/remote_globe_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/remoteGlobe.js");

// -- a DOM small enough to read ---------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        textContent: "",
        hidden: false,
        children: [],
        parentNode: null,
        style: {},
        offsetWidth: 320,
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
        contains(other) {
            if (other === element) return true;
            return element.children.some((c) => c.contains && c.contains(other));
        },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener(type, handler) {
            const kept = (listeners.get(type) || []).filter((h) => h !== handler);
            listeners.set(type, kept);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
            return true;
        },
        click() { element.dispatchEvent({ type: "click" }); },
        focus() {},
        getBoundingClientRect: () => ({ top: 0, bottom: 40, left: 900,
                                        right: 940, width: 40, height: 40 }),
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
    return walk(root).map((n) => n.textContent).join(" ");
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

// -- the document, and how many listeners are on it -------------------------

const documentListeners = { keydown: 0, mousedown: 0 };
const body = makeElement("body");

// -- the shared state this icon reads ---------------------------------------

const OPENING = ["connecting", "authenticating", "waiting_for_job",
                 "tunneling", "waiting_for_app"];
const posted = [];
const subscriptions = [];
let snapshot = null;
let modalOpens = [];

const RemotesStub = {
    OPENING,
    isOpening: (state) => OPENING.indexOf(state) >= 0,
    label: (state) => ({ waiting_for_job: "Queued",
                         connecting: "Connecting" }[state] || state),
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
    entry: (name) => (snapshot.entries || []).find((e) => e.name === name) || null,
    subscribe: (cb, options = {}) => {
        const record = { cb, active: Boolean(options.active), live: true };
        subscriptions.push(record);
        cb(snapshot);
        return () => { record.live = false; };
    },
    disconnect: (name, kind) => {
        posted.push({ action: "disconnect", name, kind });
        return Promise.resolve({});
    },
};

function liveSubscriptions() {
    return subscriptions.filter((s) => s.live);
}

function say(next) {
    snapshot = next;
    liveSubscriptions().forEach((s) => s.cb(snapshot));
}

function profile(name, opts = {}) {
    const viewer = Object.assign({ state: "idle", phase: "", error: null,
                                  prompt: null, url: null, log: [] },
                                opts.viewer || {});
    const node = Object.assign({ state: "idle", phase: "", error: null,
                                 prompt: null, node: null, registered: null,
                                 timeLeft: null, timeLimit: null },
                               opts.node || {});
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        queued: Boolean(opts.queued), viewer, node,
        connected: viewer.state === "connected" || Boolean(node.node),
        opening: RemotesStub.isOpening(viewer.state)
                 || RemotesStub.isOpening(node.state),
        prompt: null,
    };
}

function world(entries, extra = {}) {
    return Object.assign({ loaded: true, error: null, remotes: [], places: [],
                           entries, clientNode: "", serverIsRemote: false,
                           server: null, focus: {} }, extra);
}

// -- routing, asked once per panel open -------------------------------------

const fetched = [];
const dispatched = [];
let routes = {};
let health = {};
let heldRouting = null;

function fetchStub(url) {
    fetched.push(url);
    if (url.indexOf("remote_health") >= 0) {
        return Promise.resolve({
            ok: true, json: () => Promise.resolve({ health }),
        });
    }
    return Promise.resolve({
        ok: true, json: () => Promise.resolve({ routes }),
    });
}

// -- load the shipped file --------------------------------------------------

const portaled = [];
const context = {
    console,
    setTimeout,
    clearTimeout,
    Promise,
    Math,
    encodeURIComponent,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    innerWidth: 1400,
    document: {
        createElement: makeElement,
        body,
        getElementById: () => globe,
        addEventListener: (type) => { documentListeners[type] += 1; },
        removeEventListener: (type) => { documentListeners[type] -= 1; },
    },
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: (event) => { dispatched.push(event); return true; },
    CustomEvent: class {
        constructor(type, options) {
            this.type = type;
            this.detail = (options || {}).detail;
        }
    },
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
//: What this page's tiles were built from, per PlexoraRouting.held. Null --
//: the state of every page but a viewer that resolved routing -- until a
//: check sets it.
context.PlexoraRouting = { held: () => heldRouting };
context.PopoverPortal = {
    attach: (el) => { portaled.push(el); body.appendChild(el); return el; },
    detach: (el) => {
        const at = portaled.indexOf(el);
        if (at >= 0) portaled.splice(at, 1);
        el.remove();
    },
};
context.PlexoraConnectionModal = {
    open: (options) => {
        modalOpens.push(options);
        return Promise.resolve({ connected: false });
    },
};
context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };
context.flaskVariables = { datasource: "study" };

const globe = makeElement("button");
createContext(context);
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

function panelNow() {
    return portaled[0] || null;
}

async function main() {
    snapshot = world([profile("hpc", { node: { state: "idle" } })]);
    const teardown = context.pageInit();

    // -- 1. closed and settled costs nothing ---------------------------------
    check("the icon watches passively, so nothing is polled while it is grey",
          liveSubscriptions().length === 1
          && liveSubscriptions()[0].active === false);
    check("...and says what it is, rather than being an unexplained icon",
          globe.classList.contains("is-live") === false
          && globe.getAttribute("data-tooltip") === "Remote connections");
    check("nothing is fetched until somebody opens it", fetched.length === 0);

    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } })]));
    check("a connection lights it, and the tooltip names which",
          globe.classList.contains("is-live") === true
          && globe.getAttribute("data-tooltip") === "hpc · Connected");

    say(world([profile("hpc", { node: { state: "waiting_for_job" } })]));
    check("...and one on its way makes it the one moving thing in the navbar",
          globe.classList.contains("is-opening") === true
          && globe.getAttribute("data-tooltip") === "Connecting to “hpc”…");

    // Marked, not shouted: the connection failed, which is a thing to look at
    // rather than a thing that has broken the session in front of somebody.
    say(world([profile("hpc", { node: { state: "failed",
                                        error: "Permission denied" } })]));
    check("...and a failure marks the icon without taking over the navbar",
          globe.classList.contains("is-problem") === true
          && globe.classList.contains("is-live") === false);

    // -- 2/3. opening and closing ---------------------------------------------
    routes = { image: { node: "hpc-data" } };
    health = { hpc: { state: "healthy", ms: 42, detail: "" } };
    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } }),
               profile("o2", { node: { state: "idle" } })],
              { serverIsRemote: true, clientNode: "laptop" }));
    globe.click();
    await settle();
    check("opening the panel starts watching properly",
          liveSubscriptions().some((s) => s.active === true));
    check("...and puts it through the portal, not onto <body>",
          portaled.length === 1);
    check("...positioned under the icon", panelNow().style.top === "48px");
    check("...and marked as a dialog for anything reading the page",
          globe.getAttribute("aria-expanded") === "true"
          && panelNow().getAttribute("role") === "dialog");

    // The list leads with the machine Plexora is on, then one row per saved
    // machine. `rows` stays the REMOTE ones so every index below still means
    // what it did before the local row existed.
    const allRows = find(panelNow(), "remote-conn");
    const localRow = allRows.filter((r) => r.classList.contains("is-local"))[0];
    const rows = allRows.filter((r) => !r.classList.contains("is-local"));

    // -- 7. where the viewer is reading from, when that is nowhere remote -----
    check("the machine Plexora is on leads the list",
          Boolean(localRow) && allRows[0] === localRow);
    // This fixture is a Plexora running somewhere else, so the row names the
    // SERVER rather than claiming to be the computer in front of the user --
    // which would be the one lie this row could tell.
    check("...naming the server when Plexora is not on the user's own machine",
          one(localRow, "remote-conn-name").textContent
          === "This Plexora server");
    check("...called Local rather than Connected, being no connection at all",
          textOf(one(localRow, "remote-conn-status")).indexOf("Local") >= 0);
    check("...claiming no latency, because no round trip was made",
          one(localRow, "remote-conn-latency").textContent === "—");
    check("...and offering nothing to connect or disconnect",
          find(localRow, "remote-conn-act").length === 0);
    // The image in this fixture comes from a node, so the local monitor is
    // dark and the node's is lit -- exactly one in the list, either way.
    check("...its monitor dark while the picture comes from a node",
          !one(localRow, "remote-conn-screen").classList
              .contains("is-attached"));

    check("one row per saved machine, and nothing else in the list",
          rows.length === 2
          && one(rows[0], "remote-conn-name").textContent === "hpc");
    check("...saying what it is doing, in a word",
          textOf(one(rows[0], "remote-conn-status")).indexOf("Connected") >= 0
          && textOf(one(rows[1], "remote-conn-status"))
              .indexOf("Disconnected") >= 0);

    // -- 6. nothing identifying, nothing typeable ------------------------------
    check("no address, username or ssh setting is shown here",
          textOf(panelNow()).indexOf("me@hpc") < 0);
    check("...and nothing on it can be typed into",
          walk(panelNow()).every((n) => n.tagName !== "INPUT"
                                        && n.tagName !== "TEXTAREA"));

    // -- 5. answering now is not the same claim as connected -------------------
    check("the health of an open node is asked once, when the panel opens",
          fetched.filter((f) => f.indexOf("remote_health") >= 0).length === 1);
    check("...and reported beside the round trip it took",
          textOf(one(rows[0], "remote-conn-health")).indexOf("Healthy") >= 0
          && one(rows[0], "remote-conn-latency").textContent === "42 ms");
    check("a machine with nothing open is not probed, and claims no latency",
          textOf(one(rows[1], "remote-conn-health")).indexOf("Unknown") >= 0
          && one(rows[1], "remote-conn-latency").textContent === "—");

    // A data node outlives the process that started it: after a restart the
    // registry still holds it and no session does. The probe answers for it,
    // and reading the session half first is what threw that answer away and
    // reported "Unknown" about a node the rest of the app was failing to
    // reach. "Nothing there to ask about" is the absence of an ANSWER, not
    // the absence of a session.
    check("a node that answers is Connected, whoever started the tunnel",
          textOf(one(rows[0], "remote-conn-status")).indexOf("Connected") >= 0);

    const healthOf = context.PlexoraRemoteGlobe.healthOf;
    const orphan = { name: "O2", node: { node: null } };
    check("a node left on the map by a dead session still reports its state",
          healthOf(orphan, { O2: { state: "unreachable", ms: null,
                                   detail: "Connection refused" } }).word
          === "Not answering");
    check("...while a machine that has never been up is still Unknown",
          healthOf(orphan, {}).word === "Unknown");
    // The state that exists because "Healthy" was once said about a node the
    // open project could not read a single tile from. It is neither of the
    // other two: the machine IS answering, so "Not answering" is false, and
    // the server knows exactly what is wrong, so "Unknown" throws that away.
    const behind = healthOf(orphan, { O2: { state: "stale", ms: null,
                                            detail: "Reload the project." } });
    check("a node the project is still addressing at its old port says so",
          behind.word === "Reconnected" && behind.cls === "is-degraded"
          && behind.detail === "Reload the project.");
    // The BROWSER's own version of the same verdict. The server's "stale"
    // heals itself on its next proxied call, while the page's direct tile
    // URLs stay wherever the tiles on screen were fetched from -- so a
    // healthy probe is overruled when this page still holds a retired
    // address for that machine.
    const repointed = healthOf(
        { name: "O2", node: { node: "O2-data" } },
        { O2: { state: "healthy", ms: 4, detail: "" } },
        { "O2-data": true });
    check("a healthy probe is overruled when this page holds a retired address",
          repointed.word === "Reconnected" && repointed.cls === "is-degraded");

    // -- 4. which machine the picture is coming from -------------------------
    check("the routing is asked once, when the panel opens",
          fetched.filter((f) => f.indexOf("resource_routing") >= 0).length === 1);
    check("...and the machine the image comes from is the one marked",
          one(rows[0], "remote-conn-screen").classList.contains("is-attached")
          && !one(rows[1], "remote-conn-screen").classList
              .contains("is-attached"));
    check("...in words as well, for anything that cannot see an icon",
          one(rows[0], "remote-conn-screen").getAttribute("aria-label")
          === "Attached to viewer"
          && one(rows[1], "remote-conn-screen").getAttribute("aria-label")
          === "Not attached to viewer");

    // -- what it offers -------------------------------------------------------
    posted.length = 0;
    check("a connected machine can be disconnected from here",
          one(rows[0], "remote-conn-act").getAttribute("aria-label")
          === "Disconnect hpc");
    one(rows[0], "remote-conn-act").click();
    await settle();
    check("...and that ends the DATA NODE, not somebody's viewer",
          posted.some((p) => p.action === "disconnect" && p.kind === "node"));

    modalOpens = [];
    one(rows[1], "remote-conn-act").click();
    await settle();
    check("connecting one goes through the connection dialog",
          modalOpens.length === 1 && modalOpens[0].name === "o2"
          && modalOpens[0].kind === "node");
    check("...and closes the panel behind it, so nothing is left watching",
          portaled.length === 0
          && liveSubscriptions().every((s) => s.active === false));

    // -- 2, the other half: everything comes back -----------------------------
    check("closing takes its document listeners with it",
          documentListeners.keydown === 0 && documentListeners.mousedown === 0);

    // -- the clock on a scheduled job ----------------------------------------
    //
    // Only on the rows that have one. Most connections are not inside a job,
    // and an empty slot on every other row would spend the width of a
    // deliberately compact panel saying nothing.
    routes = {};
    health = { gpu: { state: "healthy", ms: 6, detail: "" },
               plain: { state: "healthy", ms: 7, detail: "" } };
    say(world([profile("gpu", { node: { state: "connected", node: "gpu-data",
                                        timeLeft: 7382 } }),
               profile("plain", { node: { state: "connected",
                                          node: "plain-data" } })]));
    globe.click();
    await settle();
    const clocked = find(panelNow(), "remote-conn")
        .filter((r) => !r.classList.contains("is-local"));
    check("a connection inside a job says how long it has left",
          textOf(one(clocked[0], "remote-conn-time")).indexOf("2:03:02") >= 0);
    check("...and one that is not on a clock says nothing about time",
          one(clocked[1], "remote-conn-time") === null);
    check("...and an hour out it is a fact, not yet a warning",
          !one(clocked[0], "remote-conn-time").classList.contains("is-urgent"));
    globe.click();
    await settle();

    // Amber, and only in the last ten minutes. Nothing has gone wrong -- the
    // job is doing exactly what it was asked to do -- so this marks the row
    // rather than alarming anybody. The dialog that interrupts is
    // services/sessionExpiry.js.
    say(world([profile("gpu", { node: { state: "connected", node: "gpu-data",
                                        timeLeft: 240 } })]));
    globe.click();
    await settle();
    const nearly = find(panelNow(), "remote-conn")
        .filter((r) => !r.classList.contains("is-local"))[0];
    check("the last ten minutes are marked on the row",
          one(nearly, "remote-conn-time").classList.contains("is-urgent")
          && textOf(one(nearly, "remote-conn-time")).indexOf("4:00") >= 0);
    globe.click();
    await settle();

    say(world([profile("gpu", { node: { state: "connected", node: "gpu-data",
                                        timeLeft: 0 } })]));
    globe.click();
    await settle();
    const gone = find(panelNow(), "remote-conn")
        .filter((r) => !r.classList.contains("is-local"))[0];
    check("...and a job that has ended says so in words, not as 0:00",
          textOf(one(gone, "remote-conn-time")).indexOf("Out of time") >= 0);
    globe.click();
    await settle();

    // -- 4, the other half: an empty name is not a match ----------------------
    //
    // A data node outlives the process that started it, so after a restart the
    // registry holds it and no session does -- `node.node` is empty for a
    // machine that is up and answering. A LOCAL project routes nowhere, so the
    // routing name is empty too. Matching one against the other found null on
    // both sides and called it a match: the panel reported the viewer attached
    // to a cluster while the picture was being read off this laptop, and lit
    // the local row saying the opposite in the same list.
    routes = {};
    health = { hpc: { state: "healthy", ms: 4, detail: "" } };
    say(world([profile("hpc", { node: { state: "idle",
                                        registered: "hpc-data" } })]));
    globe.click();
    await settle();
    const localOnly = find(panelNow(), "remote-conn");
    const attachedIn = (rowList, local) => rowList
        .filter((r) => r.classList.contains("is-local") === local)
        .map((r) => one(r, "remote-conn-screen").classList.contains("is-attached"));
    check("a machine sharing no name with the routing is not the source",
          attachedIn(localOnly, false).every((lit) => lit === false));
    check("...and the local row is the lit one, being where the picture is",
          attachedIn(localOnly, true)[0] === true);
    globe.click();
    await settle();

    // The same field, the other way round: a node whose session died is still
    // the machine the picture comes from, and the registry is now the only
    // thing that knows what it is called.
    routes = { image: { node: "hpc-data" } };
    say(world([profile("hpc", { node: { state: "idle",
                                        registered: "hpc-data" } })]));
    globe.click();
    await settle();
    const orphaned = find(panelNow(), "remote-conn");
    check("a node that outlived its session is still matched by name",
          attachedIn(orphaned, false)[0] === true);
    check("...leaving the local row dark, exactly one lit either way",
          attachedIn(orphaned, true)[0] === false);
    globe.click();
    await settle();

    // -- the page still pointing at a node's old address -----------------------
    //
    // A reconnect lands the tunnel on a new port with a new token, and the
    // open viewer's tiles keep the old ones. Opening this panel is the one
    // moment a page that missed the reconnect (another tab made it) finds
    // out: the row says so, and the repair event is fired so the viewer
    // repoints itself rather than asking somebody to reload.
    heldRouting = { routes: { image: {
        mode: "direct", node: "hpc-data", appendKey: true,
        base: "http://127.0.0.1:58808/node/v1/image/slide/tile/",
        query: "t=old",
    } } };
    routes = { image: {
        node: "hpc-data",
        tile_base: "http://127.0.0.1:51837/node/v1/image/slide/tile/",
        query: "t=new",
    } };
    health = { hpc: { state: "healthy", ms: 4, detail: "" } };
    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } })]));
    globe.click();
    await settle();
    const repointedRow = find(panelNow(), "remote-conn")
        .filter((r) => !r.classList.contains("is-local"))[0];
    check("a healthy machine the page still reads at its old address says so",
          textOf(one(repointedRow, "remote-conn-health"))
              .indexOf("Reconnected") >= 0);
    check("...and the repair event is fired so the viewer repoints itself",
          dispatched.some((event) =>
              event.type === "plexora:remote-nodes-changed"
              && (event.detail.changed || []).some(
                  (c) => c.node === "hpc-data")));
    globe.click();
    await settle();
    heldRouting = null;

    // -- a detached computer says what to run, where ---------------------------
    say(world([], { serverIsRemote: true, clientNode: "" }));
    globe.click();
    await settle();
    check("a computer the server cannot read says how to attach it",
          textOf(panelNow()).indexOf("plexora connect") >= 0);
    // A link, not a dialog: adding a machine means typing an address, and this
    // panel deliberately holds nothing typeable.
    check("...and adding a machine goes to the page that configures machines",
          one(panelNow(), "remote-panel-add").href === "/settings#remotes");

    // Close it again, so what follows is about mounting and nothing else.
    globe.click();
    await settle();

    // The navbar is not swapped markup: the router replaces the page BELOW it,
    // and PlexoraPage runs every controller again against the very same button.
    // Without a guard each navigation would add a second click handler, and one
    // click would open the panel and close it in the same breath.
    const before = liveSubscriptions().length;
    context.pageInit();
    context.pageInit();
    check("navigating does not mount a second globe onto the same button",
          liveSubscriptions().length === before);
    globe.click();
    await settle();
    check("...so one click still opens the panel, rather than toggling twice",
          portaled.length === 1);
    globe.click();
    await settle();
    check("...and a second click closes it, leaving nothing behind",
          portaled.length === 0
          && documentListeners.keydown === 0
          && documentListeners.mousedown === 0);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall remote-globe checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
