/**
 * With two tools installed, does opening one leave the other intact?
 *
 * The bug this exists to catch, in full:
 *
 * `#tool_panel_slot` is a single shared mount, and toolLoader.js filled it with
 * `slot.innerHTML = payload.fragments[slotId]` -- a whole-slot replace. That was
 * correct for exactly as long as gating was the only plugin. The moment a second
 * one exists, opening B deletes A's panel out of the DOM, while A's sidebar
 * controller keeps the element references it took in setup(); and the re-open
 * path only unhid the slot, so going back to A showed B's markup with A's live
 * controller wired to nodes no longer on the page.
 *
 * Nothing could see it. The Python suite renders panels server-side one tool at
 * a time and never runs two; tool_assets_probe.mjs opens one tool and checks its
 * assets arrived; `node --check` sees syntax. The failure needs two tools and a
 * switch between them, which is what this does.
 *
 * The second assertion is the one that matters for a tool that draws on the
 * image: switching away must CALL `onHide()`. A hidden panel whose viewer
 * handlers and document-level keyboard shortcuts are still attached goes on
 * eating input for a panel the user cannot see.
 *
 * Run directly:  node tests/js/tool_switch_probe.mjs
 *   --source <path>   probe a different toolLoader.js (used to prove the probe
 *                     can fail, by mutating a copy)
 * Exit 0 = both panels survive the switch and onHide/onShow fire. Exit 1 = not.
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

/** Two plugins, each declaring the same slot -- which is the whole point. */
const PAYLOADS = {
    gating: {
        fragments: { tool_panel_slot: "<section id='gate_marker_section'></section>" },
        scripts: ["/plugins/gating/static/csvGatingList.js?v=probe"],
        styles: ["/plugins/gating/static/gating.css?v=probe"],
    },
    roi: {
        fragments: { tool_panel_slot: "<section id='roi_panel_section'></section>" },
        scripts: ["/plugins/roi/static/roiSidebarController.js?v=probe"],
        styles: ["/plugins/roi/static/roi.css?v=probe"],
    },
};

/** Which lifecycle hooks each tool's controller was told about, in order. */
const lifecycle = [];

/** A DOM stand-in with real identity and real parent/child links, because what
 *  is under test is whether one tool's nodes survive another tool's arrival. */
function browserGlobals() {
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
            set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
            set innerHTML(v) { node.html = v; },
            get innerHTML() { return node.html; },
            setAttribute(name, value) { node.attributes[name] = String(value); },
            getAttribute(name) { return node.attributes[name] ?? null; },
            // Re-parenting DETACHES, as the real thing does. Without it,
            // adopt()'s `while (slot.firstChild) mount.appendChild(...)` never
            // empties the slot and the probe hangs -- which is exactly what a
            // stub that is subtly more forgiving than the DOM buys you.
            appendChild(child) {
                child.parentNode?.removeChild?.(child);
                node.children = node.children.filter((c) => c !== child);
                node.children.push(child);
                child.parentNode = node;
                return child;
            },
            insertBefore(child, before) {
                child.parentNode?.removeChild?.(child);
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
            // Attribute selectors only, and recursive -- a tool's panel mount
            // now lives inside its card rather than directly in the slot, so a
            // direct-children search would find nothing.
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
    slot.classList.add("tool-panel-hidden"); // as the server renders it with no tool
    byId.set("tool_panel_slot", slot);

    const toolLink = makeNode("a");
    toolLink.dataset.tool = "gating";

    const document = {
        querySelector: () => null,
        querySelectorAll: () => [toolLink],
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => byId.get(id) || null,
        head: {
            appendChild(node) {
                setTimeout(() => node.onload?.(), 0);
                return node;
            },
        },
        addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
    };

    const controller = (name) => ({
        onShow() { lifecycle.push(`${name}:show`); },
        onHide() { lifecycle.push(`${name}:hide`); },
    });

    let requested = null;

    return {
        __listeners: listeners,
        __slot: slot,
        // Opened the way a user does: through the navbar link's click handler,
        // so the loader reads the tool name off the link as it does in a browser.
        __openTool: (name) => {
            requested = name;
            toolLink.dataset.tool = name;
            return listeners.get("a:click")({ preventDefault() {} });
        },
        __controller: controller,
        console,
        Promise, Object, Array, Map, Set, JSON, String, Error,
        setTimeout, clearTimeout,
        document,
        fetch: async () => ({ json: async () => PAYLOADS[requested], ok: true }),
        window: {
            flaskVariables: { datasource: "probe_datasource" },
            PLEXORA_BASE_URL: "",
            __plexoraReady: Promise.resolve(),
            Plexora: { plugins: { get: (name) => ({ name }) } },
            __plexora: {
                activatePlugin: async (def) => ({ sidebarController: controller(def.name) }),
            },
        },
    };
}

const ctx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8"), ctx);
ctx.__listeners.get("document:DOMContentLoaded")?.();

/** Wait until the tool has actually finished opening.
 *
 * The click handler does not return openTool's promise (a DOM listener has
 * nowhere to return it to), so there is nothing to await. Waiting a fixed
 * number of milliseconds instead races the loader: it awaits one timer hop per
 * asset, and on Windows a setTimeout(0) can take a full ~15 ms tick. Watch for
 * the effect instead, with a ceiling so a tool that never opens reports as a
 * problem rather than hanging here. */
async function opened(name, timeoutMs = 3000) {
    for (const deadline = Date.now() + timeoutMs; Date.now() < deadline; ) {
        await new Promise((resolve) => setTimeout(resolve, 10));
        if (lifecycle[lifecycle.length - 1] === `${name}:show`) return;
    }
}

await ctx.__openTool("gating");
await opened("gating");
await ctx.__openTool("roi");
await opened("roi");

const slot = ctx.__slot;

/** Each tool's panel mount, wherever in the slot's subtree it ended up -- the
 *  card wrapper put it a couple of levels down from where it used to be. */
const state = (name) => {
    const mount = slot.querySelector(`[data-tool-panel="${name}"]`);
    if (!mount) return { present: false, hidden: null, html: null };
    return {
        present: true,
        hidden: mount.classList.contains("tool-panel-hidden"),
        html: mount.innerHTML,
    };
};

const after_switch = { gating: state("gating"), roi: state("roi") };

// Back to the first tool: its own markup must still be the thing that shows.
await ctx.__openTool("gating");
await opened("gating");
const after_return = { gating: state("gating"), roi: state("roi") };

const problems = [];
if (!after_switch.gating.present) {
    problems.push("opening the second tool destroyed the first tool's panel DOM");
} else if (!after_switch.gating.hidden) {
    problems.push("both tools' panels are showing at once");
}
if (!after_switch.roi.present || after_switch.roi.hidden) {
    problems.push("the tool that was just opened is not showing");
}
if (after_switch.gating.html !== PAYLOADS.gating.fragments.tool_panel_slot) {
    problems.push("the first tool's markup was overwritten by the second's");
}
if (!lifecycle.includes("gating:hide")) {
    problems.push(
        "the tool being switched away from was never told (no onHide) -- its "
        + "viewer handlers and keyboard shortcuts stay live behind the new panel"
    );
}
if (lifecycle.indexOf("gating:hide") > lifecycle.indexOf("roi:show")) {
    problems.push("the outgoing tool was told to hide only after the incoming one was shown");
}
if (after_return.roi.hidden !== true || after_return.gating.hidden !== false) {
    problems.push("returning to the first tool did not swap the panels back");
}

/**
 * The other way a tool comes up, in a context of its own.
 *
 * `?tool=roi` renders the panel server-side; nothing is fetched and the menu
 * path never runs. main.js reports the already-live tool through
 * registerLoaded, which used to set activeToolName by hand -- so the panel
 * appeared, looked completely correct, and never got onShow(). For gating that
 * is invisible (it has no onShow); for a tool that attaches its handlers there
 * it means a pen that draws nothing, shortcuts that do nothing, and no error
 * anywhere to say why.
 */
const bootCtx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8"), bootCtx);
// As the server renders it: ?tool=roi puts roi's own markup INSIDE the slot,
// loose, and the boot path wraps whatever it finds there in a card.
const bootSlot = bootCtx.document.getElementById("tool_panel_slot");
bootSlot.appendChild(bootCtx.document.createElement("section"));
bootCtx.window.PlexoraToolLoader.registerLoaded(
    "roi", ["tool_panel_slot"], bootCtx.__controller("boot-roi"));

const bootLifecycle = lifecycle.filter((entry) => entry.startsWith("boot-roi:"));
if (!bootLifecycle.includes("boot-roi:show")) {
    problems.push(
        "a server-rendered ?tool= panel was registered without onShow() -- the "
        + "tool is on screen with none of the handlers it attaches there"
    );
}
if (bootCtx.window.PlexoraToolLoader.activeTool() !== "roi") {
    problems.push("a server-rendered tool did not become the active one");
}
if (!bootSlot.querySelector('[data-tool-card="roi"]')) {
    problems.push("a server-rendered panel was not wrapped in a card");
}

/**
 * A tool whose panel is somewhere other than the sidebar.
 *
 * main.js works the boot slot list out from `data-tool-mount`, which
 * index.html stamps on EVERY slot with the active tool's name -- so a plugin
 * that declared one panel, or none in the sidebar at all, is still named on all
 * of them. Adopting an empty slot builds a card with nothing in it: a header, a
 * grip and an eye over a panel that does not exist, and an X that is the only
 * way to close a tool whose controls are elsewhere entirely.
 *
 * figure_builder is the case: its controls are on the image and it declares no
 * tool_panel_slot at all.
 */
const emptyCtx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8"), emptyCtx);
const emptySlot = emptyCtx.document.getElementById("tool_panel_slot");
emptyCtx.window.PlexoraToolLoader.registerLoaded(
    "figure_builder", ["tool_panel_slot"], emptyCtx.__controller("boot-fb"));

if (emptySlot.children.length) {
    problems.push("an empty slot grew a card for a tool that has no panel in it");
}
// It is still a loaded, selected tool -- it just has no sidebar presence.
if (emptyCtx.window.PlexoraToolLoader.activeTool() !== "figure_builder") {
    problems.push("a tool with no sidebar panel did not become the active one");
}
if (!lifecycle.includes("boot-fb:show")) {
    problems.push("a tool with no sidebar panel was registered without onShow()");
}

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    after_switch,
    after_return,
    lifecycle,
    boot_lifecycle: bootLifecycle,
    problems,
};

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
