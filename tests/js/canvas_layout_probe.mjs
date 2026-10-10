/**
 * One layout for everything that floats over the image (services/canvasLayout.js).
 *
 * What is pinned is what keeps two controls from landing on each other:
 *
 *   **Bands add up along their edge**, the lowest order outermost, and each
 *   publishes where it starts -- the YZ pane sits inside the depth strip's
 *   band, never under it -- and the totals are the safe area's insets.
 *
 *   **A dock keeps order**: core's own (order 0) nearest the corner, a
 *   plugin's after it, a 3-D control after that, whatever order they arrive
 *   in. So Figure Builder's panel stacks under the dataset chip and the
 *   orientation gizmo under Figure Builder -- never one over another.
 *
 *   **A plugin needs no call into core**: an element appended to the wrapper
 *   with `data-canvas-dock` is docked; one without it is left alone.
 *
 * Run directly:  node tests/js/canvas_layout_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/canvasLayout.js"), "utf8");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

// -- a DOM small enough to read --------------------------------------------

class HTMLElement {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.dataset = {};
        this.className = "";
        const classes = new Set();
        this.classList = {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            contains: (name) => classes.has(name) || this.className.split(/\s+/).includes(name),
        };
        const props = {};
        this.style = { setProperty: (k, v) => { props[k] = v; }, getPropertyValue: (k) => props[k] };
        this.animations = [];
    }
    get isConnected() { return true; }
    setAttribute(name, value) {
        if (name.startsWith("data-")) {
            const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            this.dataset[key] = String(value);
        }
    }
    detach() {
        if (!this.parentElement) return;
        const list = this.parentElement.children;
        list.splice(list.indexOf(this), 1);
        this.parentElement = null;
    }
    appendChild(child) { return this.insertBefore(child, null); }
    append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
    insertBefore(child, before) {
        child.detach();
        const at = before ? this.children.indexOf(before) : -1;
        if (at >= 0) this.children.splice(at, 0, child);
        else this.children.push(child);
        child.parentElement = this;
        return child;
    }
    remove() { this.detach(); }
    matches() { return false; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 10, height: 10 }; }
    animate(keyframes, options) { this.animations.push({ keyframes, options }); return {}; }
}

const wrapper = new HTMLElement("div");
const box = {
    console,
    HTMLElement,
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { (box.events ||= []).push(event); return true; },
    matchMedia: () => ({ matches: false }),
    document: {
        createElement: (tag) => new HTMLElement(tag),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
    },
};
box.window = box;
box.globalThis = box;
runInContext(SOURCE, createContext(box));
const Layout = box.PlexoraCanvasLayout;
const layout = Layout.get();
const dock = (corner) => layout.docks[corner];
const names = (corner) => dock(corner).children.map((child) => child.dataset.canvasId || child.id);

// -- bands ---------------------------------------------------------------------

Layout.reserve("sections-yz", { edge: "right", size: 300, order: 10 });
Layout.reserve("depth-strip", { edge: "right", size: 56, order: 0 });
Layout.reserve("sections-xz", { edge: "bottom", size: 180, order: 10 });
const v = (name) => wrapper.style.getPropertyValue(name);
check("bands on one edge add up", v("--plx-inset-right") === "356px"
    && v("--plx-inset-bottom") === "180px" && v("--plx-inset-left") === "0px");
check("the lowest order is outermost: the YZ pane starts where the strip's band ends",
    v("--plx-band-depth-strip") === "0px" && v("--plx-band-sections-yz") === "56px");
check("the insets are readable for a plugin",
    JSON.stringify(Layout.insets()) === JSON.stringify({ top: 0, right: 356, bottom: 180, left: 0 }));
check("a change is announced", (box.events || []).some((e) => e.type === "plexora:canvas-layout"));
Layout.release("sections-yz");
check("a band given back gives its room back", v("--plx-inset-right") === "56px");
let refused = false;
try {
    Layout.reserve("nowhere", { edge: "middle", size: 10 });
} catch (error) {
    refused = true;
}
check("an edge that does not exist is refused, not ignored", refused);

// -- docks ---------------------------------------------------------------------

const make = (id) => {
    const node = new HTMLElement("div");
    node.id = id;
    return node;
};
// Arriving out of order, as they do: the gizmo first, then the plugin, then
// core's chip, whose fetch is slowest.
Layout.place(make("gizmo"), { corner: "top-right", order: 10, id: "orientation" });
Layout.place(make("fb"), { corner: "top-right", order: 5, id: "figure-builder" });
Layout.place(make("nav"), { corner: "top-right", order: 0, id: "dataset-nav" });
check("a dock keeps order whatever order things arrive in",
    JSON.stringify(names("top-right")) === JSON.stringify(["dataset-nav", "figure-builder", "orientation"]),
    JSON.stringify(names("top-right")));
check("a docked element is marked for the dock's rules (viewer.css)",
    dock("top-right").children.every((child) => child.classList.contains("plx-docked")));

// A plugin's element appended with the attribute is docked; one without is not.
const plugin = make("plugin-panel");
plugin.dataset.canvasDock = "bottom-left";
plugin.dataset.canvasOrder = "20";
wrapper.appendChild(plugin);
layout.adopt(plugin);
const stray = make("stray");
wrapper.appendChild(stray);
layout.adopt(stray);
check("an element asking to be docked is docked", plugin.parentElement === dock("bottom-left"));
check("one that does not ask is left where it was put", stray.parentElement === wrapper);

const undock = Layout.place(make("legend"), { corner: "bottom-right", order: 0 });
check("undocking puts the element back in the wrapper", typeof undock === "function"
    && (undock(), wrapper.children.some((child) => child.id === "legend")));

console.log(failures.length ? `\n${failures.length} of ${checks} check(s) failed`
                            : `\nall ${checks} checks passed`);
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
