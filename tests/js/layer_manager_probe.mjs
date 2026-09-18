/**
 * The Layers panel: what a card says, and which drags it refuses.
 *
 * Four rules, and each has a silent failure mode.
 *
 *   **The top card is the top layer.** The stack is ordered bottom-first and the
 *   list reads downwards, so the DOM order is reversed on the way out. Getting
 *   it backwards draws the picture upside down and looks like a rendering bug
 *   rather than a list bug.
 *
 *   **A cross-surface drag is refused at the drag.** Tiles composite inside
 *   OSD's world and overlays are drawn on a canvas above it, so a points layer
 *   cannot be put underneath an image however the list is arranged. Allowing the
 *   drag and ignoring it is a reorder that silently does nothing.
 *
 *   **"Aligned by assumption" is said out loud.** A layer with no transform has
 *   never been registered against the reference image. Plexora asserted that
 *   everywhere and never said so; this line is the first thing that makes it
 *   visible, and it must not quietly become "Registered".
 *
 *   **A synthesized layer has no X.** The way to remove the mask layer is to
 *   remove the mask. An X that refused would be worse than no X.
 *
 * Run directly:  node tests/js/layer_manager_probe.mjs
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
        style: {},
        dataset: {},
        _classes: new Set(),
        _listeners: {},
        textContent: "",
        _html: "",
        // A real accessor, because `innerHTML = ""` is how the panel clears the
        // list before a rebuild. A plain property there would let cards
        // accumulate silently, and every assertion would then be reading the
        // first render rather than the current one.
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
        addEventListener(type, fn) {
            (node._listeners[type] = node._listeners[type] || []).push(fn);
        },
        click() { (node._listeners.click || []).forEach((fn) => fn({})); },
        // Enough for the probe's own walking; nothing in layerManager uses it.
        querySelectorAll() { return []; },
    };
    Object.defineProperty(node, "firstChild", { get: () => node.children[0] || null });
    return node;
}

function findAll(root, predicate, out = []) {
    for (const child of root.children || []) {
        if (predicate(child)) out.push(child);
        findAll(child, predicate, out);
    }
    return out;
}

const byId = {};
function sandbox() {
    ["layer_card_list", "layer_manager_section", "layer_manager_collapse"].forEach((id) => {
        byId[id] = makeNode(id === "layer_manager_collapse" ? "button" : "div");
    });
    const document = {
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => byId[id] || null,
    };
    const ctx = {
        document,
        console,
        Math,
        Number,
        Object,
        Array,
        Set,
        Map,
        JSON,
        String,
        Boolean,
    };
    // Sortable, stubbed down to the one thing worth testing: the options it was
    // handed. onMove is where a drag that cannot be honoured is refused, and
    // that refusal is not observable any other way.
    ctx.Sortable = function Sortable(element, options) {
        ctx.__sortable = { element, options };
        return { destroy() {} };
    };
    ctx.window = ctx;
    ctx.globalThis = ctx;
    return ctx;
}

const ctx = createContext(sandbox());
for (const file of ["cardList.js", "layerStack.js", "layerManager.js"]) {
    runInContext(readFileSync(join(VIEWS, file), "utf8"), ctx, { filename: file });
}

const { LayerStack, REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID } = ctx.PlexoraLayerStack;

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const cards = () => byId.layer_card_list.children
    .filter((c) => c.getAttribute("data-layer-card"))
    .map((c) => c.getAttribute("data-layer-card"));

const cardFor = (id) => byId.layer_card_list.children
    .find((c) => c.getAttribute("data-layer-card") === id);

const textIn = (card) =>
    findAll(card, (n) => n.tagName === "P").map((n) => n.textContent).join(" | ");

const buttons = (card) =>
    findAll(card, (n) => n.tagName === "BUTTON").map((n) => n.className);


// -- a project with an image, a mask and centroids -----------------------

const stack = new LayerStack();
stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "demo" });
stack.register(MASK_LAYER_ID, { kind: "labels", label: "Cell boundaries" });
stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
ctx.PlexoraLayerManager.init(stack);

check("every layer gets a card",
    cards().length === 3, cards().join(","));

check("the top card is the top layer",
    cards()[0] === CENTROID_LAYER_ID && cards()[2] === REFERENCE_LAYER_ID,
    `${cards().join(" > ")} -- the stack is bottom-first, the list reads downwards`);

check("the surfaces are separated by a labelled rule",
    byId.layer_card_list.children.filter((c) => c.getAttribute("data-surface-break")).length === 2,
    "the boundary a drag cannot cross has to be visible");

check("the rule says which side is which",
    byId.layer_card_list.children
        .filter((c) => c.getAttribute("data-surface-break"))
        .map((c) => c.textContent).join(" / "),
    byId.layer_card_list.children
        .filter((c) => c.getAttribute("data-surface-break"))
        .map((c) => c.textContent).join(" / "));


// -- what the card says about registration --------------------------------

check("the reference layer says it is the reference",
    textIn(cardFor(REFERENCE_LAYER_ID)).includes("Reference layer"));

check("an unregistered layer says so, rather than nothing",
    textIn(cardFor(MASK_LAYER_ID)).includes("Aligned by assumption"),
    "this is what the viewer has always silently assumed");

stack.setTransform(MASK_LAYER_ID, [1, 0, 0, 1, 500, -12]);
ctx.PlexoraLayerManager.render();
check("a registered layer says where it was put",
    textIn(cardFor(MASK_LAYER_ID)).includes("moved 500, -12"),
    textIn(cardFor(MASK_LAYER_ID)));

stack.setTransform(MASK_LAYER_ID, [1, 0, 0.5, 1, 0, 0]);
ctx.PlexoraLayerManager.render();
check("a transform OSD cannot draw is refused on the card",
    textIn(cardFor(MASK_LAYER_ID)).includes("Cannot be drawn: shear"),
    textIn(cardFor(MASK_LAYER_ID)));
check("and the card is marked for it",
    cardFor(MASK_LAYER_ID).classList.contains("is-undrawable"));

stack.setTransform(MASK_LAYER_ID, null);
ctx.PlexoraLayerManager.render();


// -- the controls ---------------------------------------------------------

check("a synthesized layer has no remove button",
    !buttons(cardFor(MASK_LAYER_ID)).some((c) => c.includes("layer-card-remove")),
    "the way to remove the mask layer is to remove the mask");

stack.register("he", { kind: "image", label: "H&E" });
ctx.PlexoraLayerManager.render();
check("a registered layer does have one",
    buttons(cardFor("he")).some((c) => c.includes("layer-card-remove")));

check("every card has a lock",
    buttons(cardFor("he")).some((c) => c.includes("layer-card-lock")));

{
    const card = cardFor("he");
    const eye = findAll(card, (n) => n.className?.includes?.("layer-card-eye"))[0];
    eye.click();
    check("the eye hides the layer and marks the card",
        stack.get("he").visible === false && card.classList.contains("is-layer-off"));
    eye.click();
    check("and shows it again", stack.get("he").visible === true);
}

{
    const card = cardFor("he");
    const chevron = findAll(card, (n) => n.className?.includes?.("layer-card-collapse"))[0];
    chevron.click();
    check("the chevron folds the card", card.classList.contains("is-collapsed"));
    chevron.click();
    check("and unfolds it", !card.classList.contains("is-collapsed"));
}

{
    const card = cardFor("he");
    const lock = findAll(card, (n) => n.className?.includes?.("layer-card-lock"))[0];
    lock.click();
    check("the padlock pins the card", card.classList.contains("is-locked"));
}


// -- a change made elsewhere shows up here --------------------------------

{
    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("a visibility change from anywhere repaints the panel",
        cardFor(REFERENCE_LAYER_ID).classList.contains("is-layer-off"),
        "the panel subscribes rather than holding a copy");
    stack.setVisible(REFERENCE_LAYER_ID, true);
}


// -- removing --------------------------------------------------------------

{
    const card = cardFor("he");
    const x = findAll(card, (n) => n.className?.includes?.("layer-card-remove"))[0];
    x.click();
    check("removing a layer takes its card with it",
        !stack.has("he") && !cards().includes("he"), cards().join(","));
}


// -- which drags are refused ----------------------------------------------

{
    const onMove = ctx.__sortable?.options?.onMove;
    check("the list is sortable by its grip alone",
        ctx.__sortable?.options?.handle === ".layer-card-grip"
        && ctx.__sortable?.options?.draggable === ".layer-card",
        "a click anywhere else in the header still has to reach the button it landed on");

    const within = (a, b) => onMove({ dragged: cardFor(a), related: cardFor(b) });

    check("a drag within one surface is allowed",
        within(REFERENCE_LAYER_ID, MASK_LAYER_ID) === true,
        "both are tiles");

    check("a drag across the surface boundary is refused",
        within(CENTROID_LAYER_ID, MASK_LAYER_ID) === false,
        "overlays are drawn above the world; a points layer cannot go under an image");

    check("a drag onto the separator itself is refused",
        onMove({
            dragged: cardFor(REFERENCE_LAYER_ID),
            related: byId.layer_card_list.children.find(
                (c) => c.getAttribute("data-surface-break")),
        }) === false);

    const lock = findAll(cardFor(MASK_LAYER_ID),
        (n) => n.className?.includes?.("layer-card-lock"))[0];
    lock.click();
    check("a pinned layer refuses to be dragged at all",
        within(MASK_LAYER_ID, REFERENCE_LAYER_ID) === false);
    lock.click();
    check("and can be dragged again once unpinned",
        within(MASK_LAYER_ID, REFERENCE_LAYER_ID) === true);
}


console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
