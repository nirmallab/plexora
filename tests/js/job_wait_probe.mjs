/**
 * views/jobWait.js: one modal-and-chip for every background job the viewer
 * watches, used twice -- the mask pyramid's wait and the 3-D mask's cell
 * surfaces (views/maskSurfaceWait.js).
 *
 * What is pinned is what two instances on one page must not do to each other:
 * each fills its own navbar mount; Escape puts away only the modal on top;
 * leaving the viewer puts away both; one keydown listener at most, removed
 * when the last modal goes. And what the second instance added: a failure
 * offers Retry when the caller can retry, and Retry starts the job again.
 *
 * The behaviour shared with the mask pyramid's wait is pinned in
 * tests/js/segmentation_wait_probe.mjs against that instance.
 *
 * Run directly:  node tests/js/job_wait_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JOB_WAIT = join(REPO, "plexora/client/src/js/views/jobWait.js");
const MASK_WAIT = join(REPO, "plexora/client/src/js/views/maskSurfaceWait.js");
const SEGMENTATION_WAIT = join(REPO, "plexora/client/src/js/views/segmentationWait.js");
const PORTAL = join(REPO, "plexora/client/src/js/views/popoverPortal.js");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

function makeElement(tag) {
    const classes = new Set();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(), id: "", type: "", title: "", hidden: false,
        textContent: "", style: {}, children: [], parent: null,
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (c) => classes.add(c), remove: (c) => classes.delete(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        setAttribute() {}, getAttribute: () => null,
        appendChild(child) { child.parent = element; element.children.push(child); return child; },
        remove() {
            const siblings = element.parent?.children;
            if (siblings) siblings.splice(siblings.indexOf(element), 1);
            element.parent = null;
        },
        addEventListener(name, fn) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(fn);
        },
        fire(name, event = {}) {
            (listeners.get(name) || []).forEach((fn) => fn({ target: element, ...event }));
        },
        find(className) {
            const found = [];
            const walk = (node) => {
                if (node.classList.contains(className)) found.push(node);
                node.children.forEach(walk);
            };
            walk(element);
            return found;
        },
        contains(other) {
            for (let node = other; node; node = node.parent) if (node === element) return true;
            return false;
        },
    };
    Object.defineProperty(element, "parentNode", { get: () => element.parent });
    return element;
}

function boot() {
    const body = makeElement("body");
    const chips = {
        segmentation_chip: makeElement("button"),
        mask_surface_chip: makeElement("button"),
    };
    Object.values(chips).forEach((chip) => { chip.hidden = true; });
    const windowListeners = new Map();
    const documentListeners = new Map();
    const timers = [];
    const retried = [];
    const win = {
        setTimeout(fn) { timers.push(fn); return timers.length; },
        addEventListener(name, fn) {
            if (!windowListeners.has(name)) windowListeners.set(name, []);
            windowListeners.get(name).push(fn);
        },
        PlexoraMaskSurfaces: { retry: () => retried.push("retry") },
    };
    const doc = {
        body, fullscreenElement: null,
        createElement: (tag) => makeElement(tag),
        getElementById: (id) => chips[id] || null,
        querySelectorAll: () => [],
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
    runInContext(readFileSync(PORTAL, "utf8"), context, { filename: "popoverPortal.js" });
    for (const [file, name] of [[JOB_WAIT, "jobWait.js"], [SEGMENTATION_WAIT, "segmentationWait.js"],
                                [MASK_WAIT, "maskSurfaceWait.js"]]) {
        runInContext(readFileSync(file, "utf8"), context, { filename: name });
    }
    const overlays = () => body.find("segmentation-progress-overlay");
    return {
        win, chips, retried, overlays,
        mask: win.PlexoraMaskSurfaceWait,
        segmentation: win.PlexoraSegmentationWait,
        press(key) { (documentListeners.get("keydown") || []).slice().forEach((fn) => fn({ key })); },
        get keyHandlers() { return (documentListeners.get("keydown") || []).length; },
        viewerHidden() { (windowListeners.get("plexora:viewer-hidden") || []).forEach((fn) => fn({})); },
        titleOf(overlay) { return overlay.find("segmentation-progress-title")[0]?.textContent; },
    };
}

{
    const app = boot();
    app.mask.start();
    const [overlay] = app.overlays();
    check("the cell surfaces' wait opens its modal",
        app.overlays().length === 1 && app.titleOf(overlay) === "Converting the mask to 3-D surfaces",
        app.titleOf(overlay));
    const buttons = overlay.find("btn").filter((b) => !b.hidden);
    check("...with the way out it promises",
        buttons.length === 1 && buttons[0].textContent === "Continue in background",
        buttons.map((b) => b.textContent).join("|"));
    buttons[0].fire("click");
    check("Continue in background puts it in its own chip",
        app.overlays().length === 0 && app.chips.mask_surface_chip.hidden === false
        && app.chips.segmentation_chip.hidden === true);
    check("...saying what is building",
        /cell surfaces/.test(app.chips.mask_surface_chip.find("segmentation-chip-label")[0]?.textContent || ""));
    app.mask.progress({ progress: 40, stageLabel: "Preview · Extracting the surface" });
    check("readings reach the chip",
        app.chips.mask_surface_chip.find("segmentation-chip-label")[0].textContent
            === "Preview · Extracting the surface"
        && app.chips.mask_surface_chip.find("segmentation-chip-fill")[0].style.width === "40%");
    app.chips.mask_surface_chip.fire("click");
    check("the chip brings the modal back", app.overlays().length === 1
        && app.chips.mask_surface_chip.hidden === true);
}

{
    const app = boot();
    app.segmentation.start();
    app.mask.start();
    check("two waits on one page are two modals", app.overlays().length === 2);
    check("...with one Escape listener between them", app.keyHandlers === 1, `${app.keyHandlers}`);
    app.press("Escape");
    check("Escape puts away only the one on top",
        app.overlays().length === 1
        && app.titleOf(app.overlays()[0]) === "Pyramidizing segmentation mask"
        && app.chips.mask_surface_chip.hidden === false);
    app.press("Escape");
    check("...and then the other", app.overlays().length === 0 && app.keyHandlers === 0,
        `${app.keyHandlers} listeners left`);
    check("each went to its own chip",
        app.chips.segmentation_chip.hidden === false && app.chips.mask_surface_chip.hidden === false);
}

{
    const app = boot();
    app.segmentation.start();
    app.mask.start();
    app.viewerHidden();
    check("leaving the viewer puts both away", app.overlays().length === 0 && app.keyHandlers === 0);
}

{
    const app = boot();
    app.mask.start({ modal: false });
    check("a build already running shows its chip only",
        app.overlays().length === 0 && app.chips.mask_surface_chip.hidden === false);
    app.mask.failed("the mask could not be read");
    const [overlay] = app.overlays();
    check("a failure reopens it once, with the reason",
        app.overlays().length === 1
        && overlay.find("segmentation-progress-detail")[0].textContent === "the mask could not be read");
    const retry = overlay.find("btn-primary")[0];
    check("...offering Retry", retry && !retry.hidden && retry.textContent === "Retry");
    retry.fire("click");
    check("Retry asks the surfaces to build again, and the failure is put away",
        app.retried.length === 1 && app.overlays().length === 0
        && app.chips.mask_surface_chip.hidden === true);
}

{
    const app = boot();
    app.segmentation.start();
    app.segmentation.failed("bad mask");
    const retry = app.overlays()[0].find("btn-primary")[0];
    check("a wait that cannot retry offers no Retry", !retry || retry.hidden);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
