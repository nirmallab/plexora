/**
 * Two tools on screen at once, and only the two that asked to be.
 *
 * Single-active is the rule everywhere else in the sidebar: opening a tool
 * folds the previous one and turns its layer off. Cell Explorer's Open ROIs
 * button is the one exception -- an ROI composition card summarises the cells
 * under an overlay, so folding the overlay away answers a question about a
 * picture the user can no longer see.
 *
 * An exception like that is only safe if it is narrow, and every way it could
 * stop being narrow is invisible on screen:
 *
 *   - if standDown() stops short-circuiting, the pair silently collapses back
 *     to one tool and the card describes an overlay that is no longer drawn;
 *   - if the pair is NOT dissolved when a third tool opens, the user is left
 *     with a stacked layer nobody asked to keep, and no card explaining it;
 *   - if closing one half does not promote the other, `activeToolName` is null
 *     with a panel still expanded -- the shared Cells control then points at
 *     nothing and every click on it is a no-op with no error.
 *
 * None of those throw. The Python suite renders panels one tool at a time and
 * never runs two; `node --check` sees syntax. This drives the real toolLoader.
 *
 * Run directly:  node tests/js/tool_coexist_probe.mjs
 *   --source <path>   probe a different toolLoader.js (used to prove the probe
 *                     can fail, by mutating a copy)
 * Exit 0 = the exception behaved. Exit 1 = it did not.
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

/** Three tools: the pair, and the outsider that has to break it up. */
const PAYLOADS = {
    cell_explorer: {
        fragments: { tool_panel_slot: "<section id='cell_explorer_panel_section'></section>" },
        scripts: [], styles: [],
    },
    roi: {
        fragments: { tool_panel_slot: "<section id='roi_panel_section'></section>" },
        scripts: [], styles: [],
    },
    gating: {
        fragments: { tool_panel_slot: "<section id='gate_marker_section'></section>" },
        scripts: [], styles: [],
    },
};

/** A DOM stand-in with real identity and real parent/child links -- what is
 *  under test is which panels are showing and which layers are drawn. */
function browserGlobals(lifecycle, layers) {
    const listeners = new Map();
    const byId = new Map();

    function makeNode(tag) {
        const classes = new Set();
        const node = {
            tagName: tag,
            dataset: {},
            attributes: {},
            children: [],
            html: null,
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
            set innerHTML(v) { node.html = v; },
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
                return child;
            },
            remove() { node.parentNode?.removeChild(node); },
            get firstChild() { return node.children[0] || null; },
            // Attribute selectors only, and recursive: a panel mount lives
            // inside its card rather than directly in the slot.
            querySelector(selector) {
                const match = /\[([a-z-]+)="([^"]+)"\]/.exec(selector);
                if (!match) return null;
                const [, attribute, value] = match;
                const find = (parent) => {
                    for (const child of parent.children) {
                        if (child.attributes?.[attribute] === value) return child;
                        const nested = find(child);
                        if (nested) return nested;
                    }
                    return null;
                };
                return find(node);
            },
            addEventListener(type, fn) { listeners.set(`${tag}:${type}`, fn); },
        };
        return node;
    }

    const slot = makeNode("div");
    slot.classList.add("tool-panel-hidden");
    byId.set("tool_panel_slot", slot);

    const toolLink = makeNode("a");
    toolLink.dataset.tool = "cell_explorer";

    const document = {
        querySelector: () => null,
        querySelectorAll: () => [toolLink],
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => byId.get(id) || null,
        head: { appendChild(node) { setTimeout(() => node.onload?.(), 0); return node; } },
        addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
    };

    const controller = (name) => ({
        onShow() { lifecycle.push(`${name}:show`); },
        onHide() { lifecycle.push(`${name}:hide`); },
    });

    return {
        __listeners: listeners,
        __slot: slot,
        //: Opened the way a user does, through the navbar link.
        __openTool: (name) => {
            toolLink.dataset.tool = name;
            return listeners.get("a:click")({ preventDefault() {} });
        },
        console,
        Promise, Object, Array, Map, Set, JSON, String, Error, Boolean,
        setTimeout, clearTimeout,
        document,
        // Keyed off the URL rather than a captured name, so two opens can be in
        // flight without one answering for the other.
        fetch: async (url) => {
            const name = /\/tools\/([^/]+)\/panel/.exec(String(url))[1];
            return { ok: true, json: async () => PAYLOADS[name] };
        },
        window: {
            flaskVariables: { datasource: "probe_datasource" },
            PLEXORA_BASE_URL: "",
            __plexoraReady: Promise.resolve(),
            Plexora: { plugins: { get: (name) => ({ name }) } },
            __plexora: {
                activatePlugin: async (def) => ({ sidebarController: controller(def.name) }),
                setToolLayerVisible: (name, on) => layers.push(`${name}:${on ? "on" : "off"}`),
                setActiveTool: (name) => lifecycle.push(`active:${name}`),
                setToolLayerOrder: () => {},
                deactivatePlugin: () => {},
            },
        },
    };
}

/** A fresh loader, because a pair is module state and the interesting cases
 *  are what happens to a pair that has only just been formed. */
function newLoader() {
    const lifecycle = [];
    const layers = [];
    const ctx = createContext(browserGlobals(lifecycle, layers));
    runInContext(readFileSync(SOURCE, "utf8"), ctx);
    ctx.__listeners.get("document:DOMContentLoaded")?.();
    return { ctx, lifecycle, layers, loader: ctx.window.PlexoraToolLoader };
}

/** The click handler cannot return openTool's promise, so wait for the effect
 *  rather than for a fixed number of milliseconds. */
async function opened(lifecycle, name, timeoutMs = 3000) {
    for (const deadline = Date.now() + timeoutMs; Date.now() < deadline;) {
        await new Promise((resolve) => setTimeout(resolve, 5));
        if (lifecycle.includes(`${name}:show`)) return;
    }
}

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

/** Whether a tool's panel is showing, wherever in the card its mount ended up. */
function showing(ctx, name) {
    const mount = ctx.__slot.querySelector(`[data-tool-panel="${name}"]`);
    if (!mount) return null;
    return !mount.classList.contains("tool-panel-hidden");
}

/** Whether a tool's card reads as selected. Both halves of a pair do. */
function selected(ctx, name) {
    const card = ctx.__slot.querySelector(`[data-tool-card="${name}"]`);
    return card ? card.classList.contains("is-active") : null;
}

// -- the pair forms ------------------------------------------------------

{
    const { ctx, lifecycle, layers, loader } = newLoader();
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    lifecycle.length = 0;
    layers.length = 0;

    await loader.openToolAlongside("roi", "cell_explorer");

    check("both panels are showing", [showing(ctx, "cell_explorer"), showing(ctx, "roi")],
        [true, true]);
    check("the tool that opened the other was never stood down",
        lifecycle.filter((e) => e === "cell_explorer:hide"), []);
    check("...and its layer was never turned off",
        layers.filter((e) => e === "cell_explorer:off"), []);
    check("the second tool was shown", lifecycle.includes("roi:show"), true);
    check("the shared controls follow the tool just opened", loader.activeTool(), "roi");
    check("both cards read as selected", [selected(ctx, "cell_explorer"), selected(ctx, "roi")],
        [true, true]);
    check("the pair knows itself", loader.isCoexisting("cell_explorer"), true);
    check("...from either side", loader.coexistPartner("roi"), "cell_explorer");

    // Clicking between the two cards is not a switch away from anything.
    lifecycle.length = 0;
    layers.length = 0;
    loader.setToolCollapsed("cell_explorer", false);
    const card = ctx.__slot.querySelector('[data-tool-card="cell_explorer"]');
    check("selecting the other half tells nobody to hide",
        lifecycle.filter((e) => e.endsWith(":hide")), []);
    check("...and the cards are both still there", Boolean(card), true);
}

// -- a third tool ends it ------------------------------------------------

{
    const { ctx, lifecycle, layers, loader } = newLoader();
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    await loader.openToolAlongside("roi", "cell_explorer");
    lifecycle.length = 0;
    layers.length = 0;

    await ctx.__openTool("gating");
    await opened(lifecycle, "gating");

    check("opening a third tool folds BOTH halves",
        [showing(ctx, "cell_explorer"), showing(ctx, "roi"), showing(ctx, "gating")],
        [false, false, true]);
    check("...and tells both of them", [
        lifecycle.includes("cell_explorer:hide"),
        lifecycle.includes("roi:hide"),
    ], [true, true]);
    check("...and takes both layers off", [
        layers.includes("cell_explorer:off"),
        layers.includes("roi:off"),
    ], [true, true]);
    check("the exception does not outlive the pairing",
        loader.isCoexisting("cell_explorer"), false);
    check("...on either side", loader.coexistPartner("roi"), null);
}

// -- closing one half promotes the other ---------------------------------

{
    const { ctx, lifecycle, loader } = newLoader();
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    await loader.openToolAlongside("roi", "cell_explorer");

    loader.hideToolPanel("roi");
    check("closing one half leaves the other selected", loader.activeTool(), "cell_explorer");
    check("...and still showing", showing(ctx, "cell_explorer"), true);
    check("...with the closed one folded", showing(ctx, "roi"), false);
    check("...and the pair dissolved", loader.isCoexisting("cell_explorer"), false);
}

{
    const { ctx, lifecycle, loader } = newLoader();
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    await loader.openToolAlongside("roi", "cell_explorer");

    // The card's X, which unloads rather than folding.
    loader.removeTool("cell_explorer");
    check("removing one half leaves the other selected", loader.activeTool(), "roi");
    check("...and showing", showing(ctx, "roi"), true);
    check("...and the removed one gone", showing(ctx, "cell_explorer"), null);
}

// -- the ordinary path is untouched --------------------------------------

{
    const { ctx, lifecycle, layers, loader } = newLoader();
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    lifecycle.length = 0;
    layers.length = 0;

    // Opening ROI from the Tools menu, which nobody asked to be a pair.
    await ctx.__openTool("roi");
    await opened(lifecycle, "roi");

    check("an ordinary open still folds the previous tool",
        [showing(ctx, "cell_explorer"), showing(ctx, "roi")], [false, true]);
    check("...and still tells it", lifecycle.includes("cell_explorer:hide"), true);
    check("...and still takes its layer off", layers.includes("cell_explorer:off"), true);
    check("...and forms no pair", loader.isCoexisting("roi"), false);
}

// -- an unrelated tool being closed ---------------------------------------

{
    const { ctx, lifecycle, loader } = newLoader();
    // A third tool, loaded and then folded away by the pair opening over it.
    await ctx.__openTool("gating");
    await opened(lifecycle, "gating");
    await ctx.__openTool("cell_explorer");
    await opened(lifecycle, "cell_explorer");
    await loader.openToolAlongside("roi", "cell_explorer");

    // Closing it says nothing about the two tools sharing the screen.
    loader.hideToolPanel("gating");
    check("closing an unrelated tool leaves the pair alone",
        loader.isCoexisting("cell_explorer"), true);
    check("...with both still showing",
        [showing(ctx, "cell_explorer"), showing(ctx, "roi")], [true, true]);
    check("...and the selection where it was", loader.activeTool(), "roi");

    loader.removeTool("gating");
    check("removing an unrelated tool leaves the pair alone",
        loader.coexistPartner("roi"), "cell_explorer");
}

// -- an anchor that is not loaded ----------------------------------------

{
    const { ctx, lifecycle, loader } = newLoader();
    // Nothing open at all: openToolAlongside has nothing to ride along with and
    // must degrade to an ordinary open rather than forming a half-pair.
    await loader.openToolAlongside("roi", "cell_explorer");
    await opened(lifecycle, "roi");
    check("with no anchor loaded it is just an open", showing(ctx, "roi"), true);
    check("...and no pair is claimed", loader.isCoexisting("roi"), false);
}

const report = {
    source: SOURCE.replace(`${REPO}/`, ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
