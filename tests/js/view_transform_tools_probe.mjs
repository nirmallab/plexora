/**
 * Core's Rotate and Flip cards, run: the real views/viewTransformTools.js, the
 * real views/slider.js and the real services/viewTransform.js, in a DOM with
 * just enough tree for the panels (built with the ids and attributes
 * templates/tools/*.html carry -- tests/test_view_transform.py holds the two
 * together).
 *
 *     node tests/js/view_transform_tools_probe.mjs [--source <tools.js>]
 *
 * What it holds, because each one fails silently:
 *   - the right angles, the slider and its number are ONE value: moving any
 *     of them repaints the other two, and a change made elsewhere (the other
 *     card, a reset, a figure restore) repaints all three;
 *   - a card set up fresh shows the live orientation, not upright -- which is
 *     what "reopening restores it" means once the card holds no state;
 *   - closing a card stops it listening, and leaves the view as it was;
 *   - the definitions are lazy, eyeless and carry help.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");
const args = process.argv.slice(2);
const sourceIndex = args.indexOf("--source");
const TOOLS = sourceIndex >= 0 ? args[sourceIndex + 1] : join(JS, "views/viewTransformTools.js");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

// -- a small DOM ------------------------------------------------------------

function makeNode(tag) {
    const node = {
        tagName: String(tag).toUpperCase(),
        children: [],
        parentNode: null,
        attributes: {},
        dataset: {},
        value: "",
        disabled: false,
        style: {
            _held: new Map(),
            setProperty(name, value) { this._held.set(name, String(value)); },
            removeProperty(name) { this._held.delete(name); },
            getPropertyValue(name) { return this._held.get(name) ?? ""; },
        },
        _classes: new Set(),
        _listeners: {},
        textContent: "",
        innerHTML: "",
        get className() { return [...node._classes].join(" "); },
        set className(value) { node._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
        get classList() {
            return {
                add: (...names) => names.forEach((n) => node._classes.add(n)),
                remove: (...names) => names.forEach((n) => node._classes.delete(n)),
                contains: (n) => node._classes.has(n),
                toggle: (n, on) => {
                    const next = on === undefined ? !node._classes.has(n) : Boolean(on);
                    if (next) node._classes.add(n); else node._classes.delete(n);
                    return next;
                },
            };
        },
        get id() { return node.attributes.id || ""; },
        set id(value) { node.attributes.id = String(value); },
        setAttribute(name, value) {
            node.attributes[name] = String(value);
            if (name.startsWith("data-")) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                node.dataset[key] = String(value);
            }
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(node.attributes, name) ? node.attributes[name] : null;
        },
        removeAttribute(name) { delete node.attributes[name]; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        append(...kids) { kids.forEach((kid) => node.appendChild(kid)); },
        insertBefore(child, reference) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = node;
            const at = reference ? node.children.indexOf(reference) : -1;
            if (at < 0) node.children.push(child); else node.children.splice(at, 0, child);
            return child;
        },
        removeChild(child) {
            const at = node.children.indexOf(child);
            if (at >= 0) node.children.splice(at, 1);
            child.parentNode = null;
            return child;
        },
        remove() { node.parentNode?.removeChild(node); },
        addEventListener(type, fn) { (node._listeners[type] = node._listeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            node._listeners[type] = (node._listeners[type] || []).filter((f) => f !== fn);
        },
        dispatch(type, extra = {}) {
            const event = { type, target: node, preventDefault() {}, stopPropagation() {}, ...extra };
            (node._listeners[type] || []).forEach((fn) => fn(event));
        },
        click() { if (!node.disabled) node.dispatch("click"); },
        blur() {},
        focus() {},
        closest() { return null; },
        querySelectorAll(selector) { return all(node, matcher(selector)); },
        querySelector(selector) { return all(node, matcher(selector))[0] || null; },
    };
    return node;
}

function matcher(selector) {
    const text = String(selector).trim();
    if (text.startsWith("#")) return (n) => n.id === text.slice(1);
    if (text.startsWith(".")) return (n) => n._classes.has(text.slice(1));
    const attr = /^\[([\w-]+)\]$/.exec(text);
    if (attr) return (n) => n.getAttribute(attr[1]) !== null;
    return (n) => n.tagName === text.toUpperCase();
}

function all(root, predicate, out = []) {
    for (const child of root.children) {
        if (predicate(child)) out.push(child);
        all(child, predicate, out);
    }
    return out;
}

function el(tag, attrs = {}, kids = []) {
    const node = makeNode(tag);
    for (const [name, value] of Object.entries(attrs)) {
        if (name === "class") node.className = value; else node.setAttribute(name, value);
    }
    kids.forEach((kid) => node.appendChild(kid));
    return node;
}

/** templates/tools/rotate_panel.html, as a tree. */
function rotatePanel() {
    return el("section", { class: "sidebar-section view-transform-panel", id: "rotate_panel_section" }, [
        el("div", { class: "cell-mode-control is-compact", id: "rotate_quick_control" }, [
            el("div", { class: "cell-mode-options" }, [0, 90, 180, 270].map((deg) =>
                el("button", { class: "cell-mode-option", "data-rotate-to": String(deg),
                               role: "radio", "aria-checked": "false" }))),
        ]),
        el("div", { class: "slider-auto-row" }, [
            el("div", { id: "rotate_slider", class: "sidebar-slider" }),
            el("button", { id: "rotate_reset_button", class: "slider-auto-button" }),
        ]),
    ]);
}

/** templates/tools/flip_panel.html, as a tree. */
function flipPanel() {
    return el("section", { class: "sidebar-section view-transform-panel", id: "flip_panel_section" }, [
        el("div", { class: "view-transform-flips" }, [
            el("button", { class: "view-transform-flip", id: "flip_horizontal_button",
                           "data-flip": "flipH", "aria-pressed": "false" }),
            el("button", { class: "view-transform-flip", id: "flip_vertical_button",
                           "data-flip": "flipV", "aria-pressed": "false" }),
        ]),
    ]);
}

const body = makeNode("body");
const document = {
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => all(body, (n) => n.id === id)[0] || null,
    body,
};

// -- the context --------------------------------------------------------

const registered = [];
const fetches = [];
const ctx = {
    document, console, Math, Number, Object, Array, Set, Map, JSON, String, Boolean, Promise, Error,
    setTimeout: () => 0,
    clearTimeout: () => {},
    fetch: (url, init) => { fetches.push({ url, init }); return Promise.resolve({ ok: true }); },
    requestAnimationFrame: (fn) => fn(),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: () => true,
    Plexora: { registerPlugin: (definition) => registered.push(definition) },
};
ctx.window = ctx;
ctx.globalThis = ctx;
createContext(ctx);
for (const file of [join(JS, "views/slider.js"), join(JS, "services/viewTransform.js"), TOOLS]) {
    runInContext(readFileSync(file, "utf8"), ctx, { filename: file });
}

// -- definitions ----------------------------------------------------------

const rotateDef = registered.find((d) => d.name === "rotate");
const flipDef = registered.find((d) => d.name === "flip");
check("both tools register", !!rotateDef && !!flipDef, registered.map((d) => d.name));
for (const def of [rotateDef, flipDef].filter(Boolean)) {
    check(`${def.name} is lazy`, def.lazy === true);
    check(`${def.name} has no layer, so no eye`, def.hasLayer === false);
    check(`${def.name} carries help`, typeof def.help?.summary === "string" && def.help.summary.length > 20);
    check(`${def.name} has a sidebar controller and nothing else to activate`,
        typeof def.createSidebarController === "function" && !def.createInstance && !def.ownsCellLayer);
}
check("Rotate's help says what a flip does to the slider",
    /flip/i.test(JSON.stringify(rotateDef?.help || {})));
check("and that the rotation is saved with the image",
    /saved/i.test(rotateDef?.help?.summary || ""));

// -- a viewer with the real service ---------------------------------------

const osd = {
    viewport: {
        rotation: 0, flipped: false,
        setFlip(state) { this.flipped = state; },
        setRotation(d) { this.rotation = d; },
        getRotation() { return this.rotation; },
        getFlip() { return this.flipped; },
        goHome() {},
    },
};
const service = new ctx.PlexoraViewTransform(osd, { datasource: "alpha" });
const imageViewer = { viewTransform: service };

function mount(panel) {
    body.appendChild(panel);
    return panel;
}

function open(def) {
    const cleanups = [];
    const controller = def.createSidebarController({ viewer: imageViewer, onCleanup: (fn) => cleanups.push(fn) });
    controller.setup();
    return { controller, close: () => cleanups.splice(0).forEach((fn) => fn()) };
}

const inputs = (root, type) => all(root, (n) => n.tagName === "INPUT" && n.type === type);
const quick = (root) => root.querySelectorAll("[data-rotate-to]");
const lit = (root) => quick(root).filter((b) => b._classes.has("is-active")).map((b) => b.dataset.rotateTo);

// Something already turned the view before either card opened -- a saved
// orientation adopted at boot.
service.adopt({ degrees: 90, flipH: true, flipV: false });

const rotateRoot = mount(rotatePanel());
const rotate = open(rotateDef);
const range = inputs(rotateRoot, "range")[0];
const number = inputs(rotateRoot, "number")[0];
check("the slider built its track and its number", !!range && !!number,
    { range: !!range, number: !!number });

check("a fresh card shows the live angle on the quick-select", lit(rotateRoot).join() === "90", lit(rotateRoot));
check("...and on the slider", Number(range?.value) === 90, range?.value);
check("...and in the number", Number(number?.value) === 90, number?.value);
check("the quick-select marks its choice for assistive tech too",
    quick(rotateRoot).find((b) => b.dataset.rotateTo === "90")?.getAttribute("aria-checked") === "true");

// Quick-select -> slider + number.
quick(rotateRoot).find((b) => b.dataset.rotateTo === "180").click();
check("a right angle sets the state", service.get().degrees === 180, service.get());
check("...moves the slider", Number(range.value) === 180, range.value);
check("...writes the number", Number(number.value) === 180, number.value);
check("...and lights only itself", lit(rotateRoot).join() === "180", lit(rotateRoot));

// Slider -> quick-select + number.
range.value = "37";
range.dispatch("input");
check("a drag tick turns the view", service.get().degrees === 37, service.get());
check("...unlights every right angle", lit(rotateRoot).length === 0, lit(rotateRoot));
check("...and the number follows the thumb", Number(number.value) === 37, number.value);
check("a drag never changes the flips", service.get().flipH === true && service.get().flipV === false);

// Number -> slider + quick-select.
number.value = "270";
number.dispatch("input");
number.dispatch("change");
check("a typed angle turns the view", service.get().degrees === 270, service.get());
check("...moves the thumb", Number(range.value) === 270, range.value);
check("...and lights its right angle", lit(rotateRoot).join() === "270", lit(rotateRoot));

number.value = "360";
number.dispatch("input");
number.dispatch("change");
check("a typed full turn is stored as upright", service.get().degrees === 0, service.get());
check("...and reads as 0 on the quick-select", lit(rotateRoot).join() === "0", lit(rotateRoot));

range.value = "360";
range.dispatch("input");
check("a thumb dragged to the end of the track stays there", Number(range.value) === 360, range.value);
check("...while the view is upright", service.get().degrees === 0);

// Reset.
service.set({ degrees: 45 });
const reset = rotateRoot.querySelector("#rotate_reset_button");
check("reset is live while the view is turned", reset && !reset.disabled);
reset.click();
check("reset turns the view upright", service.get().degrees === 0, service.get());
check("...and keeps the flip", service.get().flipH === true);
check("...and goes quiet once upright", reset.disabled === true);

// -- Flip ---------------------------------------------------------------

const flipRoot = mount(flipPanel());
const flip = open(flipDef);
const hButton = flipRoot.querySelector("#flip_horizontal_button");
const vButton = flipRoot.querySelector("#flip_vertical_button");
check("a fresh Flip card shows the live flip", hButton.getAttribute("aria-pressed") === "true"
    && hButton._classes.has("is-active") && vButton.getAttribute("aria-pressed") === "false");

service.set({ degrees: 90 });
vButton.click();
check("flipping vertically turns it on", service.get().flipV === true);
check("...leaves horizontal as it was", service.get().flipH === true);
check("...and never touches the angle", service.get().degrees === 90, service.get());
check("...and says so", vButton.getAttribute("aria-pressed") === "true" && vButton._classes.has("is-active"));
check("the Rotate card still shows the angle a flip left alone", lit(rotateRoot).join() === "90");

hButton.click();
check("pressing an on flip turns it off", service.get().flipH === false
    && hButton.getAttribute("aria-pressed") === "false" && !hButton._classes.has("is-active"));

// A change from outside either card repaints both.
service.set({ degrees: 180, flipH: true, flipV: false });
check("a change made elsewhere repaints Rotate", lit(rotateRoot).join() === "180" && Number(range.value) === 180);
check("...and Flip", hButton.getAttribute("aria-pressed") === "true" && vButton.getAttribute("aria-pressed") === "false");

// -- closing ---------------------------------------------------------------

check("two open cards are two subscribers", service._subscribers.size === 2, service._subscribers.size);
rotate.close();
flip.close();
check("closing both leaves none -- a closed card must not go on being told",
    service._subscribers.size === 0, service._subscribers.size);
const before = { ...service.get() };
service.set({ degrees: 90 });
check("a closed Rotate card stops listening", Number(range.value) === 180, range.value);
check("a closed Flip card stops listening", hButton.getAttribute("aria-pressed") === "true");
check("closing a card leaves the view as it was", before.degrees === 180 && before.flipH === true);

// Reopening -- a new panel, as toolLoader injects a fresh fragment.
rotateRoot.remove();
const reopened = mount(rotatePanel());
const again = open(rotateDef);
check("reopening shows the orientation the view has now", lit(reopened).join() === "90"
    && Number(inputs(reopened, "range")[0]?.value) === 90);
again.close();

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exitCode = failures.length ? 1 : 0;
