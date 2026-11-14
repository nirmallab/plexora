/**
 * A tool asked for twice while its first open is still loading is loaded once.
 *
 * toolLoader.js records a tool in `loadedTools` only at the very end of
 * loadToolUntimed, after the panel fetch, the scripts and activatePlugin. Until
 * then `isToolOpen()` is false and openTool's "already loaded" guard misses, so
 * a second request in that window -- the tool's shortcut pressed again while
 * the placeholder card is up (keyboardShortcuts.js clicks the row, which
 * `pointer-events: none` does not stop), the sidebar launcher (which has no
 * loading state), or the agent bridge's open_tool arriving beside the user's
 * click -- starts a second, complete open:
 *
 *   - the panel is fetched again and written over the same mount, under a
 *     controller that may already hold handles on the first copy;
 *   - the panel's header actions are lifted into the card a second time;
 *   - for a definition that is already registered (a reopen after the card's X,
 *     a plugin whose scripts are on the page), activatePlugin runs twice: two
 *     instances, two sidebar controllers, and main.js's plugin record keeps
 *     only the last, so deactivatePlugin can never unregister the first.
 *
 * The fetch is held open by hand, so the second request always lands inside
 * the first open's window: the interleaving is forced, not timed.
 *
 * Run directly:  node tests/js/tool_double_open_probe.mjs
 *   --source <path>   probe a different toolLoader.js
 * Reports JSON on stderr. Exit 0 = one load per tool. Exit 1 = not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { matches } from "./mini_dom.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(REPO, "plexora/client/src/js/views/toolLoader.js")
    : process.argv[sourceArg + 1];

//: The panel the server sends: a body, and a header action staged for the card.
const FRAGMENT = "<div data-tool-extras><button>Export</button></div><section>panel</section>";
const SCRIPT = "/plugins/probe_tool/static/probeTool.js?v=probe";

/** A DOM stand-in with real parent/child links and the selectors toolLoader
 *  uses (`.class`, `[attr]`, `[attr="v"]`, `tag[attr="v"]`, descendants). */
function makeWorld() {
    const counts = { fetches: 0, activations: 0 };
    const heldFetches = [];
    const listeners = new Map();

    function makeNode(tag) {
        const classes = new Set();
        const node = {
            tagName: String(tag).toUpperCase(),
            dataset: {},
            attributes: {},
            children: [],
            parentNode: null,
            html: "",
            style: {},
            classList: {
                add: (...c) => c.forEach((one) => classes.add(one)),
                remove: (...c) => c.forEach((one) => classes.delete(one)),
                toggle: (c, on) => ((on === undefined ? !classes.has(c) : on)
                    ? classes.add(c) : classes.delete(c)),
                contains: (c) => classes.has(c),
            },
            get className() { return Array.from(classes).join(" "); },
            set className(v) {
                classes.clear();
                String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
            },
            get textContent() { return node.text || ""; },
            set textContent(v) { node.text = String(v); },
            // A fragment written here replaces what was there, as the real
            // thing does. Only the staged header action is materialised: it is
            // the one part of the markup the loader goes looking for.
            set innerHTML(v) {
                node.html = String(v);
                node.children.forEach((child) => { child.parentNode = null; });
                node.children = [];
                if (node.html.includes("data-tool-extras")) {
                    const staged = makeNode("div");
                    staged.setAttribute("data-tool-extras", "");
                    node.appendChild(staged);
                }
            },
            get innerHTML() { return node.html; },
            setAttribute(name, value) {
                if (name === "class") node.className = value;
                else node.attributes[name] = String(value);
            },
            getAttribute(name) {
                if (name === "class") return node.className;
                return Object.prototype.hasOwnProperty.call(node.attributes, name)
                    ? node.attributes[name] : null;
            },
            removeAttribute(name) { delete node.attributes[name]; },
            appendChild(child) {
                child.parentNode?.removeChild?.(child);
                node.children.push(child);
                child.parentNode = node;
                return child;
            },
            insertBefore(child, before) {
                child.parentNode?.removeChild?.(child);
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
            querySelectorAll(selector) {
                const out = [];
                const walk = (at) => at.children.forEach((child) => {
                    if (selector.split(",").some((one) => matches(child, one))) out.push(child);
                    walk(child);
                });
                walk(node);
                return out;
            },
            querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
            addEventListener(type, fn) {
                if (!node.listeners) node.listeners = {};
                (node.listeners[type] = node.listeners[type] || []).push(fn);
            },
            removeEventListener() {},
            click() {
                (node.listeners?.click || []).forEach((fn) => fn({ preventDefault() {}, target: node }));
            },
            scrollIntoView() {},
        };
        return node;
    }

    const root = makeNode("html");
    const head = makeNode("head");
    const body = makeNode("body");
    root.appendChild(head);
    root.appendChild(body);

    const bodyDiv = makeNode("div");
    bodyDiv.setAttribute("id", "bodyDiv");
    body.appendChild(bodyDiv);

    const slot = makeNode("div");
    slot.setAttribute("id", "tool_panel_slot");
    slot.classList.add("tool-panel-hidden");
    body.appendChild(slot);

    // The Tools-menu row, and the sidebar launcher that hands the row to
    // toggleTool -- the two things a user presses.
    const menu = makeNode("ul");
    body.appendChild(menu);
    const row = makeNode("a");
    row.setAttribute("data-tool", "probe_tool");
    row.dataset.tool = "probe_tool";
    row.classList.add("dropdown-item");
    menu.appendChild(row);

    const launchers = makeNode("div");
    launchers.setAttribute("id", "viewer_tool_launchers");
    body.appendChild(launchers);
    const launcher = makeNode("button");
    launcher.setAttribute("data-tool", "probe_tool");
    launcher.dataset.tool = "probe_tool";
    launcher.setAttribute("aria-label", "Probe tool");
    launchers.appendChild(launcher);

    // The plugin's script is already on the page: a reopen after the card's
    // X, or a lazy plugin whose definition registered at boot.
    const script = makeNode("script");
    script.setAttribute("src", SCRIPT);
    script.src = `http://probe.local${SCRIPT}`;
    head.appendChild(script);

    const document = {
        baseURI: "http://probe.local/viewer",
        head,
        body,
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => root.querySelector(`[id="${id}"]`),
        querySelectorAll(selector) {
            const launcherPrefix = "#viewer_tool_launchers ";
            if (selector.startsWith(launcherPrefix)) {
                return launchers.querySelectorAll(selector.slice(launcherPrefix.length));
            }
            return root.querySelectorAll(selector);
        },
        querySelector(selector) { return document.querySelectorAll(selector)[0] || null; },
        addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
        removeEventListener() {},
        contains: (node) => {
            for (let at = node; at; at = at.parentNode) if (at === root) return true;
            return false;
        },
    };

    const definition = { name: "probe_tool" };
    const window = {
        addEventListener() {},
        removeEventListener() {},
        flaskVariables: { datasource: "probe_datasource" },
        PLEXORA_BASE_URL: "",
        __plexoraReady: Promise.resolve(),
        Plexora: { plugins: { get: (name) => (name === "probe_tool" ? definition : undefined) } },
        __plexora: {
            activatePlugin: async () => {
                counts.activations += 1;
                return { sidebarController: { id: counts.activations, onShow() {}, onHide() {} } };
            },
        },
    };

    const globals = {
        console, Promise, Object, Array, Map, Set, JSON, String, Error, URL, Boolean, Number, Math,
        setTimeout, clearTimeout,
        document,
        window,
        // Held until released, so a second request is certain to arrive while
        // the first open is still waiting on its panel.
        fetch: () => {
            counts.fetches += 1;
            return new Promise((resolve) => {
                heldFetches.push(() => resolve({
                    ok: true,
                    json: async () => ({ fragments: { tool_panel_slot: FRAGMENT },
                        scripts: [SCRIPT], styles: [] }),
                }));
            });
        },
    };

    return {
        globals, counts, slot, row, launcher, listeners,
        release() { heldFetches.splice(0).forEach((resolve) => resolve()); },
    };
}

function boot() {
    const world = makeWorld();
    const ctx = createContext(world.globals);
    runInContext(readFileSync(join(REPO, "plexora/client/src/js/views/cardList.js"), "utf8"), ctx);
    runInContext(readFileSync(SOURCE, "utf8"), ctx);
    world.listeners.get("document:DOMContentLoaded")?.();
    world.loader = ctx.window.PlexoraToolLoader;
    return world;
}

/** Let every queued promise and zero-delay timer run. */
async function settle(rounds = 20) {
    for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function extrasLifted(world) {
    const card = world.slot.querySelector('[data-tool-card="probe_tool"]');
    const header = card?.querySelector(".tool-card-header");
    return header ? header.querySelectorAll("[data-tool-extras]").length : 0;
}

const problems = [];

// 1. The user presses the tool's row (its shortcut clicks the same row), and
//    then the launcher while the placeholder card is still up.
const pressed = boot();
pressed.row.click();
await settle(2);
const openWhileLoading = pressed.loader.isToolOpen("probe_tool");
pressed.launcher.click();
await settle(2);
pressed.release();
await settle();
pressed.release(); // a second open's fetch, if one was started after the first released
await settle();
const pressedReport = {
    is_open_while_loading: openWhileLoading,
    fetches: pressed.counts.fetches,
    activations: pressed.counts.activations,
    extras_lifted: extrasLifted(pressed),
    loaded: pressed.loader.loadedTools(),
};
if (pressedReport.fetches !== 1) {
    problems.push(`a second press while the tool was loading fetched its panel again `
        + `(${pressedReport.fetches} fetches)`);
}
if (pressedReport.activations !== 1) {
    problems.push(`a second press while the tool was loading activated it again `
        + `(${pressedReport.activations} activatePlugin calls)`);
}
if (pressedReport.extras_lifted !== 1) {
    problems.push(`the panel's header actions were lifted into the card `
        + `${pressedReport.extras_lifted} times`);
}

// 2. The agent bridge's open_tool arrives while the user's open is loading.
const raced = boot();
const userOpen = raced.loader.openTool("probe_tool");
await settle(2);
const agentOpen = raced.loader.openTool("probe_tool", null, { quiet: true });
await settle(2);
raced.release();
await settle();
raced.release();
const outcomes = await Promise.all([userOpen, agentOpen]);
await settle();
const racedReport = {
    fetches: raced.counts.fetches,
    activations: raced.counts.activations,
    outcomes,
    extras_lifted: extrasLifted(raced),
};
if (racedReport.fetches !== 1) {
    problems.push(`two opens of one tool fetched its panel ${racedReport.fetches} times`);
}
if (racedReport.activations !== 1) {
    problems.push(`two opens of one tool activated it ${racedReport.activations} times `
        + `-- two plugin instances and two sidebar controllers`);
}
if (!outcomes.every((outcome) => outcome?.loaded)) {
    problems.push(`not every open of the tool reported it loaded: ${JSON.stringify(outcomes)}`);
}

console.error(JSON.stringify({ pressed: pressedReport, raced: racedReport, problems }, null, 2));
process.exit(problems.length ? 1 : 0);
