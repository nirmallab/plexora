/**
 * The "which machine?" chip on every form field that is not the home page or
 * the Import dialog (plexora/client/src/js/views/machineChip.js).
 *
 * What it has to get right, each of which fails quietly:
 *
 *   1. **It says where the field is.** "Local", or the server's name, read
 *      from the field's own state on every paint -- a chip keeping a copy of
 *      its own is a chip that disagrees with the field after a drop.
 *   2. **Its menu is the home page's.** Local, every saved server with whether
 *      it is up, Add a server…, Manage servers -- so the question is asked one
 *      way across the app.
 *   3. **A server that is not up is connected, not adopted.** Putting a field
 *      on a machine nothing can read a path on is the state the question
 *      exists to avoid; the connection modal is where a machine comes up.
 *   4. **The menu opens inside the dialog the chip is in.** A modal dialog is
 *      the top layer, and a menu under <body> would open behind it.
 *   5. **Escape closes the menu, not the dialog.**
 *
 * Run directly:  node tests/js/machine_chip_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/machineChip.js");

// -- a DOM small enough to read -----------------------------------------------

let activeElement = null;

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        href: "",
        title: "",
        hidden: false,
        disabled: false,
        textContent: "",
        dataset: {},
        style: { setProperty(name, value) { element.style[name] = value; } },
        children: [],
        parentNode: null,
        offsetWidth: 280,
        offsetHeight: 200,
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
        //: The chip's markup is four spans by class; one span per class named
        //: is all `querySelector` below needs to find the label.
        set innerHTML(markup) {
            element.children = [];
            for (const match of String(markup).matchAll(/class="([^"]+)"/g)) {
                const span = makeElement("span");
                span.className = match[1];
                element.appendChild(span);
            }
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
        appendChild(child) {
            if (child.parentNode) child.remove();
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
        insertBefore(child, before) {
            if (child.parentNode) child.remove();
            child.parentNode = element;
            const at = element.children.indexOf(before);
            element.children.splice(at < 0 ? element.children.length : at, 0, child);
            return child;
        },
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
        contains(other) {
            for (let node = other; node; node = node.parentNode) {
                if (node === element) return true;
            }
            return false;
        },
        closest(selector) {
            const wanted = selector.toUpperCase();
            for (let node = element; node; node = node.parentNode) {
                if (node.tagName === wanted) return node;
            }
            return null;
        },
        /** `.a`, `.a:not(:disabled)` and `[data-place="x"]`: what the chip asks. */
        querySelectorAll(selector) {
            const place = /^\[data-place="(.+)"\]$/.exec(selector);
            const cls = /^\.([\w-]+)(:not\(:disabled\))?$/.exec(selector);
            const found = [];
            const walk = (node) => {
                node.children.forEach((child) => {
                    if (place ? child.dataset.place === place[1]
                        : child.classList.contains(cls[1]) && !(cls[2] && child.disabled)) {
                        found.push(child);
                    }
                    walk(child);
                });
            };
            walk(element);
            return found;
        },
        querySelector(selector) { return element.querySelectorAll(selector)[0] || null; },
        getBoundingClientRect: () => ({ left: 100, top: 100, bottom: 130, right: 180 }),
        focus() { activeElement = element; },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener(type, handler) {
            listeners.set(type, (listeners.get(type) || []).filter((h) => h !== handler));
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
            return true;
        },
        click() { element.dispatchEvent({ type: "click", target: element }); },
    };
    return element;
}

const body = makeElement("body");
const documentListeners = new Map();
const context = {
    console,
    Promise,
    setTimeout,
    JSON,
    CSS: { escape: (value) => String(value) },
    plexoraUrl: (path) => `/${path}`,
    innerWidth: 1280,
    innerHeight: 800,
    addEventListener() {},
    removeEventListener() {},
    document: {
        body,
        createElement: makeElement,
        get activeElement() { return activeElement; },
        addEventListener(type, handler) {
            if (!documentListeners.has(type)) documentListeners.set(type, []);
            documentListeners.get(type).push(handler);
        },
        removeEventListener(type, handler) {
            documentListeners.set(type, (documentListeners.get(type) || [])
                .filter((h) => h !== handler));
        },
    },
};
context.window = context;

//: What `/data_places` says, through PlexoraRemotes. A test replaces it.
let snapshot = { loaded: true, places: [] };
let subscribed = 0;
context.PlexoraRemotes = {
    snapshot: () => snapshot,
    subscribe: (handler, options) => {
        subscribed += options && options.active ? 1 : 0;
        return () => { subscribed -= options && options.active ? 1 : 0; };
    },
    isOpening: (state) => state === "connecting",
};

//: The connection modal: what it was asked, and what it answers.
const modalCalls = [];
let modalAnswer = null;
context.PlexoraConnectionModal = {
    open: (spec) => {
        modalCalls.push(spec);
        return Promise.resolve(modalAnswer);
    },
};

createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);
const Chip = context.window.PlexoraMachineChip;

// -- checks -------------------------------------------------------------------

const failures = [];

function check(label, condition) {
    if (condition) {
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}`);
    }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A field in a modal dialog, with a chip on its input. */
function mount(field = { where: "local", place: null }, what) {
    const dialog = makeElement("dialog");
    body.appendChild(dialog);
    const row = makeElement("div");
    dialog.appendChild(row);
    const input = makeElement("input");
    row.appendChild(input);
    const picks = [];
    const chip = Chip.create({
        current: () => field,
        onPick: (where, place) => {
            picks.push({ where, place });
            field.where = where;
            field.place = place;
        },
        what,
    });
    const box = Chip.wrap(input, chip);
    const menu = () => dialog.children.find((c) => c.classList.contains("plx-machine-menu")) || null;
    const rows = () => (menu() ? menu().children : []);
    const named = (name) => rows().find((r) => r.children.some(
        (c) => c.classList.contains("plx-import-menu-name") && c.textContent === name)) || null;
    return { dialog, row, input, chip, box, field, picks, menu, rows, named };
}

function labelOf(chip) {
    return chip.element.querySelector(".plx-import-machine-label").textContent;
}

const HPC = { id: "hpc", kind: "remote", label: "hpc", node: "hpc-data", state: "connected" };
const O2 = { id: "o2", kind: "remote", label: "o2", node: null, state: "idle", files_only: true };
const ERIS = { id: "eris", kind: "remote", label: "eris", node: null, state: "connecting",
               phase: "Authenticating…" };

async function main() {
    // -- it says where the field is ---------------------------------------
    const local = mount();
    check("the chip sits in the path box, before the input",
          local.row.children[0] === local.box
          && local.box.classList.contains("plx-machine-field")
          && local.box.children[0] === local.chip.element
          && local.box.children[1] === local.input);
    check("...and reads Local for a field on this computer",
          labelOf(local.chip) === "Local"
          && !local.chip.element.classList.contains("is-remote")
          && /this computer/.test(local.chip.element.getAttribute("aria-label")));

    const away = mount({ where: "remote", place: { id: "hpc", label: "hpc" } });
    check("...and the server's name for a field on one, tinted as remote",
          labelOf(away.chip) === "hpc" && away.chip.element.classList.contains("is-remote"));

    away.field.where = "local";
    away.field.place = null;
    away.chip.paint();
    check("...read from the field on every paint, never kept",
          labelOf(away.chip) === "Local");

    // -- its menu is the home page's ---------------------------------------
    snapshot = { loaded: true, places: [HPC, O2, ERIS] };
    local.chip.element.click();
    check("the menu opens inside the dialog the chip is in",
          local.menu() !== null && local.menu().parentNode === local.dialog
          && local.menu().hidden === false
          && local.chip.element.getAttribute("aria-expanded") === "true");
    check("...watching the list while it is open",
          subscribed === 1);
    const names = local.rows()
        .map((r) => (r.children.find((c) => c.classList.contains("plx-import-menu-name")) || r)
            .textContent)
        .filter(Boolean);
    check("...listing Local, every saved server, Add a server… and Manage servers",
          JSON.stringify(names) === JSON.stringify(
              ["Local", "Remote", "hpc", "o2", "eris", "Add a server…", "Manage servers"]));
    const sub = (name) => local.named(name).children
        .find((c) => c.classList.contains("plx-import-menu-sub")).textContent;
    check("...each saying whether it is up",
          sub("hpc") === "Connected" && sub("o2") === "Not connected"
          && sub("eris") === "Authenticating…");
    check("...with one still connecting not pickable until it is",
          local.named("eris").disabled === true);
    check("...and Local ticked for a field on this computer",
          local.named("Local").getAttribute("aria-checked") === "true"
          && local.named("hpc").getAttribute("aria-checked") === "false");
    check("...and Manage servers going to Settings",
          local.named("Manage servers").href === "/settings#remotes");

    // -- picking -----------------------------------------------------------
    local.named("hpc").click();
    check("picking a connected server hands it to the field",
          local.picks.length === 1 && local.picks[0].where === "remote"
          && local.picks[0].place.id === "hpc" && local.picks[0].place.node === "hpc-data");
    check("...closes the menu and stops watching",
          local.menu() === null && subscribed === 0
          && local.chip.element.getAttribute("aria-expanded") === "false");
    check("...and the chip now names it", labelOf(local.chip) === "hpc");

    local.chip.element.click();
    local.named("Local").click();
    check("picking Local hands the field back to this computer",
          local.picks.at(-1).where === "local" && local.picks.at(-1).place === null
          && labelOf(local.chip) === "Local");

    // -- a server that is not up is connected, not adopted -------------------
    modalAnswer = { connected: true, name: "o2", label: "o2", node: null,
                    files: { base: "sftp://aj@o2", home: "/home/aj" } };
    const mask = mount(undefined, "the mask");
    mask.chip.element.click();
    mask.named("o2").click();
    check("a saved server that is down opens the connection dialog for it",
          modalCalls.length === 1 && modalCalls[0].name === "o2"
          && modalCalls[0].kind === "node" && mask.picks.length === 0);
    check("...saying what the field holds and what connecting will do",
          /the mask/.test(modalCalls[0].intent) && /SFTP/.test(modalCalls[0].intent));
    await settle();
    check("...and the machine it brought up is the one handed over",
          mask.picks.length === 1 && mask.picks[0].place.id === "o2"
          && mask.picks[0].place.files.base === "sftp://aj@o2");

    modalAnswer = { connected: false };
    const backOut = mount();
    backOut.chip.element.click();
    backOut.named("Add a server…").click();
    await settle();
    check("Add a server… opens the dialog for a new one",
          modalCalls.length === 2 && modalCalls[1].name === undefined
          && /Add the machine the file is on/.test(modalCalls[1].intent));
    check("...and backing out of it leaves the field where it was",
          backOut.picks.length === 0 && backOut.field.where === "local");

    // -- before the list has arrived --------------------------------------
    snapshot = { loaded: false, places: [] };
    const early = mount();
    early.chip.element.click();
    check("before the list arrives the menu says it is looking, not that there is none",
          early.rows().some((r) => r.textContent === "Looking for servers…"));
    early.chip.element.click();
    check("...and pressing the chip again puts it away", early.menu() === null);

    // -- Escape, and one menu at a time ----------------------------------------
    snapshot = { loaded: true, places: [HPC] };
    const first = mount();
    const second = mount();
    first.chip.element.click();
    second.chip.element.click();
    check("opening a second chip's menu puts the first one's away",
          first.menu() === null && second.menu() !== null);

    let prevented = false;
    let stopped = false;
    second.menu().dispatchEvent({
        type: "keydown", key: "Escape",
        preventDefault() { prevented = true; },
        stopPropagation() { stopped = true; },
    });
    check("Escape closes the menu and goes back to the chip, not the dialog",
          second.menu() === null && prevented && stopped
          && activeElement === second.chip.element);

    second.chip.element.click();
    (documentListeners.get("pointerdown") || []).forEach((h) => h({ target: body }));
    check("a press anywhere else puts the menu away", second.menu() === null);

    if (failures.length) {
        console.error(`\n${failures.length} failed`);
        process.exit(1);
    }
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
