/**
 * Loaded tools as cards: collapse, on/off, remove, and drag to restack.
 *
 * The three states a loaded tool can be in are the thing being tested, because
 * collapsing any two of them is exactly what made a second plugin unusable:
 *
 *   LOADED  - record, panel DOM, controller and cached data exist; nothing drawn.
 *   VISIBLE - it contributes a layer to the image. Several at once, and the card
 *             order is the order they stack in.
 *   ACTIVE  - the shared Cells control, picking and the gate flows act on it, and
 *             its panel is expanded. Exactly one, or none.
 *
 * Opening a tool makes it all three and stands the previous one down to LOADED.
 * That default is what keeps the viewer showing one thing; turning another
 * card's eye back on is what stacks them, and is the whole feature.
 *
 * Also pinned here, because each has a silent failure mode:
 *
 *   - Collapsing must not destroy the panel DOM. A controller takes its element
 *     handles once at setup(); rebuilding the markup under it leaves every one
 *     of them pointing at a node that is no longer on the page, and nothing
 *     reports that.
 *   - The top card is the TOP layer. Core stacks bottom-first, so the DOM order
 *     is reversed on the way out. Getting that backwards draws the picture
 *     upside down and looks like a rendering bug.
 *   - Remove has to take the tool's mounts out of EVERY slot, including the
 *     off-screen legacy one. A stale mount there is found by the re-open path,
 *     returned as if it were new, and the freshly fetched panel is written into
 *     markup the new controller never saw.
 *
 * Run directly:  node tests/js/tool_cards_probe.mjs
 *   --source <path>   probe a different toolLoader.js
 * Exit 0 = every rule holds. Exit 1 = not, with the reasons on stderr.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(REPO, "plexora/client/src/js/views/toolLoader.js")
    : process.argv[sourceArg + 1];

/** Two plugins. Gating also declares the off-screen legacy slot, which is where
 *  a leftover mount breaks re-opening. */
const PAYLOADS = {
    gating: {
        fragments: {
            tool_panel_slot: "<section id='gate_marker_section'></section>",
            tool_panel_legacy_slot: "<section id='gate_download'></section>",
        },
        scripts: [], styles: [],
    },
    cell_explorer: {
        fragments: { tool_panel_slot: "<section id='cex_panel'></section>" },
        scripts: [], styles: [],
    },
};

/** What each plugin declares to core. ROI-style plugins have no cell layer, so
 *  their card's eye has to reach the controller instead. */
const OWNS_LAYER = { gating: true, cell_explorer: true };

// -- a DOM with enough of a selector engine to run cards ------------------

function makeNode(tag) {
    const classes = new Set();
    const node = {
        tagName: tag,
        dataset: {},
        attributes: {},
        children: [],
        parentNode: null,
        style: {},
        title: "",
        _text: "",
        html: null,
        handlers: {},
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        get className() { return Array.from(classes).join(" "); },
        set className(v) {
            classes.clear();
            String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        get textContent() { return node._text; },
        set textContent(v) { node._text = String(v); },
        set innerHTML(v) {
            node.html = v;
            // Enough of a parse for a run of `<span class="...">`, which is all
            // the card builder writes -- and those spans are the glyphs an icon
            // button is made of. The eye writes two, one per state.
            node.children = [];
            for (const [, cls] of String(v).matchAll(/<span class="([^"]*)"><\/span>/g)) {
                const span = makeNode("span");
                span.className = cls;
                node.appendChild(span);
            }
        },
        get innerHTML() { return node.html; },
        setAttribute(name, value) { node.attributes[name] = String(value); },
        getAttribute(name) { return node.attributes[name] ?? null; },
        appendChild(child) {
            node.children = node.children.filter((c) => c !== child);
            node.children.push(child);
            child.parentNode = node;
            return child;
        },
        insertBefore(child, before) {
            node.children = node.children.filter((c) => c !== child);
            const at = node.children.indexOf(before);
            node.children.splice(at < 0 ? node.children.length : at, 0, child);
            child.parentNode = node;
            return child;
        },
        removeChild(child) {
            node.children = node.children.filter((c) => c !== child);
            child.parentNode = null;
            return child;
        },
        remove() { node.parentNode?.removeChild(node); },
        get firstChild() { return node.children[0] || null; },
        querySelector(selector) { return query(node, selector)[0] || null; },
        querySelectorAll(selector) { return query(node, selector); },
        addEventListener(type, fn) { node.handlers[type] = fn; },
        click() { node.handlers.click?.({ preventDefault() {} }); },
    };
    return node;
}

/** One compound selector: an optional tag followed by any number of `.class`
 *  and `[attr="value"]` (or bare `[attr]`) fragments, all of which must hold. */
function matches(node, part) {
    const tag = /^[a-z]+/.exec(part);
    if (tag && node.tagName !== tag[0]) return false;
    for (const [, name] of part.matchAll(/\.([\w-]+)/g)) {
        if (!node.classList.contains(name)) return false;
    }
    for (const [, name, , value] of part.matchAll(/\[([a-z-]+)(="([^"]*)")?\]/g)) {
        if (value === undefined) {
            if (node.attributes[name] === undefined) return false;
        } else if (node.attributes[name] !== value) {
            return false;
        }
    }
    return true;
}

function query(root, selector) {
    const parts = selector.trim().split(/\s+/);
    let current = [root];
    for (const part of parts) {
        const next = [];
        const walk = (parent) => {
            for (const child of parent.children) {
                if (matches(child, part)) next.push(child);
                walk(child);
            }
        };
        current.forEach(walk);
        current = next;
    }
    return current;
}

const lifecycle = [];
const coreCalls = [];

function browserGlobals() {
    const listeners = new Map();
    const slots = {
        tool_panel_slot: makeNode("div"),
        tool_panel_legacy_slot: makeNode("div"),
    };
    slots.tool_panel_slot.classList.add("tool-panel-hidden");

    const toolLinks = {};
    for (const name of Object.keys(PAYLOADS)) {
        const link = makeNode("a");
        link.dataset.tool = name;
        link.setAttribute("data-tool", name);
        link.textContent = name === "gating" ? "Thresholding" : "Cell Explorer";
        toolLinks[name] = link;
    }

    const document = {
        querySelector(selector) {
            for (const link of Object.values(toolLinks)) {
                if (matches(link, selector.trim())) return link;
            }
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "a[data-tool]") return Object.values(toolLinks);
            return [];
        },
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => slots[id] || null,
        head: { appendChild(node) { setTimeout(() => node.onload?.(), 0); return node; } },
        addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
    };

    const controller = (name) => ({
        onShow() { lifecycle.push(`${name}:show`); },
        onHide() { lifecycle.push(`${name}:hide`); },
        onVisibilityChange(on) { lifecycle.push(`${name}:layer:${on}`); },
    });

    let requested = null;
    const sortables = [];

    return {
        __listeners: listeners,
        __slots: slots,
        __sortables: sortables,
        // Opened the way a user does: through the navbar link's own click
        // handler, which the loader attaches on DOMContentLoaded.
        __openTool: (name) => {
            requested = name;
            return toolLinks[name].handlers.click({ preventDefault() {} });
        },
        console,
        Promise, Object, Array, Map, Set, JSON, String, Number, Boolean, Error,
        setTimeout, clearTimeout,
        document,
        fetch: async () => ({ json: async () => PAYLOADS[requested], ok: true }),
        window: {
            flaskVariables: { datasource: "probe_datasource" },
            PLEXORA_BASE_URL: "",
            __plexoraReady: Promise.resolve(),
            Sortable: class Sortable {
                constructor(element, options) {
                    this.element = element;
                    this.options = options;
                    sortables.push(this);
                }
            },
            Plexora: { plugins: { get: (name) => ({ name }) } },
            __plexora: {
                activatePlugin: async (def) => ({ sidebarController: controller(def.name) }),
                setActiveTool: (name) => coreCalls.push(`active:${name}`),
                setToolLayerVisible: (name, on) => {
                    if (!OWNS_LAYER[name]) return false;
                    coreCalls.push(`layer:${name}:${on}`);
                    return true;
                },
                setToolLayerOrder: (names) => coreCalls.push(`order:${names.join(">")}`),
                deactivatePlugin: (name) => coreCalls.push(`deactivate:${name}`),
            },
        },
    };
}

const ctx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8"), ctx);
ctx.__listeners.get("document:DOMContentLoaded")?.();

const slot = ctx.__slots.tool_panel_slot;

/** Wait until the tool has finished opening. The click handler cannot return
 *  openTool's promise, so watch for the effect with a ceiling. */
async function opened(name, timeoutMs = 3000) {
    for (const deadline = Date.now() + timeoutMs; Date.now() < deadline;) {
        await new Promise((resolve) => setTimeout(resolve, 5));
        if (lifecycle[lifecycle.length - 1] === `${name}:show`) return;
    }
}

const problems = [];
function want(condition, message) {
    if (!condition) problems.push(message);
}

const cardNames = () => slot.children.map((card) => card.getAttribute("data-tool-card"));
const card = (name) => slot.querySelector(`[data-tool-card="${name}"]`);
const mount = (slotId, name) =>
    ctx.__slots[slotId].querySelector(`[data-tool-panel="${name}"]`);
const button = (name, cls) => card(name).querySelector(cls);
const collapsed = (name) => card(name).classList.contains("is-collapsed");
const panelHidden = (name) =>
    mount("tool_panel_slot", name).classList.contains("tool-panel-hidden");

/** Whether the card says its layer is off. This class is the whole of it: the
 *  eye button carries BOTH glyphs and CSS picks one off this class, because
 *  FontAwesome is loaded as JS and replaces every icon span with an svg, so
 *  rewriting a glyph's class from JS writes to a node that is no longer on the
 *  page -- which is how a hidden layer ended up sitting under an open eye. */
const layerOff = (name) => card(name).classList.contains("is-layer-off");
const eyeGlyphs = (name) => button(name, ".tool-card-eye").children
    .map((glyph) => glyph.className);

/** Open a tool through the loader's own navbar handler, and wait for it. */
async function open(name) {
    await ctx.__openTool(name);
    await opened(name);
}

// -- opening ---------------------------------------------------------------

await open("gating");

want(cardNames().join() === "gating", `first card: ${cardNames()}`);
want(card("gating").querySelector(".tool-card-grip") !== null, "the card has no drag grip");
want(card("gating").querySelector(".tool-card-title").textContent === "Thresholding",
    "the card is not named after the Tools-menu entry it was opened from");
want(!collapsed("gating"), "the tool just opened came up folded");
want(coreCalls.includes("active:gating"), "the tool just opened did not become the active one");
want(mount("tool_panel_legacy_slot", "gating") !== null,
    "the off-screen slot did not get its own mount");
want(mount("tool_panel_legacy_slot", "gating").parentNode
    === ctx.__slots.tool_panel_legacy_slot,
    "the off-screen slot was given a card, which nobody can see");

await open("cell_explorer");

want(cardNames().join() === "cell_explorer,gating",
    `a new card must go on top: ${cardNames()}`);
want(collapsed("gating") && panelHidden("gating"),
    "opening a second tool did not fold the first one away");
want(coreCalls.includes("layer:gating:false"),
    "opening a second tool left the first one's layer drawing");
want(!collapsed("cell_explorer") && coreCalls.includes("layer:cell_explorer:true"),
    "the tool just opened is not visible");
want(lifecycle.indexOf("gating:hide") < lifecycle.indexOf("cell_explorer:show"),
    "the outgoing tool was told to hide only after the incoming one was shown");

// -- the eye: loaded and visible are different things ----------------------

want(mount("tool_panel_slot", "gating").innerHTML
    === PAYLOADS.gating.fragments.tool_panel_slot,
    "the folded tool's panel markup did not survive");

want(layerOff("gating"),
    "the folded tool's card does not say its layer is off, so the eye is drawn open "
    + "over a layer that is not being drawn");

const glyphs = eyeGlyphs("gating");
want(glyphs.some((c) => c.includes("fa-eye") && !c.includes("slash"))
    && glyphs.some((c) => c.includes("fa-eye-slash")),
    `the eye button must carry both glyphs for CSS to pick between: ${glyphs}`);

button("gating", ".tool-card-eye").click();
want(coreCalls.includes("layer:gating:true"),
    "the eye did not put the layer back on");
want(collapsed("gating"),
    "turning a layer back on also unfolded its panel -- visible is not selected");
want(!coreCalls.includes("active:gating") || coreCalls.lastIndexOf("active:gating")
    < coreCalls.lastIndexOf("active:cell_explorer"),
    "turning a layer back on stole the selection from the open tool");
want(!layerOff("gating"), "the eye does not say the layer is on");

// A pinned layer survives the single-active stand-down. Selecting the other
// card must not keep dismantling a stack the user built on purpose.
const pinnedFrom = coreCalls.length;
button("cell_explorer", ".tool-card-title").click();
want(!coreCalls.slice(pinnedFrom).includes("layer:gating:false"),
    "selecting another card switched off a layer the user had turned on by hand");
want(collapsed("gating"),
    "a pinned layer's panel should still fold away -- pinned is about the "
    + "picture, not about the panel");

button("gating", ".tool-card-eye").click();
want(layerOff("gating"), "the eye does not say the layer is off");
want(eyeGlyphs("gating").join() === glyphs.join(),
    "turning the layer off rewrote a glyph's class instead of leaving both in "
    + "place for CSS -- after FontAwesome's svg replacement there is no span "
    + `left to rewrite: ${eyeGlyphs("gating")}`);

const unpinnedFrom = coreCalls.length;
button("gating", ".tool-card-title").click();
button("cell_explorer", ".tool-card-title").click();
want(coreCalls.slice(unpinnedFrom).includes("layer:gating:false"),
    "switching the eye back off should give the layer up to the default again");

// -- collapse keeps the DOM ------------------------------------------------

const panelNode = mount("tool_panel_slot", "cell_explorer");
button("cell_explorer", ".tool-card-collapse").click();
want(collapsed("cell_explorer") && panelHidden("cell_explorer"),
    "the chevron did not fold the panel");
button("cell_explorer", ".tool-card-collapse").click();
want(!collapsed("cell_explorer") && !panelHidden("cell_explorer"),
    "the chevron did not unfold the panel");
want(mount("tool_panel_slot", "cell_explorer") === panelNode,
    "folding and unfolding rebuilt the panel -- every element handle the "
    + "controller took at setup() now points at a node that is off the page");

// -- selecting by title ----------------------------------------------------

button("gating", ".tool-card-title").click();
want(collapsed("cell_explorer") && !collapsed("gating"),
    "clicking a card's name did not select it");
want(coreCalls.lastIndexOf("active:gating") > coreCalls.lastIndexOf("active:cell_explorer"),
    "clicking a card's name did not move the shared controls onto it");

// -- drag to restack -------------------------------------------------------

want(ctx.__sortables.length === 1, "the card list is not draggable");
want(ctx.__sortables[0].options.handle === ".tool-card-grip",
    "the whole card is draggable, so a click on a button would start a drag");

const orderBefore = coreCalls.filter((c) => c.startsWith("order:")).pop();
want(orderBefore === "order:gating>cell_explorer",
    `the top card must be the TOP layer, and core stacks bottom-first: ${orderBefore}`);

// Sortable rearranges the DOM itself and then calls onSort.
slot.children.reverse();
ctx.__sortables[0].options.onSort();
const orderAfter = coreCalls.filter((c) => c.startsWith("order:")).pop();
want(orderAfter === "order:cell_explorer>gating",
    `dragging did not restack the layers: ${orderAfter}`);

// -- remove ----------------------------------------------------------------

button("gating", ".tool-card-remove").click();
want(coreCalls.includes("deactivate:gating"), "removing a tool did not tear the plugin down");
want(card("gating") === null, "the removed tool's card is still in the sidebar");
want(mount("tool_panel_slot", "gating") === null, "the removed tool's panel is still mounted");
want(mount("tool_panel_legacy_slot", "gating") === null,
    "the removed tool's off-screen mount survived -- re-opening will find it and "
    + "write the new panel into markup the new controller never saw");

const before = lifecycle.length;
await open("gating");
want(card("gating") !== null && cardNames().filter((n) => n === "gating").length === 1,
    `re-opening a removed tool did not produce exactly one card: ${cardNames()}`);
want(mount("tool_panel_slot", "gating").innerHTML
    === PAYLOADS.gating.fragments.tool_panel_slot,
    "the re-opened tool's panel is not its own markup");
want(lifecycle.slice(before).includes("gating:show"),
    "the re-opened tool never got onShow()");

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    cards: cardNames(),
    lifecycle,
    coreCalls,
    problems,
};
console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
