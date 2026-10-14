/**
 * Each of the three views says, quietly, when it is loading.
 *
 * views/paneStatus.js runs here against fake elements and a clock this probe
 * owns; statuses are handed to `onStatus` directly (and once through the
 * event, to pin the wiring). Pinned:
 *
 * - nothing is built while every view is idle;
 * - a load that lands inside DELAY_MS never shows the pill;
 * - a longer one shows the breathing dot, marks its pane aria-busy, and for
 *   the plane makes the depth strip's thumb breathe from the same status;
 * - a preview with counts that mean something shows the ring, and its
 *   fraction never goes back within a revision, starting again on a new one;
 * - once up, the pill stays MIN_VISIBLE_MS, then fades over FADE_MS;
 * - an error shows at once, with a retry button that calls `retry` once;
 * - the three views are independent, mount lazily, and unmount leaves no
 *   timer behind.
 *
 * Run directly:  node tests/js/pane_status_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

// -- a clock, frames and a small DOM ------------------------------------------------

const clock = { now: 0, timers: [], frames: [], nextId: 1 };
function setTimeoutFake(fn, ms) {
    const timer = { fn, at: clock.now + Math.max(0, ms || 0), id: clock.nextId++ };
    clock.timers.push(timer);
    return timer.id;
}
function clearTimeoutFake(id) {
    const timer = clock.timers.find((t) => t.id === id);
    if (timer) timer.cancelled = true;
}
function requestAnimationFrameFake(fn) {
    clock.frames.push(fn);
    return clock.frames.length;
}
/** Live timers (not fired, not cleared). */
function pending() {
    return clock.timers.filter((t) => !t.cancelled && !t.done).length;
}
/** Move the clock: the frames asked for so far run first (the next frame),
 *  then every timer due, in time order, each at its own time. */
function advance(ms) {
    const frames = clock.frames.splice(0);
    for (const fn of frames) fn(clock.now);
    const until = clock.now + ms;
    for (;;) {
        const due = clock.timers
            .filter((t) => !t.cancelled && !t.done && t.at <= until)
            .sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        clock.now = Math.max(clock.now, due.at);
        due.done = true;
        due.fn();
    }
    clock.now = until;
}

function element(tag = "div") {
    const attrs = new Map();
    const classes = new Set();
    const listeners = {};
    const node = {
        tagName: tag.toUpperCase(), children: [], parentNode: null, hidden: false,
        style: {}, dataset: {},
        get className() { return [...classes].join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...names) => names.forEach((c) => classes.add(c)),
            remove: (...names) => names.forEach((c) => classes.delete(c)),
            toggle(name, on) {
                const want = on === undefined ? !classes.has(name) : Boolean(on);
                if (want) classes.add(name); else classes.delete(name);
                return want;
            },
            contains: (name) => classes.has(name),
        },
        setAttribute(key, value) {
            if (key === "class") node.className = value;    // as an SVG element is classed
            attrs.set(key, String(value));
        },
        getAttribute(key) { return attrs.has(key) ? attrs.get(key) : null; },
        hasAttribute(key) { return attrs.has(key); },
        removeAttribute(key) { attrs.delete(key); },
        appendChild(child) {
            child.remove();
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        append(...kids) { kids.forEach((kid) => node.appendChild(kid)); },
        remove() {
            const parent = node.parentNode;
            if (!parent) return;
            parent.children.splice(parent.children.indexOf(node), 1);
            node.parentNode = null;
        },
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        removeEventListener(name, fn) {
            listeners[name] = (listeners[name] || []).filter((f) => f !== fn);
        },
        /** A click as the browser would deliver it. */
        click() {
            let stopped = false;
            const event = { stopPropagation() { stopped = true; }, preventDefault() {} };
            for (const fn of listeners.click || []) fn(event);
            return { stopped };
        },
    };
    return node;
}

const documentFake = {
    createElement: (tag) => element(tag),
    createElementNS: (ns, tag) => element(tag),
};

/** A window: only what the module reads, plus an event target. */
const windowListeners = {};
const box = {
    console,
    Math, Number, String, Object, Date,
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    requestAnimationFrame: requestAnimationFrameFake,
    performance: { now: () => clock.now },
    document: documentFake,
    addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); },
    removeEventListener(name, fn) {
        windowListeners[name] = (windowListeners[name] || []).filter((f) => f !== fn);
    },
    dispatchEvent(event) {
        for (const fn of windowListeners[event.type] || []) fn(event);
        return true;
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "views/paneStatus.js"), "utf8"), context);
const { PaneStatus, DELAY_MS, MIN_VISIBLE_MS, FADE_MS, meaningful } = box.PlexoraPaneStatus;

const ARC = 34.56;

/** A PaneStatus over three hosts and a strip, with every clock injected. */
function setup({ mainHost = element(), xzHost = element(), yzHost = element() } = {}) {
    const hosts = { main: mainHost, xz: xzHost, yz: yzHost };
    const busy = { main: element(), xz: xzHost, yz: yzHost };
    const strip = {
        calls: [], loadingSource: null,
        paintLoading(on) { strip.calls.push(Boolean(on)); },
    };
    const status = new PaneStatus({
        mounts: { main: () => hosts.main, xz: () => hosts.xz, yz: () => hosts.yz },
        busyHosts: { main: () => busy.main, xz: () => busy.xz, yz: () => busy.yz },
        strip: () => strip,
        now: () => clock.now,
        setTimeout: setTimeoutFake,
        clearTimeout: clearTimeoutFake,
        requestAnimationFrame: requestAnimationFrameFake,
        target: box,
        document: documentFake,
    });
    status.mount();
    return { status, hosts, busy, strip };
}

const pill = (host) => host?.children.find((c) => c.className.includes("plx-pane-status")) || null;
const part = (node, name) => node.children.find((c) => c.className.includes(name));
const offset = (node) => parseFloat(part(part(node, "plx-pane-status-ring"), "plx-pane-status-arc").style.strokeDashoffset);
const showsDot = (node) => !part(node, "plx-pane-status-dot").hidden
    && part(node, "plx-pane-status-ring").hasAttribute("hidden");
const showsRing = (node) => part(node, "plx-pane-status-dot").hidden
    && !part(node, "plx-pane-status-ring").hasAttribute("hidden");
const on = (node) => Boolean(node) && !node.hidden && node.classList.contains("is-visible");
const st = (surface, phase, extra = {}) => ({ surface, rev: 1, spec: "z1", phase, loaded: 0,
                                             needed: 0, failed: 0, level: 0, since: clock.now,
                                             retry: null, ...extra });

// -- constants, and the counts worth a ring ------------------------------------------

check("the timings are the plan's: 220 ms delay, 300 ms hold, 150 ms fade",
      DELAY_MS === 220 && MIN_VISIBLE_MS === 300 && FADE_MS === 150);
check("a ring only for at least two tiles and a count that is a count",
      meaningful(2, 8) && meaningful(0, 2) && meaningful(8, 8) && !meaningful(0, 0)
      && !meaningful(1, 1) && !meaningful(9, 8) && !meaningful(-1, 8) && !meaningful(NaN, 8));

// -- idle builds nothing --------------------------------------------------------------

{
    const { status, hosts, strip } = setup();
    status.onStatus(st("main", "idle"));
    status.onStatus(st("xz", "idle"));
    advance(1000);
    check("nothing is built or mounted while every view is idle",
          hosts.main.children.length === 0 && hosts.xz.children.length === 0
          && status.state("main").shows === 0 && pending() === 0 && strip.calls.every((c) => c === false));
    status.unmount();
}

// -- a load that lands in a blink never shows ---------------------------------------

{
    const { status, hosts } = setup();
    status.onStatus(st("main", "fetching"));
    advance(150);
    status.onStatus(st("main", "idle"));
    advance(1000);
    const node = pill(hosts.main);
    check("fetching then idle inside 150 ms never shows the pill",
          status.state("main").shows === 0 && !status.state("main").visible
          && node && node.hidden && !node.classList.contains("is-visible"));
    status.unmount();
}

// -- a slower one: the dot, aria-busy, the strip's thumb -----------------------------

{
    const { status, hosts, busy, strip } = setup();
    status.onStatus(st("main", "fetching"));
    advance(DELAY_MS - 1);
    const node = pill(hosts.main);
    check("not before the delay", node && node.hidden && !status.state("main").visible);
    advance(1);
    advance(16);                                     // the frame that fades it up
    check("fetching for 220 ms shows the breathing dot, phase on the pill",
          on(node) && showsDot(node) && node.dataset.phase === "fetching"
          && node.className.includes("is-main") && node.getAttribute("aria-hidden") === "true"
          && status.state("main").shows === 1 && status.state("main").phase === "fetching");
    check("the plane's host is aria-busy and the strip's thumb breathes from the same status",
          busy.main.getAttribute("aria-busy") === "true" && strip.calls.at(-1) === true
          && typeof strip.loadingSource === "function" && strip.loadingSource() === true);

    // -- the ring --------------------------------------------------------------------
    status.onStatus(st("main", "preview", { loaded: 2, needed: 8 }));
    check("a preview at 2 of 8 shows the ring a quarter round, not the dot; the thumb stops",
          showsRing(node) && node.classList.contains("has-ring")
          && Math.abs(offset(node) - ARC * 0.75) <= 0.05 && strip.calls.at(-1) === false
          && strip.loadingSource() === false && Math.abs(status.state("main").ratio - 0.25) < 1e-9,
          String(offset(node)));
    status.onStatus(st("main", "preview", { loaded: 3, needed: 24 }));
    check("more tiles needed at a finer level never takes the ring back within a revision",
          showsRing(node) && Math.abs(offset(node) - ARC * 0.75) <= 0.05
          && Math.abs(status.state("main").ratio - 0.25) < 1e-9, String(offset(node)));
    status.onStatus(st("main", "refining", { loaded: 18, needed: 24 }));
    check("refining keeps the ring, and it moves on",
          showsRing(node) && node.dataset.phase === "refining"
          && Math.abs(offset(node) - ARC * 0.25) <= 0.05, String(offset(node)));
    status.onStatus(st("main", "preview", { rev: 2, spec: "z2", loaded: 1, needed: 8 }));
    check("a new revision starts the ring again",
          showsRing(node) && Math.abs(offset(node) - ARC * (7 / 8)) <= 0.05
          && Math.abs(status.state("main").ratio - 0.125) < 1e-9, String(offset(node)));
    status.onStatus(st("main", "preview", { rev: 2, spec: "z2", loaded: 0, needed: 0 }));
    const zero = showsDot(node) && !node.classList.contains("has-ring");
    status.onStatus(st("main", "preview", { rev: 2, spec: "z2", loaded: 9, needed: 8 }));
    check("counts that mean nothing (0 needed, more loaded than needed) show the dot",
          zero && showsDot(node) && !node.classList.contains("has-ring"));
    status.unmount();
}

// -- the hold and the fade ------------------------------------------------------------

{
    const { status, hosts, busy } = setup();
    status.onStatus(st("xz", "fetching", { token: "xzy10", spec: undefined }));
    advance(DELAY_MS);
    advance(50);                                     // the frame, and 50 ms on screen
    const node = pill(hosts.xz);
    status.onStatus(st("xz", "idle", { token: "xzy10", spec: undefined }));
    check("idle after 50 ms on screen: still up, and the pane is no longer busy",
          on(node) && busy.xz.getAttribute("aria-busy") === "false");
    advance(MIN_VISIBLE_MS - 50 - 1);
    const held = on(node);
    advance(1);
    const fading = !node.hidden && !node.classList.contains("is-visible");
    advance(FADE_MS - 1);
    const stillThere = !node.hidden;
    advance(1);
    check("held until 300 ms on screen, then faded, then hidden after 150 ms",
          held && fading && stillThere && node.hidden && !status.state("xz").visible
          && status.state("xz").shows === 1);
    // Busy again while fading: back up at once, no new delay.
    status.onStatus(st("xz", "fetching", { token: "xzy11", spec: undefined }));
    advance(DELAY_MS);
    status.onStatus(st("xz", "idle", { token: "xzy11", spec: undefined }));
    advance(MIN_VISIBLE_MS + 10);
    status.onStatus(st("xz", "fetching", { token: "xzy12", spec: undefined }));
    advance(16);
    check("busy again while it fades: back up at once, and counted as another show",
          on(node) && status.state("xz").shows === 3 && status.state("xz").visible);
    status.unmount();
}

// -- errors -----------------------------------------------------------------------------

{
    const { status, hosts } = setup();
    let retried = 0;
    status.onStatus(st("main", "fetching"));
    advance(DELAY_MS);
    advance(16);
    const mainNode = pill(hosts.main);
    const mainBefore = { phase: mainNode.dataset.phase, shows: status.state("main").shows };
    status.onStatus(st("xz", "error", { token: "xzy10", spec: undefined, failed: 3,
                                        retry: () => { retried += 1; } }));
    advance(16);                                     // one frame, no delay
    const node = pill(hosts.xz);
    const button = part(node, "plx-pane-status-retry");
    check("an error shows at once: the retry button only, the pill no longer aria-hidden",
          on(node) && button && !button.hidden && part(node, "plx-pane-status-dot").hidden
          && part(node, "plx-pane-status-ring").hasAttribute("hidden")
          && !node.hasAttribute("aria-hidden") && node.dataset.phase === "error"
          && button.getAttribute("aria-label") === "Retry loading the XZ section"
          && button.type === "button" && /did not finish loading/.test(button.title));
    const first = button.click();
    button.click();
    check("a click calls retry() once, and never reaches the pane under it",
          retried === 1 && first.stopped);
    advance(10000);
    check("an error never takes itself down", on(node) && status.state("xz").phase === "error");
    check("an error in XZ leaves the plane's pill as it was",
          mainNode.dataset.phase === mainBefore.phase && status.state("main").shows === mainBefore.shows
          && on(mainNode) && !pill(hosts.yz));
    // The retry under way, then loaded.
    status.onStatus(st("xz", "fetching", { token: "xzy10", spec: undefined }));
    const busyAgain = on(node) && showsDot(node) && button.hidden && node.getAttribute("aria-hidden") === "true";
    status.onStatus(st("xz", "idle", { token: "xzy10", spec: undefined }));
    advance(MIN_VISIBLE_MS + FADE_MS);
    check("after a retry: the dot while it loads, then gone", busyAgain && node.hidden);
    status.unmount();
}

// -- lazy mounts ------------------------------------------------------------------------

{
    let host = null;
    const status = new PaneStatus({
        mounts: { yz: () => host },
        busyHosts: {},
        strip: () => null,
        now: () => clock.now, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake,
        requestAnimationFrame: requestAnimationFrameFake, target: box, document: documentFake,
    });
    status.mount();
    status.onStatus(st("yz", "fetching", { token: "yzx1", spec: undefined }));
    advance(DELAY_MS + 16);
    const before = status.state("yz").visible;
    host = element();                               // the panes are built now
    status.onStatus(st("yz", "refining", { token: "yzx1", spec: undefined, loaded: 1, needed: 4 }));
    const node = pill(host);
    const rebuilt = element();
    host = rebuilt;                                 // sections off and on: a new pane
    status.onStatus(st("yz", "refining", { token: "yzx1", spec: undefined, loaded: 2, needed: 4 }));
    check("mounts resolve lazily: no host yet costs nothing, and a pane built later or rebuilt gets the pill",
          before && node && pill(rebuilt) === node && node.className.includes("is-yz"));
    status.unmount();
}

// -- through the event, and unmount -------------------------------------------------------

{
    const { status, hosts, strip } = setup();
    box.dispatchEvent({ type: "plexora:surface-status", detail: st("main", "slow") });
    const node = pill(hosts.main);
    check("the plexora:surface-status event reaches it; slow is busy like fetching",
          node && status.state("main").phase === "slow" && strip.calls.at(-1) === true);
    status.onStatus(st("xz", "fetching", { token: "xzy1", spec: undefined }));
    advance(DELAY_MS + 16);
    status.onStatus(st("xz", "idle", { token: "xzy1", spec: undefined }));   // a hold timer
    status.onStatus(st("yz", "fetching", { token: "yzx1", spec: undefined })); // a delay timer
    const nodes = ["main", "xz", "yz"].map((surface) => pill(hosts[surface])).filter(Boolean);
    const armed = pending();
    status.unmount();
    const live = pending();
    advance(5000);
    box.dispatchEvent({ type: "plexora:surface-status", detail: st("main", "fetching") });
    check("unmount clears every timer, removes the pills, stops listening and hands the thumb back",
          armed >= 2 && live === 0 && nodes.length === 3 && nodes.every((n) => n.parentNode === null)
          && strip.loadingSource === null && strip.calls.at(-1) === false
          && status.state("main").shows === 0 && pill(hosts.main) === null);
}

// -- the depth strip's thumb reads the same status -------------------------------------

{
    runInContext(readFileSync(join(JS, "views/depthStrip.js"), "utf8"), context);
    const DepthStrip = box.PlexoraDepthStrip;
    const vs = { depth: 10, frames: 1, get: () => ({ z: 3, t: 0, mode: "plane", layout: "single",
                                                     slab: { z0: 0, z1: 0 } }),
                 spec: () => "z3", appliedSpec: () => "z2", label: () => "z 4/10" };
    const strip = new DepthStrip(vs);
    strip.root = element();
    strip.thumb = element();
    strip.band = element();
    strip.rail = element();
    strip.modeButtons = {};
    strip.toolButtons = {};
    strip.applyControls = () => null;
    strip.paintPlay = () => {};
    strip.paint();
    const own = strip.root.classList.contains("is-loading");
    const status = new PaneStatus({
        mounts: { main: () => strip.root }, busyHosts: {}, strip: () => strip,
        now: () => clock.now, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake,
        requestAnimationFrame: requestAnimationFrameFake, target: box, document: documentFake,
    });
    status.mount();
    status.onStatus(st("main", "preview", { loaded: 2, needed: 8 }));
    strip.paint();                                  // a repaint for any reason
    const follows = !strip.root.classList.contains("is-loading");
    status.onStatus(st("main", "fetching", { rev: 2, spec: "z4" }));
    strip.paint();
    check("before any status the strip reads spec vs applied; after, its repaint follows the status",
          own && follows && strip.root.classList.contains("is-loading"));
    status.unmount();
    strip.paint();
    check("and unmounted, the strip reads spec vs applied again",
          strip.loadingSource === null && strip.root.classList.contains("is-loading"));
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\n${checks} checks passed`);
