/**
 * The Layers panel with a raster layer stored ABOVE a points or surface layer.
 *
 * The panel lists its cards grouped by surface -- gl, then overlay, then
 * tiles -- because that is the order the page composites in. The stack it
 * reads does not have to be in that order: a project's registered layers
 * come back from `/config` in the order they were added, and a new layer is
 * registered on top. So a surface built in Surface Studio and then a pinned
 * projection, or a transcripts layer and then an H&E added with "+ Add
 * Layer", leaves a raster above a gl or overlay layer in the stack.
 *
 * The panel has to cope with that: build the grouped list, once, and stop.
 * It must not decide the list is out of date because the stack's own order
 * differs from the grouped one, re-render, and decide the same thing again.
 *
 * Run directly:  node tests/js/layer_manager_cross_surface_order_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

// -- a DOM with just enough of a tree to stack cards in -------------------

function makeNode(tag) {
    const node = {
        tagName: String(tag).toUpperCase(),
        children: [],
        parentNode: null,
        attributes: {},
        style: {
            _held: new Map(),
            setProperty(name, value) { this._held.set(name, String(value)); },
            removeProperty(name) { this._held.delete(name); },
            getPropertyValue(name) { return this._held.get(name) ?? ""; },
        },
        dataset: {},
        _classes: new Set(),
        _listeners: {},
        _text: "",
        get textContent() {
            return node._text + node.children.map((c) => c.textContent).join("");
        },
        set textContent(value) {
            node._text = String(value);
            node.children.splice(0).forEach((child) => { child.parentNode = null; });
        },
        _html: "",
        get innerHTML() { return node._html; },
        set innerHTML(value) {
            node._html = String(value);
            node.children.splice(0).forEach((child) => { child.parentNode = null; });
        },
        get className() { return [...node._classes].join(" "); },
        set className(value) {
            node._classes = new Set(String(value).split(/\s+/).filter(Boolean));
        },
        get classList() {
            return {
                add: (...names) => names.forEach((n) => node._classes.add(n)),
                remove: (...names) => names.forEach((n) => node._classes.delete(n)),
                contains: (n) => node._classes.has(n),
                toggle: (n, on) => {
                    const next = on === undefined ? !node._classes.has(n) : Boolean(on);
                    if (next) node._classes.add(n);
                    else node._classes.delete(n);
                    return next;
                },
            };
        },
        setAttribute(name, value) { node.attributes[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(node.attributes, name)
                ? node.attributes[name] : null;
        },
        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        removeChild(child) {
            const at = node.children.indexOf(child);
            if (at >= 0) node.children.splice(at, 1);
            child.parentNode = null;
            return child;
        },
        append(...kids) { kids.forEach((kid) => node.appendChild(kid)); },
        insertBefore(child, reference) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = node;
            const at = reference ? node.children.indexOf(reference) : -1;
            if (at < 0) node.children.push(child);
            else node.children.splice(at, 0, child);
            return child;
        },
        replaceWith(other) {
            const parent = node.parentNode;
            if (!parent) return;
            const at = parent.children.indexOf(node);
            if (other.parentNode) other.parentNode.removeChild(other);
            other.parentNode = parent;
            parent.children.splice(at, 1, other);
            node.parentNode = null;
        },
        remove() { node.parentNode?.removeChild(node); },
        removeAttribute(name) { delete node.attributes[name]; },
        addEventListener(type, fn) {
            (node._listeners[type] = node._listeners[type] || []).push(fn);
        },
        click() {
            const event = { target: node, stopPropagation() {} };
            for (let el = node; el; el = el.parentNode) {
                (el._listeners.click || []).forEach((fn) => fn(event));
            }
        },
        closest(selector) {
            const parts = String(selector).split(",").map((one) => one.trim());
            const hits = (n) => parts.some((one) => (one.startsWith(".")
                ? n._classes?.has?.(one.slice(1))
                : n.tagName === one.toUpperCase()));
            for (let n = node; n; n = n.parentNode) if (hits(n)) return n;
            return null;
        },
        querySelectorAll() { return []; },
        querySelector(selector) {
            const attr = /^\[([\w-]+)\]$/.exec(String(selector));
            if (!attr) return null;
            return findAll(node, (n) => n.getAttribute(attr[1]) !== null)[0] || null;
        },
    };
    Object.defineProperty(node, "firstChild", { get: () => node.children[0] || null });
    Object.defineProperty(node, "firstElementChild", { get: () => node.children[0] || null });
    return node;
}

function findAll(root, predicate, out = []) {
    for (const child of root.children || []) {
        if (predicate(child)) out.push(child);
        findAll(child, predicate, out);
    }
    return out;
}

/**
 * A fresh page: its own DOM, its own LayerStack class and its own Layer
 * Manager, because the manager is a module-level singleton and each scenario
 * below is a separate page load.
 */
function page() {
    const byId = {};
    ["layer_card_list", "layer_manager_section", "layer_manager_collapse"].forEach((id) => {
        byId[id] = makeNode(id === "layer_manager_collapse" ? "button" : "div");
    });
    const errors = [];
    const ctx = {
        document: {
            createElement: (tag) => makeNode(tag),
            getElementById: (id) => byId[id] || null,
        },
        // console.error is where LayerStack._changed reports a listener that
        // threw, so it is counted rather than printed.
        console: { ...console, error: (...args) => { errors.push(args.map(String).join(" ")); } },
        Math, Number, Object, Array, Set, Map, JSON, String, Boolean,
    };
    ctx.Sortable = function Sortable() { return { destroy() {} }; };
    ctx.PopoverPortal = { attach: (el) => el, detach: () => {}, root: () => null };
    ctx.ColorSwatchPicker = function ColorSwatchPicker(mount) {
        mount.appendChild(ctx.document.createElement("button"));
        return { setValue() {}, destroy() {} };
    };
    ctx.ColorSwatchPicker.DEFAULT_PRESETS = ["#ffffff"];
    ctx.requestAnimationFrame = (fn) => fn();
    ctx.window = ctx;
    ctx.globalThis = ctx;
    createContext(ctx);
    for (const file of ["slider.js", "cardList.js", "layerStack.js", "layerManager.js"]) {
        runInContext(readFileSync(join(VIEWS, file), "utf8"), ctx, { filename: file });
    }
    const list = byId.layer_card_list;
    const cards = () => list.children
        .map((c) => c.getAttribute("data-layer-card"))
        .filter(Boolean);
    return { ctx, errors, cards };
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

function attempt(fn) {
    try {
        fn();
        return null;
    } catch (error) {
        return error;
    }
}


// -- a stored surface, then a pinned projection, opened fresh -------------
//
// Surface Studio's surface (`kind: "surface"`, drawn on the gl surface) was
// saved first, the Depth tool's "Add as a layer" projection (`kind: "image"`)
// second. /config gives them back in that order, so the stack -- bottom first
// -- has the projection above the surface. The panel is initialised on that
// stack during boot (main.js), before the ViewerManager exists.

{
    const { ctx, errors, cards } = page();
    const { LayerStack, REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID } = ctx.PlexoraLayerStack;
    const stack = new LayerStack();
    stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "stack" });
    stack.register(MASK_LAYER_ID, { kind: "labels", label: "Cell boundaries", pinned: true });
    stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
    stack.register("surf-1", { kind: "surface", label: "Nuclei surface" });
    stack.register("proj-1", { kind: "image", label: "Max projection" });

    const error = attempt(() => ctx.PlexoraLayerManager.init(stack));
    check("a raster stored above a surface does not stop the panel's init",
        error === null, error ? `${error.name}: ${error.message}` : "");
    check("...and the cards come out grouped by surface, the surface first",
        cards().join(",") === `surf-1,proj-1,${REFERENCE_LAYER_ID}`, cards().join(" > "));

    errors.length = 0;
    const later = attempt(() => stack.setOpacity("proj-1", 0.5));
    check("an opacity change afterwards repaints without a listener failing",
        later === null && errors.length === 0,
        later ? `${later.name}: ${later.message}` : errors.slice(0, 1).join(""));
    check("...and the eye state still reaches the card",
        (() => {
            attempt(() => stack.setVisible("proj-1", false));
            const card = ctx.document.getElementById("layer_card_list").children
                .find((c) => c.getAttribute("data-layer-card") === "proj-1");
            return Boolean(card?.classList.contains("is-layer-off"));
        })(),
        "paint has to reach its class toggles");
}


// -- a claimed points layer, then an image added in the session -----------
//
// The transcripts plugin claims its points layer, so it has a card; then
// "+ Add Layer" registers an H&E, which lands on top of the stack.

{
    const { ctx, errors, cards } = page();
    const { LayerStack, REFERENCE_LAYER_ID, CENTROID_LAYER_ID } = ctx.PlexoraLayerStack;
    const stack = new LayerStack();
    stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "morphology" });
    stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
    stack.register("transcripts", { kind: "points", label: "Transcripts" });
    stack.claim("transcripts", "transcripts");
    const boot = attempt(() => ctx.PlexoraLayerManager.init(stack));
    check("a project with a claimed points layer boots the panel",
        boot === null, boot ? `${boot.name}: ${boot.message}` : "");

    errors.length = 0;
    const added = attempt(() => stack.register("he", { kind: "image", label: "H&E" }));
    check("an image added above a claimed points layer repaints without a listener failing",
        added === null && errors.length === 0,
        added ? `${added.name}: ${added.message}` : errors.slice(0, 1).join(""));
    check("...and the points card stays above the image cards",
        cards().join(",") === `transcripts,he,${REFERENCE_LAYER_ID}`, cards().join(" > "));
}


if (failures.length) {
    console.log(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
console.log("\nall checks passed");
