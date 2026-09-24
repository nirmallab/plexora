/**
 * The Layers panel: what gets a card, what a card says, and which drags it
 * refuses.
 *
 * Six rules, and each has a silent failure mode.
 *
 *   **The top card is the top layer, and the base image is the bottom one.**
 *   The stack is ordered bottom-first and the list reads downwards, so the DOM
 *   order is reversed on the way out. Getting it backwards draws the picture
 *   upside down and looks like a rendering bug rather than a list bug.
 *
 *   **The cell mask has no card.** There is one segmentation in a project and
 *   the Cells footer owns how it is drawn. A "Cell boundaries" card was a
 *   second eye and a second slider for a control that already existed, wired
 *   to nothing -- and two controls that disagree is worse than one.
 *
 *   **A cross-surface drag is refused at the drag.** Rasters composite inside
 *   OSD's world and points are drawn on a canvas above it, so a points layer
 *   cannot be put underneath an image however the list is arranged. Allowing
 *   the drag and ignoring it is a reorder that silently does nothing.
 *
 *   **Nothing goes under the base image.** It is the ground; a raster dropped
 *   beneath it would be fetched, decoded, drawn and never seen.
 *
 *   **"Aligned by assumption" is said out loud.** A layer with no transform has
 *   never been registered against the reference image. Plexora asserted that
 *   everywhere and never said so; this line is the first thing that makes it
 *   visible, and it must not quietly become "Registered".
 *
 *   **Cards are reconciled, not rebuilt.** A card holds markup somebody else
 *   owns -- the channel slots and their d3 sliders, a plugin's whole panel --
 *   and a wipe-and-rebuild leaves every handle held into it pointing at a node
 *   no longer on the page, with nothing reporting it. So the same node has to
 *   survive a re-render.
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
        // Enough of CSSStyleDeclaration for views/slider.js, which paints a
        // handle's position by writing two custom properties and nothing else.
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
        // Own text plus every descendant's, as the real one is. A card's title
        // holds its name in a span of its own now (so a tool card's shortcut
        // can be printed beside it without joining it), and a plain property
        // here would report every card as unnamed.
        get textContent() {
            return node._text + node.children.map((c) => c.textContent).join("");
        },
        set textContent(value) {
            node._text = String(value);
            node.children.splice(0).forEach((child) => { child.parentNode = null; });
        },
        _html: "",
        // A real accessor, because `innerHTML = ""` used to be how the panel
        // cleared the list before a rebuild. It no longer does, and this is
        // what would notice if it started again: a wipe here detaches every
        // child, so the reconcile checks below would fail rather than pass on
        // a fresh set of nodes that happen to look the same.
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
        remove() { node.parentNode?.removeChild(node); },
        removeAttribute(name) { delete node.attributes[name]; },
        addEventListener(type, fn) {
            (node._listeners[type] = node._listeners[type] || []).push(fn);
        },
        /**
         * Bubbles, and carries a target.
         *
         * It used to fire the one node's own listeners and stop. The card
         * HEADER now listens for clicks that land anywhere on it and decides
         * what to do by where they landed, so a probe that did not bubble
         * would pass every check below while the page did nothing at all --
         * and one that bubbled without a target would fold the card on every
         * button press.
         */
        click() {
            const event = { target: node, stopPropagation() {} };
            for (let el = node; el; el = el.parentNode) {
                (el._listeners.click || []).forEach((fn) => fn(event));
            }
        },
        /** Tag names and single classes, which is the whole of what the header
         *  handler's selector list is made of. */
        closest(selector) {
            const parts = String(selector).split(",").map((one) => one.trim());
            const hits = (n) => parts.some((one) => (one.startsWith(".")
                ? n._classes?.has?.(one.slice(1))
                : n.tagName === one.toUpperCase()));
            for (let n = node; n; n = n.parentNode) if (hits(n)) return n;
            return null;
        },
        // Enough for the probe's own walking; nothing in layerManager uses it.
        querySelectorAll() { return []; },
        /**
         * `[attr]` and nothing else, which is the whole of what layerManager
         * asks of a staged node: where the opacity control goes
         * (`[data-layer-opacity-slot]`), where the channel counter is
         * (`[data-layer-count]`), and whether the card staged an opacity
         * control of its own (`[data-layer-opacity]`). Returning null for
         * everything, as this used to, meant the card silently took the
         * fallback path on every one of them.
         */
        querySelector(selector) {
            const attr = /^\[([\w-]+)\]$/.exec(String(selector));
            if (!attr) return null;
            return findAll(node, (n) => n.getAttribute(attr[1]) !== null)[0] || null;
        },
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
    // The base image's opacity slider lives in a popover parked on a portal
    // rather than in the card -- stubbed to the two calls buildOpacityControl
    // makes, because what this probe is checking is that the TRACK IS NOT IN
    // THE CARD, and a real portal would need a real <body> to park it on.
    ctx.PopoverPortal = { attach: (el) => el, detach: () => {}, root: () => null };
    // The swatch on the fallback channel row (a layer with channels and no
    // panel module) and the ground dot in a card header. Stubbed to its
    // constructor, because what this probe asks of that row is that it EXISTS
    // -- but the options are kept, because the ground palette is BUILT here
    // and what it comes out as is this file's business.
    ctx.__pickers = [];
    ctx.ColorSwatchPicker = function ColorSwatchPicker(mount, options = {}) {
        mount.appendChild(ctx.document.createElement("button"));
        ctx.__pickers.push({ mount, options });
        return { setValue() {}, destroy() {} };
    };
    // The real palette, read out of the file that owns it rather than copied:
    // the ground swatches are built from it, so a copy here would agree with
    // itself forever while the two lists drifted apart -- and "the grid comes
    // out full" is a claim about the REAL number of colours. Loaded into a
    // context of its own because a top-level `class` is a lexical binding: run
    // in this one it would shadow the stub above and the probe would start
    // driving the real widget through a hand-rolled DOM.
    const palette = createContext({});
    runInContext(readFileSync(join(VIEWS, "colorSwatchPicker.js"), "utf8"),
        palette, { filename: "colorSwatchPicker.js" });
    ctx.ColorSwatchPicker.DEFAULT_PRESETS =
        runInContext("ColorSwatchPicker.DEFAULT_PRESETS", palette);
    ctx.requestAnimationFrame = (fn) => fn();
    ctx.window = ctx;
    ctx.globalThis = ctx;
    return ctx;
}

const ctx = createContext(sandbox());
// slider.js first, as base.html loads it: every opacity row in a card is one
// of its sliders, and a probe that left it out would pass while the page threw.
for (const file of ["slider.js", "cardList.js", "layerStack.js", "layerManager.js"]) {
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


// -- what index.html and a plugin stage for a card to swallow -------------
//
// `#layer_section_slot` holds markup that belongs INSIDE a card: the base
// image's channel list, a plugin's whole panel. The card moves it in, and the
// move is the point -- a clone would leave every handle the sidebar and the
// plugin took at build time pointing at the copy left behind, with nothing
// reporting it. So this stands in for the slot, and the lookups answer only
// while a node is still in it, exactly as a query scoped to the slot does.

const slot = makeNode("div");
const staged = { bodies: {}, modalities: {}, extras: {} };

function stage(into, key, node) {
    slot.appendChild(node);
    staged[into][key] = node;
    return node;
}

const inSlot = (node) => Boolean(node) && node.parentNode === slot;
const answer = (node) => (inSlot(node) ? node : null);

const baseBody = stage("bodies", REFERENCE_LAYER_ID, makeNode("div"));
// The two nodes index.html marks inside that body for the card to place: the
// line the opacity control shares with the rename button, and the channel
// counter that belongs on the card's last line rather than in its header.
const opacitySlot = makeNode("div");
opacitySlot.setAttribute("data-layer-opacity-slot", "");
baseBody.appendChild(opacitySlot);
const channelCount = makeNode("div");
channelCount.setAttribute("data-layer-count", "");
baseBody.appendChild(channelCount);
// A brightfield project stages no such line and no counter; it still has its
// own Reset button in the header, which is what keeps `extras` exercised.
const baseExtras = stage("extras", REFERENCE_LAYER_ID, makeNode("div"));
const txMount = stage("modalities", "transcripts", makeNode("div"));
const txExtras = makeNode("div");
txExtras.setAttribute("data-layer-extras", "");
txMount.appendChild(txExtras);

ctx.PlexoraLayerSections = {
    bodyFor: (id) => answer(staged.bodies[id]),
    bodyForModality: (modality) => answer(staged.modalities[modality]),
    extrasFor: (id) => answer(staged.extras[id]),
    extrasIn: (mount) => (mount?.children || [])
        .find((child) => child.getAttribute("data-layer-extras") !== null) || null,
};


// -- a project with an image, a mask and centroids -----------------------

const stack = new LayerStack();
stack.register(REFERENCE_LAYER_ID, { kind: "image", label: "demo" });
stack.register(MASK_LAYER_ID, { kind: "labels", label: "Cell boundaries", pinned: true });
stack.register(CENTROID_LAYER_ID, { kind: "points", label: "Cell centroids" });
ctx.PlexoraLayerManager.init(stack);

check("the base image's card swallows the staged channel markup",
    findAll(cardFor(REFERENCE_LAYER_ID), (n) => n === baseBody).length === 1
    && !inSlot(baseBody),
    "moved, not copied: channelList's slots hold their handles into these nodes");

check("and its staged header controls land in the header",
    findAll(cardFor(REFERENCE_LAYER_ID), (n) => n === baseExtras).length === 1
    && !inSlot(baseExtras),
    "a brightfield card's Reset, a plugin's kebab -- beside the eye");

check("a plain project has exactly one card: its image",
    cards().join(",") === REFERENCE_LAYER_ID, cards().join(","));

check("the cell mask gets no card",
    !cards().includes(MASK_LAYER_ID),
    "there is one segmentation and the Cells footer owns how it is drawn -- "
    + "a card here would be a second eye for a control that already exists");

check("a points layer gets no card",
    !cards().includes(CENTROID_LAYER_ID),
    "an eye and an opacity slider control nothing a core renderer draws for "
    + "points -- whoever owns them draws them, from inside their own card");

check("one surface draws no separator",
    byId.layer_card_list.children.filter((c) => c.getAttribute("data-surface-break")).length === 0,
    "the rule shows where a drag stops; with one surface there is no boundary "
    + "to show and it was a caption over the whole list");


// -- a registered raster --------------------------------------------------

stack.register("he", { kind: "image", label: "H&E" });

check("a registered layer takes its place above the base image",
    cards().join(",") === `he,${REFERENCE_LAYER_ID}`,
    `${cards().join(" > ")} -- the stack is bottom-first, the list reads downwards`);

check("the base image's card is last whatever the stack order says",
    cards()[cards().length - 1] === REFERENCE_LAYER_ID,
    "it is the ground; a list whose bottom row is not the bottom of the "
    + "picture makes the one thing this panel exists to show a lie");


// -- a points layer something actually draws ------------------------------
//
// The exception, and the reason it is an exception rather than a kind: the
// carded set is standing in for "will these controls do anything", and for a
// points layer the answer depends on whether anybody renders it. The
// transcripts plugin does, and says so with `LayerStack.claim`; nothing
// claims the centroids, because the Cells footer owns that control and a
// second eye for it would be two answers to one question.

stack.register("transcripts", { kind: "points", label: "Transcripts" });
check("an unclaimed points layer still gets no card",
    !cards().includes("transcripts"),
    "registering a layer is not the same as drawing it");

stack.claim("transcripts", "transcripts");
check("a claimed points layer gets an ordinary card",
    cards().includes("transcripts"), cards().join(","));
check("...at the top, because points draw over images",
    cards()[0] === "transcripts", cards().join(" > "));
check("...with the eye and the drag every card has",
    buttons(cardFor("transcripts")).some((c) => c.includes("layer-card-eye")),
    buttons(cardFor("transcripts")).join(","));
check("...and the centroids beside it still get none",
    !cards().includes(CENTROID_LAYER_ID),
    "core draws them, and the Cells footer is where they are switched");
check("...it is drawn over the image, so a drag cannot put it under one",
    cardFor("transcripts").getAttribute("data-layer-surface") === "overlay",
    cardFor("transcripts").getAttribute("data-layer-surface"));

check("two surfaces are grouped, with nothing drawn between them",
    byId.layer_card_list.children.every((c) => c.getAttribute("data-layer-card")),
    "the labelled rule that used to sit here explained a refusal most users "
    + "never meet, in a sentence, in a 300px column -- the grouping is what "
    + "is left of the boundary, and the refusal still stands in onMove");

stack.claim("transcripts", null);
check("giving the claim up takes the card away",
    !cards().includes("transcripts"),
    "a plugin torn down must not leave a card behind that nothing answers");
stack.unregister("transcripts");


// -- a layer whose plugin staged a panel for it ---------------------------

{
    // No claim: the panel is in the page from the moment it loads, and the
    // plugin claims its layer only once it has something to draw. A layer
    // still being built has to carry its own "Preparing..." line before then,
    // which means it has to have a card before then.
    stack.register("tx", { kind: "points", label: "Transcripts",
                           spec: { modality: "transcripts", status: "pending" } });

    check("a staged panel earns a card on its own, before any claim",
        cards().includes("tx"), cards().join(","));

    const card = cardFor("tx");
    check("...and the panel itself becomes the card's body",
        findAll(card, (n) => n === txMount).length === 1 && !inSlot(txMount),
        "moved, not copied: the plugin's controller holds handles into it");
    check("...with the panel's own header control beside the eye",
        findAll(card, (n) => n === txExtras).length === 1
        && txExtras.parentNode !== txMount,
        "taken out of the body before the body was adopted, or it would ride in");
    check("...and no opacity slider of core's",
        !findAll(card, (n) => n.tagName === "INPUT").some((n) => n.type === "range"),
        "the plugin's own slider already writes the stack and reads it back; "
        + "a second one here would be two controls for one number");

    ctx.PlexoraLayerManager.render();
    check("...surviving a re-render, panel and all",
        cardFor("tx") === card && findAll(card, (n) => n === txMount).length === 1);

    stack.unregister("tx");
    check("and the card goes when the layer does",
        !cards().includes("tx"));
}


// -- cards are reconciled, not rebuilt ------------------------------------

{
    const before = cardFor("he");
    const beforeBase = cardFor(REFERENCE_LAYER_ID);
    ctx.PlexoraLayerManager.render();
    check("a re-render keeps the very same card node",
        cardFor("he") === before && cardFor(REFERENCE_LAYER_ID) === beforeBase,
        "cards hold markup somebody else owns handles into; rebuilding them "
        + "leaves every one of those pointing at a node off the page");
}


// -- what the card says about registration --------------------------------

check("the reference layer says it is the reference",
    textIn(cardFor(REFERENCE_LAYER_ID)).includes("Reference layer"));

check("an unregistered layer says so, rather than nothing",
    textIn(cardFor("he")).includes("Aligned by assumption"),
    "this is what the viewer has always silently assumed");

stack.setTransform("he", [1, 0, 0, 1, 500, -12]);
ctx.PlexoraLayerManager.render();
check("a registered layer says where it was put",
    textIn(cardFor("he")).includes("moved 500, -12"),
    textIn(cardFor("he")));

stack.setTransform("he", [1, 0, 0.5, 1, 0, 0]);
ctx.PlexoraLayerManager.render();
check("a transform OSD cannot draw is refused on the card",
    textIn(cardFor("he")).includes("Cannot be drawn: shear"),
    textIn(cardFor("he")));
check("and the card is marked for it",
    cardFor("he").classList.contains("is-undrawable"));

stack.setTransform("he", null);
ctx.PlexoraLayerManager.render();


// -- the controls ---------------------------------------------------------

check("the base image layer has no remove button",
    !buttons(cardFor(REFERENCE_LAYER_ID)).some((c) => c.includes("layer-card-remove")),
    "the way to remove the image is to remove the sample");

const lockOn = (id) => findAll(cardFor(id),
    (n) => n.tagName === "BUTTON" && n.className.includes("layer-card-lock"))[0] || null;

check("the base image layer does have a padlock",
    Boolean(lockOn(REFERENCE_LAYER_ID)),
    "every card that can be dragged has one");

check("...and it is a real one",
    lockOn(REFERENCE_LAYER_ID)?.disabled !== true
    && lockOn(REFERENCE_LAYER_ID)?.getAttribute("aria-disabled") !== "true",
    "it used to be drawn stuck shut, because the row could not be dragged and "
    + "the padlock was the only thing on it that accounted for that; the row "
    + "can be dragged now, so a lock that could not be opened would be the lie");

check("the base image layer carries a ground swatch",
    Boolean(findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className?.includes?.("layer-card-ground"))[0]),
    "its channels carry coverage in their alpha now, so what shows through "
    + "where they have no signal is a choice somebody has to be able to make");

/** The palette the ground dot on this card was built with. */
function groundPalette(id) {
    const card = cardFor(id);
    const found = ctx.__pickers.find(
        (picker) => picker.mount.className?.includes?.("layer-card-ground")
            && findAll(card, (n) => n === picker.mount).length === 1);
    return found?.options?.presets || [];
}

{
    const presets = groundPalette(REFERENCE_LAYER_ID);
    const hexes = presets.map((preset) => preset.hex.toLowerCase());

    check("the ground palette opens with the slash, black and white",
        hexes[0] === "transparent" && hexes[1] === "#000000"
        && hexes[2] === "#ffffff",
        "those are the three a ground is actually set to; everything after "
        + "them is a colour the channel swatches already offer");

    check("...and fills its grid, with no hole in the last row",
        presets.length > 0 && presets.length % 4 === 0,
        "4 is GROUND_COLUMNS in layerManager.js, which hands it to the grid "
        + `as --ground-columns; this palette is ${presets.length} long. A `
        + "cell with nothing in it reads as a swatch that failed to load, so "
        + "a colour added to ColorSwatchPicker.DEFAULT_PRESETS has to be "
        + "matched by a change to the column count or by a fourth head entry");

    check("...and offers no colour twice",
        new Set(hexes).size === hexes.length,
        "white is on both lists and is filtered off this one -- a palette "
        + "with two whites in it has the user hunting for the difference");

    check("the reference image's slash is Default, not None",
        presets[0]?.label === "Default",
        "the canvas behind the scene's frame is always SOME colour; what the "
        + "slot does there is give back the one viewer.css picks when nothing "
        + "is stored, which is the only way to unset a ground once it is set");
}

check("the base image's card is titled Image, not the project",
    findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-title"))[0]?.textContent === "Image",
    "the layer is labelled 'demo' here, which is what the server fills it with "
    + "-- the project's name, which names the whole view and not one layer of it");

check("the channel counter moves onto the card's last line",
    channelCount.parentNode?.className?.includes?.("layer-card-footer") === true,
    "beside 'Reference layer': two captions, one at each end, where the "
    + "counter used to be crowded into the header beside the eye");

check("...and the alignment line is on that same line",
    findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-alignment"))[0]
        ?.parentNode === channelCount.parentNode);

check("the opacity control shares the line the rename button is on",
    buttons(opacitySlot).some((c) => c.includes("layer-opacity-trigger")),
    "one row for everything that acts on the whole image");

check("...and it is FIRST on that line",
    opacitySlot.children[0]?.className?.includes?.("layer-opacity-trigger") === true);

check("...so the base card grows no opacity row of its own",
    !findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-row")).length,
    "two sliders for one number is what this panel is undoing");

check("...and its track is not in the card at all",
    !findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.tagName === "INPUT" && n.type === "range").length,
    "it is in a popover on the portal, behind the value on the button");

check("a registered layer does have a remove button",
    buttons(cardFor("he")).some((c) => c.includes("layer-card-remove")));

check("and a padlock",
    buttons(cardFor("he")).some((c) => c.includes("layer-card-lock")));

check("and its own opacity slider",
    findAll(cardFor("he"), (n) => n.tagName === "INPUT").some((n) => n.type === "range"),
    "nothing else in the panel fades a registered raster");

check("...with a number beside it you can type into",
    findAll(cardFor("he"), (n) => n.tagName === "INPUT" && n.type === "number"
        && n.classList.contains("plx-number")
        && !n.classList.contains("layer-card-number")).length === 1,
    "0.35 is a value somebody copies from one layer to another, and the "
    + "<output> this replaced could only be dragged towards");


// -- the controls that make order mean something --------------------------

{
    //: Nothing offers a composite operation any more. A layer's channels
    //: composite among themselves the way the reference image's do, and where
    //: the layer SITS is the stack order and the opacity slider -- the Add /
    //: Over pair that used to be here made a multichannel layer show one
    //: channel at a time. See ViewerManager.addLayerChannelSet.
    const blendOptions = (id) => findAll(cardFor(id), (n) => n.tagName === "BUTTON")
        .map((n) => n.getAttribute("data-blend")).filter(Boolean);
    // The WINDOW's two boxes, not every number box in the card: every slider
    // in the app now carries one of its own, and the opacity row's is one.
    const numbers = (id) => findAll(cardFor(id), (n) => n.tagName === "INPUT")
        .filter((n) => n.type === "number" && n.className.includes("layer-card-number"));

    check("no card offers a blend at all",
        blendOptions("he").length === 0
        && blendOptions(REFERENCE_LAYER_ID).length === 0,
        "source-over between a layer's own channels shows one at a time");

    check("and a window to type, rather than a slider with no domain",
        numbers("he").length === 2,
        "the client has no bit range for a registered layer, and an empty box "
        + "is how the file's own window is asked for back");

    stack.register("slide", { kind: "image", label: "Slide",
                              spec: { render: { rgb: true } } });
    check("an rgb layer is offered no window at all",
        numbers("slide").length === 0,
        "its tiles are already the picture the scanner recorded; parse_style "
        + "applies lo/hi only to a channel plane");
    stack.unregister("slide");

    check("the base image layer has no window either",
        numbers(REFERENCE_LAYER_ID).length === 0,
        "it is the ground, and its own channels carry their own windows");
}

{
    const card = cardFor("he");
    const eye = findAll(card, (n) => n.className?.includes?.("layer-card-eye"))[0];
    eye.click();
    check("the eye hides the layer and marks the card",
        stack.get("he").visible === false && card.classList.contains("is-layer-off"));
    eye.click();
    check("and shows it again", stack.get("he").visible === true);
}

// -- one card open at a time ---------------------------------------------

const headerOf = (id) => cardFor(id).children
    .find((n) => n.className.includes("layer-card-header"));
const isOpen = (id) => !cardFor(id).classList.contains("is-collapsed");

check("a second layer's card arrives folded",
    !isOpen("he") && isOpen(REFERENCE_LAYER_ID),
    "nobody clicked it open, and two bodies of controls on a 300px column is "
    + "a scroll with the thing you came for somewhere in the middle");

{
    const card = cardFor("he");
    const chevron = findAll(card, (n) => n.className?.includes?.("layer-card-collapse"))[0];
    chevron.click();
    check("the chevron unfolds the card", isOpen("he"));
    check("...and folds every other one as it goes",
        !isOpen(REFERENCE_LAYER_ID),
        "one card open at a time is the whole point of folding on open");
    chevron.click();
    check("and folds it again", !isOpen("he"));
    check("...leaving nothing open, because folding is not opening",
        !isOpen(REFERENCE_LAYER_ID),
        "the user closed a card; that is not a request to open another");
}

{
    // The gesture the chevron is now only the smallest target for.
    headerOf(REFERENCE_LAYER_ID).click();
    check("clicking the header opens the card", isOpen(REFERENCE_LAYER_ID));
    headerOf(REFERENCE_LAYER_ID).click();
    check("and clicking it again folds it", !isOpen(REFERENCE_LAYER_ID));

    const title = findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-title"))[0];
    title.click();
    check("so does the title, on a card that does nothing else with it",
        isOpen(REFERENCE_LAYER_ID),
        "a layer card's title is an inert button; a tool card's selects");

    const eye = findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-eye"))[0];
    eye.click();
    check("...but the eye keeps its own click",
        isOpen(REFERENCE_LAYER_ID),
        "every control on the row is let through the header's handler");
    eye.click();

    const grip = findAll(cardFor(REFERENCE_LAYER_ID),
        (n) => n.className.includes("layer-card-grip"))[0];
    grip.click();
    check("...and so does the grip",
        isOpen(REFERENCE_LAYER_ID),
        "a drag begins there, and a drag that ends where it started is a click");
}

{
    const card = cardFor("he");
    const lock = findAll(card, (n) => n.className?.includes?.("layer-card-lock"))[0];
    lock.click();
    check("the padlock pins the card", card.classList.contains("is-locked"));
    lock.click();
}

{
    ctx.PlexoraLayerManager.setCollapsed(REFERENCE_LAYER_ID, true);
    check("a card can be folded from outside, for a tool needing the room",
        cardFor(REFERENCE_LAYER_ID).classList.contains("is-collapsed"),
        "toolLoader's collapseForNewTool, through viewerSidebar");
    ctx.PlexoraLayerManager.setCollapsed(REFERENCE_LAYER_ID, false);
}


// -- a change made elsewhere shows up here --------------------------------

{
    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("a visibility change from anywhere repaints the panel",
        cardFor(REFERENCE_LAYER_ID).classList.contains("is-layer-off"),
        "the panel subscribes rather than holding a copy");
    stack.setVisible(REFERENCE_LAYER_ID, true);
}


// -- the order pushed back onto the stack ---------------------------------

{
    ctx.PlexoraLayerManager.syncOrder();
    check("the base image is named at the position its card is in",
        stack.order()[0] === REFERENCE_LAYER_ID,
        "still the bottom here, because nothing has been dragged -- but it "
        + "arrives from the card list now rather than being hoisted in front "
        + "of it: " + stack.order().join(","));
    check("and the pinned mask is still on top of everything",
        stack.order()[stack.order().length - 1] === MASK_LAYER_ID,
        "it has no card to be dragged back up with, so the model holds it there");
}


// -- the base image, dragged ----------------------------------------------

{
    // THE CARDS ARE THE ORDER. Sortable moves a row and calls back, so a row
    // moved by hand and `syncOrder` called is exactly the gesture. The base
    // image's row used to be hoisted to the front of that list whatever the
    // DOM said, which made a drag of it a reorder that silently did nothing.
    //
    // The list reads TOP-first and the stack reads BOTTOM-first, so the base
    // image's card moving to the head of the list is it moving to the top of
    // the picture.
    const list = byId.layer_card_list;
    const row = cardFor(REFERENCE_LAYER_ID);
    const was = stack.order().slice();
    list.children.splice(list.children.indexOf(row), 1);
    list.children.unshift(row);
    ctx.PlexoraLayerManager.syncOrder();

    const order = stack.order();
    check("dragging the base image's card restacks the base image",
        order.indexOf(REFERENCE_LAYER_ID) > order.indexOf("he"),
        "above the registered slide it used to be the floor of: "
        + was.join(",") + " -> " + order.join(","));
    check("...and it is the only thing that moved",
        order.filter((id) => id !== REFERENCE_LAYER_ID).join(",")
        === was.filter((id) => id !== REFERENCE_LAYER_ID).join(","),
        order.join(","));

    // Put it back, so what follows reads the list it was written against.
    list.children.splice(list.children.indexOf(row), 1);
    list.children.push(row);
    ctx.PlexoraLayerManager.syncOrder();
    check("and dragging it back down puts it back",
        stack.order().join(",") === was.join(","),
        stack.order().join(","));
}


// -- which drags are refused ----------------------------------------------

{
    const onMove = ctx.__sortable?.options?.onMove;
    check("the list is sortable by its grip alone",
        ctx.__sortable?.options?.handle === ".layer-card-grip"
        && ctx.__sortable?.options?.draggable === ".layer-card",
        "a click anywhere else in the header still has to reach the button it landed on");

    stack.register("second", { kind: "image", label: "Second slide" });
    const move = (a, b, after = false) =>
        onMove({ dragged: cardFor(a), related: cardFor(b), willInsertAfter: after });

    check("a drag within one surface is allowed",
        move("he", "second") === true, "both are tiles");

    check("a drop UNDER the base image is allowed",
        move("he", REFERENCE_LAYER_ID, true) === true,
        "there can be something under the base image now: it composites as a "
        + "group over whatever that is, rather than onto its own black tile");

    check("and a drop above it, as before",
        move("he", REFERENCE_LAYER_ID, false) === true);

    check("the base image itself can be dragged",
        move(REFERENCE_LAYER_ID, "he") === true,
        "which image was imported first should not decide which can be on top");

    // Built by hand rather than taken off a card: this asserts the predicate
    // itself, which is what has to keep holding whatever is carded that day.
    const overlayCard = {
        getAttribute: (name) => (name === "data-layer-card" ? "somewhere-else"
            : name === "data-layer-surface" ? "overlay" : null),
    };
    check("a drag across the surface boundary is refused",
        onMove({ dragged: overlayCard, related: cardFor("he") }) === false,
        "points are drawn above the world; a points layer cannot go under an image");

    check("a drop onto anything in the list that is not a card is refused",
        onMove({ dragged: cardFor("he"), related: undefined }) === false,
        "a reorder whose target cannot be named cannot be honoured");

    const lock = findAll(cardFor("second"),
        (n) => n.className?.includes?.("layer-card-lock"))[0];
    lock.click();
    check("a pinned layer refuses to be dragged at all",
        move("second", "he") === false);
    lock.click();
    check("and can be dragged again once unpinned",
        move("second", "he") === true);
    stack.unregister("second");
}


// -- a layer's channel panel ----------------------------------------------
//
// The panel itself is views/layerChannelPanel.js, which mounts a whole
// ViewerSidebar and needs a DOM this hand-rolled one is not. What THIS file
// owns is the lifecycle: mounted once, moved rather than rebuilt when the
// card's body is refreshed, and destroyed when the card goes. Each has a
// silent failure mode -- a remount loses the user's slots and refetches every
// channel's stats, and a panel outliving its card keeps a window listener for
// the HD toggle.

{
    const mounts = [];
    const destroyed = [];
    ctx.PlexoraLayerChannels = {
        mount(options) {
            const node = makeNode("div");
            const footer = makeNode("div");
            node.appendChild(footer);
            const mounted = {
                node, footer, options,
                setAlignment(note) { footer.children.splice(0); footer.appendChild(note); },
                destroy() { destroyed.push(options.layer.id); },
            };
            mounts.push(mounted);
            return mounted;
        },
    };

    stack.register("mx", {
        kind: "image", label: "Multiplex",
        spec: { channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                           { name: "mx_1", src: "/mx/mx_1/" }] },
    });
    check("a channelled layer's card mounts the channel panel",
        mounts.length === 1 && mounts[0].options.layer.id === "mx");
    check("...and swallows the panel's node",
        mounts[0].node.parentNode !== null,
        "the panel carries the slot list, Add Channel and the counter");
    check("...with the alignment note on the panel's own last line",
        mounts[0].footer.children.some(
            (n) => n.className?.includes?.("layer-card-alignment")),
        "the counter has to stay inside the sidebar instance's root");

    check("a registered layer's ground dot offers None, not Default",
        groundPalette("mx")[0]?.label === "None",
        "it is transparent where its channels have no signal -- a real "
        + "answer and its default, not the absence of one; the rest of the "
        + "palette is the same list the reference image is offered");

    const first = mounts[0].node;
    // What `refreshBody` does on every change to the layer: wipe the body and
    // build it again.
    stack.register("mx", {
        kind: "image", label: "Multiplex",
        spec: { channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                           { name: "mx_1", src: "/mx/mx_1/" }] },
        transform: [1, 0, 0, 1, 12, 4],
    });
    check("a rebuilt card gets the SAME panel back, not a second one",
        mounts.length === 1 && cardFor("mx") && first.parentNode !== null,
        "a remount throws away the user's slots and refetches every channel");

    const rgbBefore = mounts.length;
    stack.register("rgb_layer", {
        kind: "image", label: "Second slide",
        spec: { render: { rgb: true },
                channels: [{ name: "rgb", src: "/he/rgb/" }] },
    });
    check("an rgb layer gets no channel panel",
        mounts.length === rgbBefore,
        "its bytes are already the picture: no channel to pick, no window");
    stack.unregister("rgb_layer");

    // Unregistering re-renders through the panel's own subscription, which is
    // where a card whose layer has gone is dropped.
    stack.unregister("mx");
    check("a layer that leaves the stack takes its panel's listeners with it",
        destroyed.includes("mx"),
        "an unmounted panel goes on remapping slots on every HD toggle");
    delete ctx.PlexoraLayerChannels;
}

// A registered multiplex arrives called Channel_0 ... Channel_n as often as one
// opened as the reference image does, so its card carries the SAME "Upload
// channel names" button. What this file owns is the two ends of it: the panel
// is handed a callback, and what the server applies is written back onto the
// layer the viewer is reading.

{
    const mounts = [];
    const destroyed = [];
    const opened = [];
    ctx.PlexoraLayerChannels = {
        mount(options) {
            const node = makeNode("div");
            const footer = makeNode("div");
            node.appendChild(footer);
            const mounted = {
                node, footer, options,
                setAlignment(note) { footer.children.splice(0); footer.appendChild(note); },
                destroy() { destroyed.push(options.layer.id); },
            };
            mounts.push(mounted);
            return mounted;
        },
    };
    ctx.PlexoraChannelNames = { open: (options) => opened.push(options) };
    //: Only for this block. `persist` is guarded on it, and every card built
    //: while it is set would try to fetch -- which this sandbox has no fetch
    //: for. Nothing here touches a control, so nothing here persists.
    ctx.flaskVariables = { datasource: "demo" };

    stack.register("mx", {
        kind: "image", label: "Multiplex",
        spec: { channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                           { name: "mx_1", src: "/mx/mx_1/" }] },
    });
    check("a layer's channel panel is handed a rename callback",
        typeof mounts[0].options.rename === "function",
        "without it the panel draws no button, which is right for a build "
        + "with no modal and wrong for the viewer");

    mounts[0].options.rename();
    check("...which opens the reference image's own dialog, told which layer",
        opened.length === 1 && opened[0].datasource === "demo"
        && opened[0].layer === "mx" && opened[0].label === "Multiplex",
        "one flow and one file reader for both images, or a spreadsheet can "
        + "be read two ways");

    const spec = stack.get("mx").spec;
    opened[0].onApplied(["CD45", "DAPI"]);
    check("applying the names rewrites the layer's own channel list",
        spec.channels.map((c) => c.name).join(",") === "CD45,DAPI"
        && spec.channels.map((c) => c.fullname).join(",") === "CD45,DAPI",
        "`spec` IS the entry in config.layers, which is what the viewer reads");
    check("...and leaves every tile address exactly where it was",
        spec.channels.map((c) => c.src).join(",") === "/mx/mx_0/,/mx/mx_1/",
        "the plane number is IN the address: a rename that moved it would 404");
    check("...and builds the panel again, from the new names",
        destroyed.includes("mx") && mounts.length === 2
        && mounts[1].options.layer.spec.channels[0].name === "CD45",
        "the panel holds name->index and name->src maps built at mount");

    stack.unregister("mx");
    delete ctx.PlexoraLayerChannels;
    delete ctx.PlexoraChannelNames;
    delete ctx.flaskVariables;
}

// The action line is the panel's, and the opacity control on it is the
// reference card's. A multiplex imported as a layer had a label, a full track
// and a number box where one imported as the reference image had
// `Opacity 100%` beside "Upload channel names" -- two widgets for one kind of
// picture, told apart by import order. The panel now builds the reference
// card's line and marks it as index.html marks the reference's; what THIS
// file owns is putting the same compact control first on it, and the
// lifecycle: held across a rebuild, and taken off the portal with the panel.

{
    const mounts = [];
    const detached = [];
    const portal = ctx.PopoverPortal;
    ctx.PopoverPortal = { attach: (el) => el, detach: (el) => detached.push(el), root: () => null };
    ctx.PlexoraLayerChannels = {
        mount(options) {
            const node = makeNode("div");
            const actions = makeNode("div");
            actions.className = "layer-card-actions";
            actions.setAttribute("data-layer-opacity-slot", "");
            const upload = makeNode("button");
            upload.className = "layer-card-action";
            actions.appendChild(upload);
            node.appendChild(actions);
            const footer = makeNode("div");
            node.appendChild(footer);
            const mounted = {
                node, footer, actions, options,
                setAlignment(note) { footer.children.splice(0); footer.appendChild(note); },
                destroy() {},
            };
            mounts.push(mounted);
            return mounted;
        },
    };
    const spec = { channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                              { name: "mx_1", src: "/mx/mx_1/" }] };
    stack.register("mx3", { kind: "image", label: "Multiplex", spec });
    const line = () => mounts[0].actions;
    const triggers = () => line().children.filter(
        (n) => n.className?.includes?.("layer-opacity-trigger"));

    check("a channelled layer's opacity is the reference card's compact control",
        triggers().length === 1,
        "a multiplex imported as a layer looked like a different widget from "
        + "one imported as the reference image");
    check("...FIRST on the panel's action line, before Upload channel names",
        line().children[0]?.className?.includes?.("layer-opacity-trigger") === true
        && line().children[1]?.className?.includes?.("layer-card-action") === true,
        "the same line in the same order as the reference card's");
    check("...so the card grows no opacity row of its own",
        !findAll(cardFor("mx3"), (n) => n.className?.includes?.("layer-card-row")).length,
        "two controls for one number is what this panel is undoing");
    check("...and its track is not in the card",
        !findAll(cardFor("mx3"), (n) => n.tagName === "INPUT" && n.type === "range").length,
        "it is in a popover on the portal, as the reference image's is");

    const first = triggers()[0];
    // What `refreshBody` does on every change to the layer.
    stack.register("mx3", { kind: "image", label: "Multiplex", spec,
                            transform: [1, 0, 0, 1, 12, 4] });
    check("a rebuilt card keeps the SAME opacity control, not a second one",
        mounts.length === 1 && triggers().length === 1 && triggers()[0] === first
        && detached.length === 0,
        "one rebuilt with the card would leave its popover on the portal every time");

    stack.unregister("mx3");
    check("a layer that leaves the stack takes its opacity popover off the portal",
        detached.some((n) => n.className?.includes?.("layer-opacity-popover")),
        "the orphan ColorSwatchPicker warns about");

    ctx.PopoverPortal = portal;
    delete ctx.PlexoraLayerChannels;
}

{
    // Without the module -- an older page, or this probe before the block
    // above -- a channelled layer falls back to the single-select row rather
    // than to nothing.
    stack.register("mx2", {
        kind: "image", label: "Multiplex",
        spec: { channels: [{ name: "a", src: "/a/" }, { name: "b", src: "/b/" }] },
    });
    check("with no panel module the card still offers a channel row",
        findAll(cardFor("mx2"), (n) => n.tagName === "SELECT").length === 1,
        "a build without the module must degrade, not go blank");
    stack.unregister("mx2");
}


// -- the Image card's overflow menu ----------------------------------------

{
    const menuOn = (id) => findAll(cardFor(id),
        (n) => n.tagName === "BUTTON" && n.className.includes("layer-card-menu"))[0] || null;
    const menu = menuOn(REFERENCE_LAYER_ID);
    check("the fluorescence Image card has a menu button in its header",
        Boolean(menu) && findAll(cardFor(REFERENCE_LAYER_ID),
            (n) => n.className?.includes?.("layer-card-extras")
                && findAll(n, (m) => m === menu).length === 1).length === 1,
        "beside the eye, with the other header controls");
    check("...which survives the card being rebuilt",
        (() => { ctx.PlexoraLayerManager.render(); return menuOn(REFERENCE_LAYER_ID) === menu; })(),
        "the staged markup that earned it has been adopted by then");
    let threw = null;
    try { menu.click(); } catch (error) { threw = error; }
    check("...and clicking it without a menu primitive loaded neither folds nor throws",
        threw === null
        && !cardFor(REFERENCE_LAYER_ID).classList?.contains?.("is-collapsed"),
        String(threw || ""));
    check("a registered layer gets no menu",
        !menuOn("he"), "copy and paste are the reference image's");
}


// -- removing --------------------------------------------------------------

{
    const card = cardFor("he");
    const x = findAll(card, (n) => n.className?.includes?.("layer-card-remove"))[0];
    x.click();
    check("removing a layer takes its card with it",
        !stack.has("he") && !cards().includes("he"), cards().join(","));
}


console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
