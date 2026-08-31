/**
 * Saying so before a scheduled job ends.
 *
 * A connection running inside a scheduler's job has a deadline nobody is
 * looking at. Slurm does not warn; it kills the allocation, the tunnel goes
 * with it, and the first sign is a tile that will not load an hour into a
 * session. The countdowns on the Settings card and in the navbar panel are for
 * somebody who thinks to check. This is for everybody else, and what it has to
 * get right is when NOT to speak:
 *
 *   - **Nothing at all for a connection with no walltime**, which is most of
 *     them. No timer, no dialog, nothing subscribed that costs anything.
 *   - **Once per session, not once per check.** The watcher runs on a timer;
 *     a dialog per tick would be unusable.
 *   - **Again after a reconnect.** A fresh job is a fresh four hours and a
 *     fresh warning, and the only thing separating it from the old one is that
 *     the remaining time went back UP.
 *   - **Passively.** `PlexoraRemotes` stops polling when everything is
 *     settled, which is the state a job sits in for its whole life; a watcher
 *     subscribing `active` would turn that into a request a second for the
 *     privilege of watching a number go down.
 *   - **Not again once it has been closed**, on this page or the next one. An
 *     ended job reports the same zero for as long as its node is on the map,
 *     so a dismissal that lived in a variable was undone by every reload.
 *   - **Not at all on a zero the server disagrees with.** The countdown is
 *     interpolated off a snapshot that may be an hour old, and a session
 *     started from another tab is invisible from in here until somebody asks.
 *
 * Run directly:  node tests/js/session_expiry_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/sessionExpiry.js");

// -- a DOM small enough to read, with a <dialog> in it ----------------------

function makeElement(tag) {
    const classes = new Set();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        textContent: "",
        children: [],
        parentNode: null,
        open: false,
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
        setAttribute() {},
        getAttribute() { return null; },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
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
        },
        click() { element.dispatchEvent({ type: "click" }); },
        // The two halves of <dialog> this file uses.
        showModal() { element.open = true; },
        close() {
            element.open = false;
            element.dispatchEvent({ type: "close" });
        },
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

function textOf(root) {
    return walk(root).map((n) => n.textContent).join(" ");
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

// -- a clock the test drives ------------------------------------------------

let now = 0;
const intervals = new Map();
let nextTimer = 1;

function setIntervalStub(fn, every) {
    const id = nextTimer++;
    intervals.set(id, { every: every || 1, fn, last: now });
    return id;
}

function clearIntervalStub(id) {
    intervals.delete(id);
}

/** Advance the clock, firing every interval that came due. */
function tick(ms) {
    now += ms;
    Array.from(intervals.entries()).forEach(([id, timer]) => {
        while (intervals.has(id) && timer.last + timer.every <= now) {
            timer.last += timer.every;
            timer.fn();
        }
    });
}

// -- the shared state this watcher reads ------------------------------------

const posted = [];
const modalOpens = [];
const subscriptions = [];
const refreshes = [];
let snapshot = { loaded: true, entries: [], at: 0 };

//: What the next `refresh()` should find, or null to find what is already
//: there. The whole point of the request is that the world may have moved on
//: without this page hearing about it.
let onRefresh = null;

//: One storage, shared by every page context below -- which is what makes the
//: second load a reload of the same browser rather than a different browser.
const stored = new Map();
const localStorageStub = {
    getItem: (key) => (stored.has(key) ? stored.get(key) : null),
    setItem: (key, value) => { stored.set(key, String(value)); },
    removeItem: (key) => { stored.delete(key); },
};

const OPENING = ["connecting", "authenticating", "waiting_for_job",
                 "tunneling", "waiting_for_app"];

const RemotesStub = {
    WARN_SECONDS: 600,
    snapshot: () => snapshot,
    entry: (name) => (snapshot.entries || []).find((e) => e.name === name) || null,
    //: Faithful to the shipped one: what is left, measured against how long
    //: ago the snapshot said it. The formatter and the interpolation are
    //: checked against the real file in tests/js/remote_state_probe.mjs.
    remaining: (entry) => {
        const half = entry && entry.node;
        if (!half || half.timeLeft === null || half.timeLeft === undefined) {
            return null;
        }
        //: The liveness half, faithful to the shipped one: a clock belongs to
        //: an allocation, and a disconnected connection has none.
        if (OPENING.indexOf(half.state) < 0 && half.state !== "connected"
                && !half.registered) {
            return null;
        }
        return Math.max(0, Math.round(half.timeLeft - (now - snapshot.at) / 1000));
    },
    duration: (seconds) => {
        const total = Math.max(0, Math.round(seconds));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const pad = (v) => (v < 10 ? "0" + v : String(v));
        return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
    },
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
    refresh: () => {
        refreshes.push(now);
        if (onRefresh) onRefresh();
        return Promise.resolve(snapshot);
    },
};

function say(entries) {
    snapshot = { loaded: true, entries, at: now };
    subscriptions.filter((s) => s.live).forEach((s) => s.cb(snapshot));
}

function job(name, timeLeft) {
    return {
        name, label: name, target: `me@${name}`, detail: `me@${name}`,
        viewer: { state: "idle" },
        node: { state: "connected", node: name + "-data", registered: null,
                timeLeft: timeLeft, timeLimit: 14400 },
        connected: true, opening: false, prompt: null,
    };
}

// -- load the shipped file --------------------------------------------------

const source = readFileSync(SOURCE, "utf-8");

/** One page context: a fresh script scope over the shared storage and state. */
function load() {
    const body = makeElement("body");
    const context = {
        console,
        setInterval: setIntervalStub,
        clearInterval: clearIntervalStub,
        Promise,
        Math,
        String,
        JSON,
        Object,
        localStorage: localStorageStub,
        document: { createElement: makeElement, body },
    };
    context.window = context;
    context.PlexoraRemotes = RemotesStub;
    context.PlexoraConnectionModal = {
        open: (options) => {
            modalOpens.push(options);
            return Promise.resolve({ connected: true });
        },
    };
    context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };

    createContext(context);
    runInContext(source, context);
    return { context, body };
}

const first = load();
const context = first.context;
const body = first.body;

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

function dialogIn(where) {
    return where.children.find((c) => c.tagName === "DIALOG" && c.open) || null;
}

function dialogNow() {
    return dialogIn(body);
}

async function main() {
    // -- 1. nothing on a clock, nothing running -------------------------------
    say([{ name: "plain", label: "plain", node: { state: "connected",
                                                  node: "plain-data",
                                                  timeLeft: null } }]);
    context.pageInit();
    await settle();
    check("it watches passively, so a settled job costs no polling",
          subscriptions.length === 1 && subscriptions[0].active === false);
    check("a connection with no walltime starts no timer and says nothing",
          intervals.size === 0 && dialogNow() === null);

    // -- 2. plenty of time left is not a thing to say -------------------------
    say([job("gpu", 4 * 3600)]);
    await settle();
    check("a job with hours left is watched, quietly",
          intervals.size === 1 && dialogNow() === null);

    // -- 3. the ten-minute warning --------------------------------------------
    say([job("gpu", 9 * 60)]);
    await settle();
    const warned = dialogNow();
    check("ten minutes out, it says so", Boolean(warned));
    check("...naming the machine, not just 'a connection'",
          textOf(warned).indexOf("gpu") >= 0);
    check("...with the number somebody is deciding on, live",
          textOf(warned).indexOf("9:00") >= 0);
    check("...and it is a countdown, not a static string",
          (tick(60000), textOf(warned).indexOf("8:00") >= 0));
    check("...saying what is NOT at risk, which is everything on this computer",
          textOf(warned).indexOf("projects, ROIs, figures and gates") >= 0);

    // -- 4. once, not once per check ------------------------------------------
    warned.close();
    await settle();
    tick(60000);
    await settle();
    check("having been told once, it is not told again on the next check",
          dialogNow() === null);

    // -- 5. a fresh job, and the button ---------------------------------------
    posted.length = 0;
    modalOpens.length = 0;
    say([job("gpu", 4 * 3600)]);       // reconnected: the clock went back up
    await settle();
    say([job("gpu", 3 * 60)]);
    await settle();
    const again = dialogNow();
    check("a fresh job is warned about again, the clock having gone back up",
          Boolean(again));
    buttonSaying(again, "Start a new session").click();
    await settle();
    check("...and starting a new session ends the old one first",
          posted.length === 1 && posted[0].action === "disconnect"
          && posted[0].kind === "node");
    check("...then opens the one dialog that connects a machine",
          modalOpens.length === 1 && modalOpens[0].name === "gpu"
          && modalOpens[0].kind === "node");
    check("...and closes itself on the way, leaving one window on screen",
          dialogNow() === null);

    // -- 6. and when it is actually gone --------------------------------------
    say([job("gpu", 0)]);
    await settle();
    const over = dialogNow();
    check("a job that has ended is said out loud even after the warning was",
          Boolean(over) && textOf(over).indexOf("run out of time") >= 0);
    check("...in the past tense, because there is nothing left to save",
          textOf(over).indexOf("has ended the job") >= 0);
    over.close();
    await settle();
    tick(60000);
    await settle();
    check("...and that too is said once", dialogNow() === null);

    // -- 6b. a warning about a connection that has since been closed ----------
    //
    // Somebody who reads "about to run out of time" and goes and disconnects
    // from the globe has answered the question. Leaving the dialog on screen
    // urging a new session for a machine that already has none is worse than
    // never having asked -- and the countdown inside it would go on running,
    // because the dialog ticks off the snapshot rather than off a poll.
    say([job("gpu", 4 * 3600)]);
    await settle();
    say([job("gpu", 5 * 60)]);
    await settle();
    check("a connection close to the end is warned about",
          Boolean(dialogNow()));
    const closed = job("gpu", 5 * 60);
    closed.node.state = "exited";
    closed.node.node = null;
    say([closed]);
    tick(1000);
    await settle();
    check("...and disconnecting it takes the warning away with it",
          dialogNow() === null);

    // -- 7. and it stops when there is nothing to watch -----------------------
    say([{ name: "plain", label: "plain", node: { state: "idle",
                                                  timeLeft: null } }]);
    await settle();
    check("with no clock left anywhere, the timer is stopped",
          intervals.size === 0);

    // -- 8. and closing it means closed, on the next page too -----------------
    //
    // An ended job goes on reporting the same zero for as long as its node is
    // on the map: the registry entry outlives the allocation, and `time_left`
    // floors at zero rather than becoming null. A dismissal kept in a variable
    // met that unchanged fact again on every reload and in every second tab,
    // which is a dialog that cannot be closed, only postponed.
    const asked = refreshes.length;
    say([job("gpu", 0)]);
    await settle();
    const ended = dialogNow();
    check("an ended job is said out loud, the server having been asked first",
          Boolean(ended) && refreshes.length === asked + 1);
    ended.close();
    await settle();

    const reloaded = load();
    reloaded.context.pageInit();
    await settle();
    tick(60000);
    await settle();
    check("...and a reload does not announce the same ended job again",
          dialogIn(reloaded.body) === null && dialogNow() === null);

    // -- 9. a zero this page cannot vouch for ---------------------------------
    //
    // The countdown is interpolated off the last snapshot, and the poll stops
    // once everything is settled -- so a tab left open across a session
    // started from Settings, another tab or the command line counts its way
    // down to zero on numbers that are an hour out of date. Asking once at the
    // transition is the difference between that and telling somebody the
    // machine they are working on has gone.
    onRefresh = () => { onRefresh = null; say([job("cpu", 4 * 3600)]); };
    say([job("cpu", 0)]);
    await settle();
    check("a zero the server disagrees with is not announced at all",
          dialogNow() === null && dialogIn(reloaded.body) === null);
    tick(60000);
    await settle();
    check("...and the machine goes back to being watched quietly",
          dialogNow() === null && dialogIn(reloaded.body) === null);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall session-expiry checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
