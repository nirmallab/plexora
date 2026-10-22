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
 *   **A column that runs out of room folds what can fold**, the highest
 *   order first, and only as much as it must; the room coming back opens
 *   them again, the last folded first.
 *
 *   **A movable control goes where it is dragged and back**: by its handle
 *   only, pinned inside the safe area, kept as fractions of the room for the
 *   tab, restored before the first paint on the next page, and the x (or a
 *   drop next to home, or Escape) puts it back where it came from.
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
            toggle: (name, on) => ((on ?? !classes.has(name)) ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name) || this.className.split(/\s+/).includes(name),
        };
        const props = {};
        this.style = {
            setProperty: (k, v) => { props[k] = v; },
            getPropertyValue: (k) => props[k],
            removeProperty: (k) => { delete props[k]; },
        };
        this.animations = [];
        this.listeners = {};
    }
    get parentNode() { return this.parentElement; }
    get nextSibling() {
        const list = this.parentElement?.children || [];
        return list[list.indexOf(this) + 1] || null;
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) {
        this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
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
    getBoundingClientRect() { return this.rect || { left: 0, top: 0, width: 10, height: 10 }; }
    // Heights: an element is its own `h` plus what it holds; one given a
    // `room` (a column) shows only that much.
    get scrollHeight() { return (this.h || 0) + this.children.reduce((sum, c) => sum + c.scrollHeight, 0); }
    get clientHeight() { return this.room ?? this.scrollHeight; }
    dispatchEvent(event) { (this.events ||= []).push(event); return true; }
    animate(keyframes, options) { this.animations.push({ keyframes, options }); return { cancel() {} }; }
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
        createComment: (text) => Object.assign(new HTMLElement("#comment"), { data: text }),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        documentElement: new HTMLElement("html"),
    },
    listeners: {},
    addEventListener(type, fn) { (box.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
        box.listeners[type] = (box.listeners[type] || []).filter((f) => f !== fn);
    },
    sessionStorage: (() => {
        const items = new Map();
        return {
            getItem: (k) => (items.has(k) ? items.get(k) : null),
            setItem: (k, v) => { items.set(k, String(v)); },
            removeItem: (k) => { items.delete(k); },
        };
    })(),
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

// -- folding ---------------------------------------------------------------

const fresh = new HTMLElement("div");
box.document.getElementById = (id) => (id === "openseadragon_wrapper" ? fresh : null);
const roomy = new box.PlexoraCanvasLayout.CanvasLayout(fresh);
const right = roomy.safe.children[2];
const panel = (id, open, folded, order) => {
    const node = make(id);
    node.h = open;
    roomy.place(node, { corner: "top-right", order, collapse: (shut) => { node.h = shut ? folded : open; } });
    return node;
};
const figure = panel("figure", 200, 20, 5);
const gizmo = panel("gizmo", 150, 20, 10);
const legend = make("legend");
legend.h = 100;
roomy.place(legend, { corner: "bottom-right", order: 0 });
const shut = (node) => node.classList.contains("plx-canvas-collapsed");

right.room = 500;
roomy.fit();
check("a column with room folds nothing", !shut(figure) && !shut(gizmo));
right.room = 400;
roomy.fit();
check("short of room, the highest order folds first, and only that",
      shut(gizmo) && !shut(figure) && gizmo.h === 20 && figure.h === 200);
right.room = 250;
roomy.fit();
check("shorter still, the next one folds too", shut(gizmo) && shut(figure));
check("what cannot fold is left as it is", legend.h === 100 && !shut(legend));
right.room = 400;
roomy.fit();
check("room coming back opens the last folded first, as far as it fits",
      shut(gizmo) && !shut(figure));
right.room = 600;
roomy.fit();
check("and the rest when there is room for it", !shut(gizmo) && !shut(figure));

const plain = make("plugin-card");
plain.h = 300;
plain.dataset.canvasCollapsible = "";
roomy.place(plain, { corner: "top-right", order: 30 });
right.room = 500;
roomy.fit();
check("a plugin's element folds by event, with no call into core",
      shut(plain) && (plain.events || []).some((e) => e.type === "plexora:canvas-collapse"
                                                    && e.detail.collapsed === true));

// -- gliding ---------------------------------------------------------------

// The mini-map closing: its root shrinks to the lens in the bottom-left
// corner, so its top drops and its bottom stays. Measured from the top-left,
// that read as a move and the lens slid down from where the map's top had been.
const host = new HTMLElement("div");
box.document.getElementById = (id) => (id === "openseadragon_wrapper" ? host : null);
const glider = new box.PlexoraCanvasLayout.CanvasLayout(host);
const map = make("mini-map");
const chip = make("chip");
const key = make("legend");
glider.place(map, { corner: "bottom-left", order: 0 });
glider.place(chip, { corner: "bottom-left", order: 10 });
glider.place(key, { corner: "bottom-right", order: 0 });
map.rect = { left: 12, top: 500, width: 300, height: 200 };
chip.rect = { left: 12, top: 450, width: 100, height: 40 };
key.rect = { left: 1000, top: 600, width: 200, height: 100 };
glider.remember();
map.rect = { left: 12, top: 666, width: 34, height: 34 };
chip.rect = { left: 12, top: 616, width: 100, height: 40 };
key.rect = { left: 900, top: 600, width: 300, height: 100 };
glider.glide();
check("an item that only shrinks toward its corner does not glide",
      map.animations.length === 0, JSON.stringify(map.animations));
check("a right-dock item that only grows leftwards does not glide",
      key.animations.length === 0, JSON.stringify(key.animations));
check("the neighbour it let down glides from where it was",
      chip.animations.length === 1 && chip.animations[0].keyframes[0].translate === "0px -166px",
      JSON.stringify(chip.animations));

// -- movable controls -----------------------------------------------------

const stage = new HTMLElement("div");
box.document.getElementById = (id) => (id === "openseadragon_wrapper" ? stage : null);
const mover = new box.PlexoraCanvasLayout.CanvasLayout(stage);
const { Movable } = box.PlexoraCanvasLayout;
check("the free layer is in the safe area, last, and is not a column that folds",
      mover.safe.children[3] === mover.free && mover.columns.length === 3
      && !mover.columns.includes(mover.free));

// The free layer's box on screen, and a control 248 x 40 whose box is home
// (under the caption's name) or wherever its fractions put it -- what
// viewer.css computes from --plx-x/--plx-y.
const AREA = { left: 12, top: 12, width: 800, height: 600 };
const HOME = { left: 12, top: 40, width: 248, height: 40 };
mover.free.rect = AREA;
const captionBox = make("caption");
mover.place(captionBox, { corner: "top-left", order: 0, id: "caption" });
const control = (id) => {
    const node = make(id);
    const head = make(`${id}-head`);
    node.appendChild(head);
    captionBox.appendChild(node);
    node.getBoundingClientRect = () => {
        if (node.hidden) return { left: 0, top: 0, width: 0, height: 0 };
        if (!node.classList.contains("plx-floating")) return HOME;
        const x = Number(node.style.getPropertyValue("--plx-x"));
        const y = Number(node.style.getPropertyValue("--plx-y"));
        const at = Movable.offset({ x, y }, AREA, HOME);
        return { left: AREA.left + at.left, top: AREA.top + at.top, width: HOME.width, height: HOME.height };
    };
    return { node, head };
};
const fire = (type, init = {}) => {
    const event = { button: 0, pointerId: 1, clientX: 0, clientY: 0, key: "",
                    target: { closest: () => null }, defaultPrevented: false,
                    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...init };
    for (const fn of [...(box.listeners[type] || [])]) fn(event);
    return event;
};
const press = (head, init = {}) => {
    const event = { button: 0, pointerId: 1, clientX: 0, clientY: 0, target: { closest: () => null },
                    preventDefault() {}, stopPropagation() {}, ...init };
    for (const fn of head.listeners.pointerdown || []) fn(event);
};
const drag = (head, from, to, { release = true } = {}) => {
    press(head, { clientX: from[0], clientY: from[1] });
    fire("pointermove", { clientX: to[0], clientY: to[1] });
    if (release) fire("pointerup", { clientX: to[0], clientY: to[1] });
};
const KEY = "plexora:canvas-position:gating-contrast";
const stored = () => box.sessionStorage.getItem(KEY);
const near = (a, b) => Math.abs(a - b) < 1e-3;

const gate = control("gate-contrast");
const gated = mover.movable(gate.node, { id: "gating-contrast", handle: gate.head });
const reset = gate.head.children.find((child) => child.className === "plx-movable-reset");
const homeMark = captionBox.children[captionBox.children.indexOf(gate.node) - 1];
check("a movable starts at home, its x hidden, its home held by a node that takes no room",
      gate.node.parentElement === captionBox && reset && reset.hidden === true
      && homeMark?.tagName === "#COMMENT" && !gated.moved);
check("the handle is marked for its cursor and grip (viewer.css)",
      gate.head.classList.contains("plx-movable-handle") && gate.node.classList.contains("plx-movable"));

press(gate.head, { target: { closest: () => ({}) } });
check("a press on something to click inside the handle is its own, not a drag",
      !(box.listeners.pointermove || []).length);

drag(gate.head, [100, 50], [102, 51]);
check("a press that does not travel is a click: it stays home",
      gate.node.parentElement === captionBox && !gated.moved && stored() === null);

drag(gate.head, [100, 50], [400, 350], { release: false });
check("dragged, it leaves home for the free layer and follows the hand",
      gate.node.parentElement === mover.free && gate.node.classList.contains("plx-floating")
      && near(gated.at.x, 300 / 552) && near(gated.at.y, 328 / 560),
      JSON.stringify(gated.at));
check("its slot in the caption closes; the home node stays",
      !captionBox.children.includes(gate.node) && captionBox.children.includes(homeMark));
check("the x shows while it is away", reset.hidden === false);
check("everything else stops reacting to the pointer while it moves",
      box.document.documentElement.classList.contains("plx-moving-control"));
fire("pointerup", { clientX: 400, clientY: 350 });
const kept = JSON.parse(stored() || "null");
check("let go, the place is kept for the tab, as fractions of the room",
      kept && near(kept.x, 300 / 552) && near(kept.y, 328 / 560), stored());
check("and the cursor comes back", !box.document.documentElement.classList.contains("plx-moving-control"));

drag(gate.head, [400, 350], [5000, -5000]);
check("dragged past the edges it is pinned inside the safe area",
      gated.at.x === 1 && gated.at.y === 0, JSON.stringify(gated.at));

const fromRect = gate.node.getBoundingClientRect();
for (const fn of reset.listeners.click || []) fn({ preventDefault() {}, stopPropagation() {} });
const homeGlide = gate.node.animations.at(-1);
check("the x puts it back home, after its home node, and hides",
      gate.node.parentElement === captionBox
      && captionBox.children.indexOf(gate.node) === captionBox.children.indexOf(homeMark) + 1
      && reset.hidden === true && !gated.moved && !gate.node.classList.contains("plx-floating"));
check("going home forgets the place", stored() === null);
check("it glides home from where it was",
      homeGlide && homeGlide.keyframes[0].translate
          === `${fromRect.left - HOME.left}px ${fromRect.top - HOME.top}px`
      && homeGlide.keyframes[1].translate === "0px 0px" && homeGlide.options.duration > 0,
      JSON.stringify(homeGlide));

drag(gate.head, [100, 50], [300, 300]);
drag(gate.head, [300, 300], [105, 55]);
check("dropped next to home, it goes home and forgets the place",
      gate.node.parentElement === captionBox && !gated.moved && stored() === null);

drag(gate.head, [100, 50], [300, 300], { release: false });
fire("keydown", { key: "Escape" });
check("Escape mid-drag from home puts it back home",
      gate.node.parentElement === captionBox && !gated.moved && stored() === null);
gated.moveTo({ x: 0.2, y: 0.3 });
gated.save();
drag(gate.head, [100, 200], [500, 500], { release: false });
fire("keydown", { key: "Escape" });
check("Escape mid-drag from a place puts it back at that place",
      near(gated.at.x, 0.2) && near(gated.at.y, 0.3) && JSON.parse(stored()).x === 0.2);

// Hidden with its panel (gating off), shown again: it is the same element,
// so it is where it was; nothing about hiding moves it.
gate.node.hidden = true;
press(gate.head, { clientX: 1, clientY: 1 });
check("a hidden control cannot be picked up", !(box.listeners.pointermove || []).length);
gate.node.hidden = false;
drag(gate.head, [300, 300], [500, 400], { release: false });
gate.node.hidden = true;
fire("pointerup", { clientX: 500, clientY: 400 });
gate.node.hidden = false;
check("hidden under the hand (its panel closed mid-drag), it keeps the place it was dragged to",
      gate.node.parentElement === mover.free && gated.moved && JSON.parse(stored()).x > 0.2);
gated.moveTo({ x: 0.2, y: 0.3 });
gated.save();
check("hidden and shown again, it is where it was left",
      gate.node.parentElement === mover.free && near(gated.at.x, 0.2));

// The next sample: a full page load, the control built again under the
// caption, and the place read back before anything is painted.
gated.dispose();
check("disposed, it is given back to its home as it was, and the place is kept",
      gate.node.parentElement === captionBox && !captionBox.children.includes(homeMark)
      && !gate.head.children.includes(reset) && !gate.node.classList.contains("plx-floating")
      && stored() !== null);
gate.node.remove();
const again = control("gate-contrast-2");
const restored = mover.movable(again.node, { id: "gating-contrast", handle: again.head });
check("the next page opens with it where it was left, without a glide",
      again.node.parentElement === mover.free && near(restored.at.x, 0.2) && near(restored.at.y, 0.3)
      && again.node.animations.length === 0
      && again.head.children.find((child) => child.className === "plx-movable-reset").hidden === false);

box.matchMedia = () => ({ matches: true });
const before = again.node.animations.length;
restored.goHome(true);
check("under reduced motion it jumps home rather than gliding",
      again.node.parentElement === captionBox && again.node.animations.length === before);
box.matchMedia = () => ({ matches: false });
restored.dispose();

check("fractions are of the room the control can travel in",
      JSON.stringify(Movable.fractions({ left: 276, top: 560 }, AREA, HOME)) === JSON.stringify({ x: 0.5, y: 1 }));
check("and turn back into the same pixels",
      JSON.stringify(Movable.offset({ x: 0.5, y: 1 }, AREA, HOME)) === JSON.stringify({ left: 276, top: 560 }));
check("on an axis with no room the fraction is 0, not a division by zero",
      Movable.fractions({ left: 50, top: 50 }, { width: 200, height: 600 }, HOME).x === 0);
box.sessionStorage.setItem("plexora:canvas-position:junk", '{"x": 2, "y": 0.5}');
check("a remembered place that is not two fractions is ignored",
      Movable.read("plexora:canvas-position:junk") === null && Movable.read(null) === null);

// -- the lane rises over the bottom-centre dock (the bookmarks tray) ------------------
{
    const tray = new HTMLElement("section");
    Layout.place(tray, { corner: "bottom-center", order: 20, id: "bookmarks" });
    dock("bottom-center").rect = { left: 0, top: 0, width: 400, height: 90 };
    layout.measure();
    const withTray = parseFloat(v("--plx-lane-bottom"));
    tray.hidden = true;
    layout.measure();
    const without = parseFloat(v("--plx-lane-bottom"));
    check("the chat's lane starts above the bookmarks tray, and comes down when it closes",
          withTray >= Layout.insets().bottom + 12 + 90 + 12 && withTray > without, `${withTray} ${without}`);
    tray.remove();
}

console.log(failures.length ? `\n${failures.length} of ${checks} check(s) failed`
                            : `\nall ${checks} checks passed`);
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
