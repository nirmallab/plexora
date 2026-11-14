/**
 * Removing a plugin-drawn layer from the Layers panel stops it being drawn.
 *
 * A transcripts layer and a Visium HD spot or bin layer are points layers that
 * core does not draw: the plugin claims the layer, which earns it a card, and
 * draws it itself -- a WebGL point renderer, a density or bin raster through
 * `ctx.layers.addTiled`, or an overlay on core's OverlayHost tagged with the
 * layer's id. The card's eye reaches the plugin through `onLayerChange`, where
 * `syncVisibility` reads the stack record back.
 *
 * The card's X (layerManager `removeLayer`) deletes the layer on the server,
 * takes away core's own world item for it, and unregisters it from the stack.
 * For a plugin-drawn layer that has to stop the picture as well: a card, an
 * eye and a stored layer that are all gone while the molecules, spots or bins
 * stay on the image leave the user nothing to hide them with but a reload.
 *
 * Run directly:  node tests/js/layer_remove_plugin_drawing_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");
const PLUGINS = join(REPO, "plexora/plugins");

// -- a DOM with just enough of a tree to stack cards in -------------------
// (the same shape as layer_manager_cross_surface_order_probe.mjs)

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
 * A fresh page: the Layers panel, the layer stack, and one plugin's sidebar
 * controller, with the server's DELETE answering ok and the confirm dialog
 * answering Remove.
 */
function page(pluginFile) {
    const byId = {};
    ["layer_card_list", "layer_manager_section", "layer_manager_collapse"].forEach((id) => {
        byId[id] = makeNode(id === "layer_manager_collapse" ? "button" : "div");
    });
    const errors = [];
    const requests = [];
    const ctx = {
        document: {
            createElement: (tag) => makeNode(tag),
            getElementById: (id) => byId[id] || null,
        },
        console: { ...console, error: (...args) => { errors.push(args.map(String).join(" ")); } },
        Math, Number, Object, Array, Set, Map, JSON, String, Boolean, Promise,
    };
    ctx.Sortable = function Sortable() { return { destroy() {} }; };
    ctx.PopoverPortal = { attach: (el) => el, detach: () => {}, root: () => null };
    ctx.ColorSwatchPicker = function ColorSwatchPicker(mount) {
        mount.appendChild(ctx.document.createElement("button"));
        return { setValue() {}, destroy() {} };
    };
    ctx.ColorSwatchPicker.DEFAULT_PRESETS = ["#ffffff"];
    ctx.requestAnimationFrame = (fn) => fn();
    ctx.setTimeout = () => 0;
    ctx.clearTimeout = () => {};
    ctx.plexoraUrl = (path) => `/${path}`;
    ctx.fetch = (url, options = {}) => {
        requests.push(`${options.method || "GET"} ${url}`);
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    };
    ctx.flaskVariables = { datasource: "sample" };
    ctx.PlexoraConfirm = { ask: async () => true };
    ctx.__plexora = {
        seaDragonViewer: { viewerManagerVMain: { tiledLayers: new Map() } },
        adoptLayers: async () => [],
    };
    ctx.window = ctx;
    ctx.globalThis = ctx;
    createContext(ctx);
    for (const file of ["slider.js", "cardList.js", "layerStack.js", "layerManager.js"]) {
        runInContext(readFileSync(join(VIEWS, file), "utf8"), ctx, { filename: file });
    }
    runInContext(readFileSync(join(PLUGINS, pluginFile), "utf8"), ctx, { filename: pluginFile });
    const list = byId.layer_card_list;
    const cards = () => list.children
        .map((c) => c.getAttribute("data-layer-card"))
        .filter(Boolean);
    const removeButton = (id) => {
        const card = list.children.find((c) => c.getAttribute("data-layer-card") === id);
        return card ? findAll(card, (n) => n._classes?.has?.("layer-card-remove"))[0] || null : null;
    };
    return { ctx, errors, requests, cards, removeButton };
}

/** What a plugin's layer object exposes to its sidebar controller, recorded. */
function fakePluginLayer(layerId) {
    return {
        layerId,
        visible: true,
        destroyed: false,
        state: { opacity: 1 },
        setVisible(on) { this.visible = Boolean(on); },
        setOpacity(value) { this.state.opacity = value; },
        syncTransform() {},
        destroy() { this.destroyed = true; },
        show() {},
    };
}

/** A sidebar controller of the real class, wired the way `start` wires it. */
function controllerFor(ctx, stack, ClassName, layer) {
    const controller = Object.create(ctx[ClassName].prototype);
    controller.ctx = { layers: { get: (id) => stack.get(id) } };
    controller.layer = layer;
    controller.paintControls = () => {};
    controller.save = () => {};
    controller.opacity = null;
    // As `start` does: ctx.layers.onLayerChange(() => this.syncVisibility()).
    stack.subscribe(() => controller.syncVisibility());
    return controller;
}

const settle = async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}


// -- a Xenium run: the transcripts plugin's molecules ----------------------

{
    const { ctx, errors, requests, cards, removeButton } = page(
        "transcripts/static/transcriptsSidebarController.js");
    const { LayerStack, REFERENCE_LAYER_ID, CENTROID_LAYER_ID } = ctx.PlexoraLayerStack;
    const stack = new LayerStack();
    stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "morphology" });
    stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
    stack.register("transcripts", {
        kind: "points", label: "Transcripts",
        spec: { id: "transcripts", kind: "points", modality: "transcripts" },
    });
    // TranscriptLayer.attach: claim the layer, which earns it a card.
    stack.claim("transcripts", "transcripts");
    const molecules = fakePluginLayer("transcripts");
    controllerFor(ctx, stack, "TranscriptsSidebarController", molecules);
    ctx.PlexoraLayerManager.init(stack);

    const button = removeButton("transcripts");
    check("the claimed transcripts layer has a card with an X", Boolean(button),
        cards().join(" > "));
    button?.click();
    await settle();

    check("the X deletes the transcripts layer on the server",
        requests.includes("DELETE /project/sample/layers/transcripts"), requests.join(", "));
    check("...and the layer leaves the stack and the panel",
        !stack.has("transcripts") && !cards().includes("transcripts"), cards().join(" > "));
    check("the transcripts panel stops drawing the molecules of a layer that was removed",
        !molecules.visible || molecules.destroyed,
        `renderer visible=${molecules.visible} destroyed=${molecules.destroyed}`
        + (errors.length ? `  errors: ${errors.slice(0, 2).join(" | ")}` : ""));
}


// -- a Visium HD run: bins through addTiled, spots on core's overlay -------

{
    const { ctx, errors, requests, cards, removeButton } = page(
        "visium_hd/static/visiumHdSidebarController.js");
    const { LayerStack, OverlayHost, REFERENCE_LAYER_ID, CENTROID_LAYER_ID } = ctx.PlexoraLayerStack;
    const stack = new LayerStack();
    stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "H&E" });
    stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
    stack.register("bins", {
        kind: "points", label: "Visium HD bins",
        spec: { id: "bins", kind: "points", modality: "visium_hd" },
    });
    stack.claim("bins", "visium_hd");
    const bins = fakePluginLayer("bins");
    controllerFor(ctx, stack, "VisiumHdSidebarController", bins);

    // SpotLayer.attach: an overlay on core's host, tagged with its layer.
    const host = new OverlayHost({ stack, repaint: () => {} });
    host.add({ id: "visium_hd:spots:bins", layerId: "bins", kind: "points", draw: () => {} });
    const drawn = () => host.drawList().map((entry) => entry.id);
    ctx.PlexoraLayerManager.init(stack);

    check("the layer's overlay is drawn while the layer is there",
        drawn().includes("visium_hd:spots:bins"), drawn().join(", "));
    const button = removeButton("bins");
    check("the claimed Visium HD layer has a card with an X", Boolean(button),
        cards().join(" > "));
    button?.click();
    await settle();

    check("the X deletes the Visium HD layer on the server",
        requests.includes("DELETE /project/sample/layers/bins"), requests.join(", "));
    check("...and the Visium HD layer leaves the stack and the panel",
        !stack.has("bins") && !cards().includes("bins"), cards().join(" > "));
    check("an overlay drawn for a layer is not drawn once that layer is removed",
        !drawn().includes("visium_hd:spots:bins"), `drawList: ${drawn().join(", ")}`);
    check("the Visium HD panel stops drawing the bins of a layer that was removed",
        !bins.visible || bins.destroyed,
        `bins visible=${bins.visible} destroyed=${bins.destroyed}`
        + (errors.length ? `  errors: ${errors.slice(0, 2).join(" | ")}` : ""));
}


if (failures.length) {
    console.log(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
console.log("\nall checks passed");
