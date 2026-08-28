/**
 * The mask-pyramid wait, as the viewer shows it.
 *
 * One promise runs through this whole file: **closing the panel does not stop
 * the job.** That is the entire reason the wait moved off the import-style
 * blocking overlay and into the viewer, and it is the one thing a user cannot
 * verify without waiting several minutes to find out they were wrong. So every
 * check below is really about where the wait IS at each moment -- modal, chip,
 * or gone -- and about the fact that readings keep landing either way.
 *
 * The other half is that this file asks the server nothing. main.js owns the
 * single poll and announces each reading; two surfaces polling one job would be
 * two answers free to disagree. The fake `fetch` here is never installed, and
 * the absence of any request is asserted by the events being the ONLY input.
 *
 * Run against the shipped file in a DOM stand-in. The stand-in is small on
 * purpose -- what is under test is which surface is up and what it says, not
 * the browser's layout engine -- so elements here are trees with classes, text
 * and a hidden flag, and nothing else.
 *
 * Run directly:  node tests/js/segmentation_wait_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/segmentationWait.js");
const PORTAL = join(REPO, "plexora/client/src/js/views/popoverPortal.js");

// -- a DOM small enough to read -----------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        id: "",
        type: "",
        title: "",
        hidden: false,
        textContent: "",
        style: {},
        children: [],
        parent: null,
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        setAttribute: (name, value) => attributes.set(name, String(value)),
        getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
        appendChild(child) {
            child.parent = element;
            element.children.push(child);
            return child;
        },
        remove() {
            const siblings = element.parent?.children;
            if (siblings) siblings.splice(siblings.indexOf(element), 1);
            element.parent = null;
        },
        addEventListener(name, fn) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(fn);
        },
        /** Deliver an event the way the browser would, to this node only. */
        fire(name, event = {}) {
            (listeners.get(name) || []).forEach((fn) => fn({ target: element, ...event }));
        },
        /** Every descendant carrying `className`, this node included. */
        find(className) {
            const found = [];
            const walk = (node) => {
                if (node.classList.contains(className)) found.push(node);
                node.children.forEach(walk);
            };
            walk(element);
            return found;
        },
        first(className) { return element.find(className)[0] || null; },
        /** Real Elements have this, and PopoverPortal asks it whether the
         *  fullscreen element already contains <body> -- the case where the
         *  popups do not need moving at all. */
        contains(other) {
            for (let node = other; node; node = node.parent) {
                if (node === element) return true;
            }
            return false;
        },
    };
    // PopoverPortal compares against `parentNode` before it moves anything.
    Object.defineProperty(element, "parentNode", { get: () => element.parent });
    return element;
}

/**
 * Load the module fresh, with the navbar mount point base.html supplies and
 * nothing else. Returns the handles a test needs to drive it.
 */
function boot() {
    const body = makeElement("body");
    const chip = makeElement("button");
    chip.id = "segmentation_chip";
    chip.hidden = true;

    const windowListeners = new Map();
    const documentListeners = new Map();
    const timers = [];

    const win = {
        setTimeout(fn, delay) { timers.push({ fn, delay }); return timers.length; },
        addEventListener(name, fn) {
            if (!windowListeners.has(name)) windowListeners.set(name, []);
            windowListeners.get(name).push(fn);
        },
    };
    // What the viewer's full-screen button fullscreens (ImageViewer's
    // "pre-full-page" handler), and the reason the overlay may not simply be
    // appended to <body>: the Fullscreen API paints an opaque ::backdrop over
    // everything outside this subtree.
    const bodyDiv = makeElement("div");
    bodyDiv.id = "bodyDiv";
    body.appendChild(bodyDiv);

    const doc = {
        body,
        fullscreenElement: null,
        createElement: (tag) => makeElement(tag),
        getElementById: (id) => (id === "segmentation_chip" ? chip : null),
        addEventListener(name, fn) {
            if (!documentListeners.has(name)) documentListeners.set(name, []);
            documentListeners.get(name).push(fn);
        },
        removeEventListener(name, fn) {
            const list = documentListeners.get(name) || [];
            const at = list.indexOf(fn);
            if (at >= 0) list.splice(at, 1);
        },
    };

    const context = createContext({
        console, Object, Array, String, Boolean, Number, Math, JSON, Set, Map,
        window: win, document: doc,
    });
    // The real one, run from source: where the overlay is parented is a
    // decision this file makes, and the fullscreen ::backdrop makes a wrong
    // answer invisible rather than broken.
    runInContext(readFileSync(PORTAL, "utf8"), context, { filename: "popoverPortal.js" });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "segmentationWait.js" });

    return {
        api: win.PlexoraSegmentationWait,
        chip,
        bodyDiv,
        /** The overlay while it is on screen, wherever it has been parented. */
        get modal() {
            return body.find("segmentation-progress-overlay")[0] || null;
        },
        /** Go in and out of the viewer's full-page mode. */
        fullscreen(on) {
            doc.fullscreenElement = on ? bodyDiv : null;
            (documentListeners.get("fullscreenchange") || []).slice()
                .forEach((fn) => fn({}));
        },
        announce(what, detail) {
            (windowListeners.get(`plexora:segmentation-${what}`) || [])
                .forEach((fn) => fn({ detail }));
        },
        viewerHidden() {
            (windowListeners.get("plexora:viewer-hidden") || []).forEach((fn) => fn({}));
        },
        press(key) {
            (documentListeners.get("keydown") || []).slice().forEach((fn) => fn({ key }));
        },
        /** How many document-level keydown listeners are still installed -- a
         *  closed modal that left one behind would swallow Escape forever. */
        get keyHandlers() { return (documentListeners.get("keydown") || []).length; },
        runTimers() {
            const due = timers.splice(0, timers.length);
            due.forEach(({ fn }) => fn());
        },
        get pendingTimers() { return timers.length; },
    };
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/** All of a node's text, which is what a user reads off the card. */
function textOf(node) {
    let out = node.textContent || "";
    node.children.forEach((child) => { out += ` ${textOf(child)}`; });
    return out;
}

// -- arriving in the viewer with a job already running ------------------------

{
    const app = boot();
    check("nothing is on screen until the viewer says a job is running",
        app.modal === null && app.chip.hidden === true);

    app.api.start();
    const card = app.modal?.first("segmentation-progress-card");
    check("start() puts the modal up",
        Boolean(card), "the mask the user just attached is why they are here");
    check("...saying the work is already running, in the background",
        /background/i.test(textOf(card)) && /not pyramidized/i.test(textOf(card)),
        textOf(card));
    check("...and that closing it costs nothing",
        /close this at any time/i.test(textOf(card))
        && /keep viewing the image/i.test(textOf(card)),
        "the whole reason this is not the import pages' blocking overlay");
    check("...and that the mask arrives by itself",
        /outlines by itself/i.test(textOf(card)),
        "otherwise closing reads as giving up on it");
    check("the bar slides rather than sitting at zero before the first reading",
        card.first("segmentation-progress-fill").classList.contains("is-indeterminate"),
        "a conversion reports nothing while it opens the file");
    check("the navbar stays clear while the modal is up",
        app.chip.hidden === true, "two surfaces saying one sentence");
}

// -- readings land, and move both surfaces ------------------------------------

{
    const app = boot();
    app.api.start();
    app.announce("progress", { progress: 42, message: "Converting segmentation mask" });
    const fill = app.modal.first("segmentation-progress-fill");
    check("a reading from main.js's poll moves the bar",
        fill.style.width === "42%" && !fill.classList.contains("is-indeterminate"),
        `width ${fill.style.width}`);
    check("...and the server's own line replaces the opening one",
        app.modal.first("segmentation-progress-detail").textContent
            === "Converting segmentation mask",
        "it says which kind of mask, and what the supplied one was missing");
}

// -- closing it, which is the point -------------------------------------------

{
    const app = boot();
    app.api.start();
    app.announce("progress", { progress: 30, message: "Converting segmentation mask" });
    app.modal.first("btn").fire("click");

    check("closing the modal takes it off screen",
        app.modal === null);
    check("...and leaves no keydown handler behind",
        app.keyHandlers === 0, `${app.keyHandlers} still installed`);
    check("the wait moves to the navbar rather than vanishing",
        app.chip.hidden === false,
        "a job with nowhere to show reads as a job that was cancelled");
    check("...labelled for the job it is",
        app.chip.first("segmentation-chip-label").textContent
            === "Pyramidizing segmentation mask…",
        app.chip.first("segmentation-chip-label").textContent);
    check("...carrying the progress it already had",
        app.chip.first("segmentation-chip-fill").style.width === "30%");

    app.announce("progress", { progress: 65, message: "Converting segmentation mask" });
    check("readings keep landing after the modal is gone",
        app.chip.first("segmentation-chip-fill").style.width === "65%",
        "closing the panel must not stop the job, or stop reporting it");

    app.chip.fire("click");
    check("clicking the chip brings the detail back",
        Boolean(app.modal), "the chip is where the modal went");
    check("...at the progress the job is actually at, not where it was left",
        app.modal.first("segmentation-progress-fill").style.width === "65%");
    check("...and the navbar gives way again",
        app.chip.hidden === true);
}

// -- the other two ways out ---------------------------------------------------

{
    const app = boot();
    app.api.start();
    app.press("Escape");
    check("Escape closes it too", app.modal === null && app.chip.hidden === false);
}

{
    const app = boot();
    app.api.start();
    const overlay = app.modal;
    overlay.fire("click", { target: overlay });
    check("so does clicking beside the card",
        app.modal === null && app.chip.hidden === false);
}

{
    const app = boot();
    app.api.start();
    const overlay = app.modal;
    overlay.fire("click", { target: overlay.first("segmentation-progress-card") });
    check("clicking the card itself does not",
        Boolean(app.modal), "or reading the message would dismiss it");
}

// -- the viewer's full-page mode ----------------------------------------------

{
    const app = boot();
    app.api.start();
    check("outside fullscreen the overlay sits on <body>, as the import pages' does",
        app.modal.parent.tagName === "BODY", app.modal.parent.tagName);

    app.fullscreen(true);
    check("going full-page carries it inside the fullscreen element",
        app.modal.parent === app.bodyDiv,
        "left on <body> it would be under the opaque ::backdrop -- open, and invisible");

    app.fullscreen(false);
    check("...and coming back out returns it",
        app.modal.parent.tagName === "BODY");
}

{
    const app = boot();
    app.fullscreen(true);
    app.api.start();
    check("a wait that begins during full-page mode lands inside it straight away",
        app.modal.parent === app.bodyDiv);
    app.modal.first("btn").fire("click");
    app.fullscreen(true);
    check("a closed overlay is not resurrected by the next fullscreen toggle",
        app.modal === null,
        "the portal must be told to let go, not just have the element removed");
}

// -- routed away from the viewer ----------------------------------------------

{
    const app = boot();
    app.api.start();
    app.viewerHidden();
    check("routing to another page puts the modal away",
        app.modal === null,
        "appRouter swaps pages inside this document -- the scrim would cover one");
    check("...and the chip takes over, since the navbar does not move",
        app.chip.hidden === false);
    app.announce("progress", { progress: 80, message: "" });
    check("...with the job still being reported",
        app.chip.first("segmentation-chip-fill").style.width === "80%");
}

// -- the job finishes ---------------------------------------------------------

{
    const app = boot();
    app.api.start();
    app.announce("ready", { segmentation: "/derived/mask.zarr" });
    const card = app.modal?.first("segmentation-progress-card");
    check("finishing says so on the modal that was still open",
        Boolean(card) && /ready/i.test(card.first("segmentation-progress-title").textContent),
        card && card.first("segmentation-progress-title").textContent);
    check("...with the bar full",
        card.first("segmentation-progress-fill").style.width === "100%");
    check("...and no offer to close a wait that is over",
        card.first("segmentation-progress-note").hidden === true
        && card.first("btn").hidden === true);
    check("...held briefly rather than snapped away",
        app.pendingTimers === 1, "a fast conversion would otherwise just flicker");
    app.runTimers();
    check("...then everything goes",
        app.modal === null && app.chip.hidden === true);
}

{
    const app = boot();
    app.api.start();
    app.modal.first("btn").fire("click");
    app.announce("ready", { segmentation: "/derived/mask.zarr" });
    check("finishing reopens nothing for a user who had closed it",
        app.modal === null,
        "main.js draws the mask as this fires; a modal would cover the news");
    check("...it just takes the chip away",
        app.chip.hidden === true);
}

// -- the job fails ------------------------------------------------------------

{
    const app = boot();
    app.api.start();
    app.modal.first("btn").fire("click");
    app.announce("failed", { error: "The mask does not match the image dimensions" });
    const card = app.modal?.first("segmentation-progress-card");
    check("a failure DOES reopen it",
        Boolean(card),
        "the job that was promised to finish by itself will not, and nothing else says so");
    check("...with the server's reason on it",
        card.first("segmentation-progress-detail").textContent
            === "The mask does not match the image dimensions");
    check("...marked as the failure it is",
        card.first("segmentation-progress-title").classList.contains("has-error")
        && card.first("segmentation-progress-fill").classList.contains("has-error"));
    check("...and offering to be dismissed rather than left",
        card.first("btn").textContent === "Dismiss");

    card.first("btn").fire("click");
    check("dismissing a failure ends it",
        app.modal === null && app.chip.hidden === true,
        "a red chip for a job that is over is a notification with nothing behind it");
}

{
    const app = boot();
    app.api.start();
    app.announce("failed", { error: "" });
    const card = app.modal.first("segmentation-progress-card");
    check("a failure with no message still says what happened",
        card.first("segmentation-progress-detail").textContent
            === "The mask could not be converted.");
}

// -- nothing runs when nothing is running -------------------------------------

{
    const app = boot();
    app.announce("progress", { progress: 50, message: "Converting" });
    app.announce("ready", { segmentation: "/derived/mask.zarr" });
    check("readings for a job this viewer never started are ignored",
        app.modal === null && app.chip.hidden === true,
        "start() is the gate -- main.js opens it only on segmentation_status pending");
    app.chip.fire("click");
    check("...and the chip cannot conjure a panel for one",
        app.modal === null);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
