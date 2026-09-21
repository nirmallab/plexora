/**
 * Two channel sidebars on one page never touch each other's rows.
 *
 * `ViewerSidebar` has always supported a second scoped instance -- `{root,
 * idPrefix}` -- and Figure Builder's Quick Edit has shipped one for a while.
 * That was safe only because Quick Edit's copy lives in a modal the viewer's
 * own sidebar is not inside of: the viewer's `root` is the whole `document`,
 * so anything it resolves by CSS selector rather than by prefixed id can find
 * the other instance's markup.
 *
 * Three per-slot lookups did exactly that. They read
 * `.channel-slot[data-slot="N"]`, and a layer's channel panel mounts its rows
 * in the SAME document, in a card ABOVE the base image's card -- so
 * `querySelector` reaches the layer's row first and the reference image's
 * `syncSlotDom` rewrites a layer channel's colour, marker and enabled state.
 * Nothing throws; the wrong row simply moves.
 *
 * The fix is an id per row and `el()` for every lookup, which is already
 * root- and prefix-aware. These checks are what keeps a fourth lookup from
 * being written as a selector.
 *
 * The other half is `destroy()`. The constructor registers a `window`
 * listener for the HD toggle and there was no way to take it off, which was
 * fine while every listening instance outlived the page. A layer's panel does
 * not -- it goes away with its card -- and a dead instance still listening
 * goes on remapping slots whose DOM has been removed.
 *
 * Run directly:  node tests/js/sidebar_scoping_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

// -- enough DOM to hold two copies of the same markup ---------------------

function makeNode(tag) {
    const node = {
        tagName: String(tag).toUpperCase(),
        children: [],
        parentNode: null,
        attributes: {},
        dataset: {},
        style: {
            _held: new Map(),
            setProperty(name, value) { this._held.set(name, String(value)); },
            getPropertyValue(name) { return this._held.get(name) ?? ""; },
        },
        _classes: new Set(),
        _listeners: {},
        innerHTML: "",
        checked: false,
        disabled: false,
        title: "",
        type: "",
        get classList() {
            return {
                add: (...n) => n.forEach((x) => node._classes.add(x)),
                remove: (...n) => n.forEach((x) => node._classes.delete(x)),
                contains: (x) => node._classes.has(x),
                toggle: (x, on) => {
                    const next = on === undefined ? !node._classes.has(x) : Boolean(on);
                    if (next) node._classes.add(x); else node._classes.delete(x);
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
        addEventListener(type, fn) {
            (node._listeners[type] = node._listeners[type] || []).push(fn);
        },
        /** `[id="x"]` and a single class, which is all this file asks for. */
        querySelector(selector) {
            return descend(node, matcher(selector))[0] || null;
        },
        querySelectorAll(selector) { return descend(node, matcher(selector)); },
    };
    return node;
}

function matcher(selector) {
    const byId = /^\[id="([^"]+)"\]$/.exec(String(selector).trim());
    if (byId) return (n) => n.getAttribute("id") === byId[1];
    const byClass = String(selector).trim().replace(/^\./, "");
    return (n) => n._classes.has(byClass);
}

function descend(root, hits, out = []) {
    for (const child of root.children || []) {
        if (hits(child)) out.push(child);
        descend(child, hits, out);
    }
    return out;
}

// -- the page --------------------------------------------------------------

const body = makeNode("div");
const windowListeners = {};

const ctx = {
    console, Math, Number, Object, Array, Set, Map, JSON, String, Boolean,
    Promise, parseFloat, parseInt,
    document: {
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => descend(body, (n) => n.getAttribute("id") === id)[0] || null,
        // Deliberately present and document-wide: it is what the old `q()`
        // lookups used, and it is the leak. Without it here, reverting the fix
        // would throw rather than quietly rewrite the wrong row -- which is a
        // failure for the wrong reason.
        querySelector: (selector) => descend(body, matcher(selector))[0] || null,
    },
    // The two control classes a slot builds, stubbed to what the sidebar asks
    // of them: a value it can write back, and a destroy() for the unmount.
    ColorSwatchPicker: class { constructor(mount, o) { this.value = o.value; mount.appendChild(makeNode("div")); } setValue(v) { this.value = v; } destroy() { this.destroyed = true; } },
    SearchableSelect: class { constructor(mount, o) { this.value = o.value; mount.appendChild(makeNode("div")); } setValue(v) { this.value = v; } setOptions() {} destroy() { this.destroyed = true; } },
    PlexoraSlider: class { constructor(el) { this.el = { style: { setProperty() {} } }; this.target = el; } setBounds() {} set() {} blurFieldsOnEnter() { return this; } destroy() { this.destroyed = true; } },
    ChannelList: { events: { CHANNELS_CHANGE: "c", COLOR_TRANSFER_CHANGE: "t", BRUSH_MOVE: "b" } },
    d3: { rgb: (r, g, b) => ({ r, g, b }) },
    _: { pull: (list, value) => list.filter((x) => x !== value) },
};
ctx.window = ctx;
ctx.globalThis = ctx;
ctx.window.addEventListener = (type, fn) => {
    (windowListeners[type] = windowListeners[type] || []).push(fn);
};
ctx.window.removeEventListener = (type, fn) => {
    const held = windowListeners[type] || [];
    const at = held.indexOf(fn);
    if (at >= 0) held.splice(at, 1);
};
ctx.window.clearTimeout = () => {};
ctx.window.setTimeout = () => 0;

const context = createContext(ctx);
runInContext(`${readFileSync(join(VIEWS, "viewerSidebar.js"), "utf8")}
    ;globalThis.ViewerSidebar = ViewerSidebar;`, context, { filename: "viewerSidebar.js" });
const ViewerSidebar = context.ViewerSidebar;

// -- two instances over one document ---------------------------------------

const noopLayer = {
    imageBitRange: [0, 65536],
    getFullChannelName: (name) => name,
    getSavedChannelList: async () => [],
    saveChannelList: async () => null,
};
const noopChannels = () => ({
    image_channels: {}, rangeConnector: {}, colorConnector: {},
    selections: [], sel: {}, hasChannelGMM: {},
    ensureChannelStats: async () => {}, getAndDrawChannelGMM: async () => {},
});
const noopBus = { trigger() {} };

function mount(idPrefix, root) {
    const list = makeNode("div");
    list.setAttribute("id", `${idPrefix}channel_slot_list`);
    (root === context.document ? body : root).appendChild(list);
    const sidebar = new ViewerSidebar({}, ["DAPI", "CD3"], noopLayer, noopBus,
        noopChannels(), { root, idPrefix, persist: false });
    const slot = {
        index: 0, name: "DAPI", color: { r: 1, g: 2, b: 3 }, colorHex: "#010203",
        enabled: true, visible: true, expanded: false, range: [0, 255],
        userColorChanged: false, userRangeChanged: false,
        autoLeveled: false, autoLeveling: false, preAutoRange: null,
    };
    sidebar.channelSlots = [slot];
    list.appendChild(sidebar.createChannelSlot(slot));
    return { sidebar, slot, list };
}

// The layer's panel is mounted FIRST and its root is a node inside the same
// document -- which is the real arrangement: layer cards sit above the base
// image's card in `#layer_card_list`.
const panel = makeNode("div");
body.appendChild(panel);
const layer = mount("layer_demo_", panel);
const reference = mount("", context.document);

check("each row carries this instance's own prefixed id",
    layer.list.children[0].getAttribute("id") === "layer_demo_channel_slot_0"
    && reference.list.children[0].getAttribute("id") === "channel_slot_0",
    "`data-slot` alone is not unique across two copies of the markup");

check("an instance resolves its own row",
    reference.sidebar.slotRow(0) === reference.list.children[0]
    && layer.sidebar.slotRow(0) === layer.list.children[0]);

reference.slot.colorHex = "#ff0000";
reference.slot.enabled = false;
reference.sidebar.syncSlotDom(reference.slot);
check("syncSlotDom writes its own row and not the other instance's",
    reference.list.children[0].style.getPropertyValue("--slot-color") === "#ff0000"
    && layer.list.children[0].style.getPropertyValue("--slot-color") === "#010203",
    "the document-rooted instance used to reach the layer's row first");
check("...and the other instance's row keeps its enabled state",
    !layer.list.children[0]._classes.has("is-disabled")
    && reference.list.children[0]._classes.has("is-disabled"));

layer.slot.expanded = true;
layer.sidebar.applySlotExpansion(layer.slot);
check("applySlotExpansion expands its own row only",
    layer.list.children[0].querySelector(".channel-slot-detail")._classes.has("is-expanded")
    && !reference.list.children[0].querySelector(".channel-slot-detail")._classes.has("is-expanded"));

layer.slot.preAutoRange = { range: [1, 2] };
layer.sidebar.syncSlotAutoButton(layer.slot);
check("the Auto button swapped is its own",
    layer.list.children[0].querySelector(".slider-auto-button").dataset.state === "revert"
    && reference.list.children[0].querySelector(".slider-auto-button").dataset.state === "auto",
    "Revert offered on the wrong slot restores a range that slot never had");

// -- unmount ---------------------------------------------------------------

check("an unpinned instance listens for the HD toggle",
    (windowListeners["plexora:hd-mode-changed"] || []).length === 2,
    "both instances follow the viewer's domain");

layer.sidebar.destroy();
check("destroy() takes the HD listener back off",
    (windowListeners["plexora:hd-mode-changed"] || []).length === 1,
    "a dead panel's listener goes on remapping slots whose DOM is gone");
const survivor = (windowListeners["plexora:hd-mode-changed"] || [])[0];
layer.slot.range = [10, 20];
reference.slot.range = [10, 20];
survivor?.({ detail: { enabled: true } });
check("...and the one still mounted is the one that kept listening",
    reference.sidebar._hdModeListener === survivor
    && layer.sidebar._hdModeListener === null,
    "unmounting the layer's panel must not deafen the viewer's own sidebar");
check("destroy() is safe to call twice",
    (() => { try { layer.sidebar.destroy(); return true; } catch { return false; } })(),
    "a card can be removed by its X and again by the list noticing it is gone");

console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
