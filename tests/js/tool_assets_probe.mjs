/**
 * When a tool is opened from the Tools menu, does every asset the server sent
 * for it actually reach the page?
 *
 * The bug this exists to catch, in full:
 *
 * `/<datasource>/tools/<tool>/panel` has always returned three things --
 * `fragments`, `scripts` and `styles`. toolLoader.js injected the first two and
 * silently dropped the third, from the day the lazy path was written. Nobody
 * noticed while gating's appearance still lived in core's viewer.css, because
 * index.html links that unconditionally. The moment those ~150 lines moved into
 * the plugin's own gating.css -- which is where a plugin's CSS belongs -- the
 * panel started rendering raw: the hidden file input showed as a bare "Choose
 * File" button, the download panel lost its surface, `visibility: hidden` never
 * applied. Opening the same tool via `?tool=gating` looked perfect, because
 * base.html renders `active_tool_styles` server-side.
 *
 * Nothing could see it. `node --check` sees syntax only; the Python suite never
 * executes client JS; the CSS boundary test checks which side OWNS a rule, not
 * whether the stylesheet holding it ever loads; and the golden route inventory
 * compares the payload the server sends, which was correct all along. The two
 * paths were asserted to agree at the server and diverged at the client.
 *
 * So the assertion is not "toolLoader ran without throwing" but "every URL the
 * payload named ended up in the document". A dropped asset is invisible by
 * construction -- that is what made this last -- so the only honest check is to
 * open a tool and look at what the page actually got.
 *
 * Run directly:  node tests/js/tool_assets_probe.mjs
 *   --source <path>   probe a different toolLoader.js (used to prove the probe
 *                     can fail, by mutating a copy)
 * Exit 0 = every script and stylesheet arrived. Exit 1 = at least one did not.
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

/** Exactly what tool_routes.py's tool_panel() returns, with gating's own
 *  asset names -- see Plugin.asset_urls(). */
const PAYLOAD = {
    fragments: {
        tool_panel_slot: "<section id='gate_marker_section'></section>",
        tool_panel_legacy_slot: "<div id='csv_channel_list_wrapper'></div>",
    },
    scripts: [
        "/plugins/gating/static/gatingApi.js?v=probe",
        "/plugins/gating/static/csvGatingList.js?v=probe",
        "/plugins/gating/static/gatingSidebarController.js?v=probe",
    ],
    styles: ["/plugins/gating/static/gating.css?v=probe"],
};

/** Everything the page received, by tag. */
const appended = { script: [], link: [] };
const slotsFilled = [];

/** A DOM stand-in no wider than what toolLoader actually touches: every name
 *  added here is a name this probe stops checking. */
function browserGlobals() {
    const listeners = new Map();

    const element = (tag) => ({
        tagName: tag,
        dataset: {},
        attributes: {},
        classList: { add() {}, remove() {}, toggle() {} },
        // Counted only for a per-tool MOUNT. The card the loader wraps each
        // panel in writes innerHTML too -- an icon glyph per header button --
        // and counting those would make this number about the card's markup
        // rather than about the tool's panel arriving.
        set innerHTML(v) {
            if (this.attributes["data-tool-panel"]) slotsFilled.push(v);
        },
        addEventListener(type, fn) { listeners.set(`${tag}:${type}`, fn); },
        // The loader gives each tool its own mount inside the slot rather than
        // writing the whole slot, so it looks for an existing one and appends a
        // new one. Nothing is on the page here, so every lookup misses and the
        // fragment lands in a freshly created mount -- still one innerHTML write
        // per slot, which is what this probe counts.
        querySelector: () => null,
        appendChild(node) { return node; },
        insertBefore(node) { return node; },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        firstChild: null,
    });

    const document = {
        // Nothing is already on the page, so both assets take the load path
        // rather than the "already present" short-circuit.
        querySelector: () => null,
        querySelectorAll: () => [toolLink],
        createElement: (tag) => element(tag),
        getElementById: (id) => element(`#${id}`),
        head: {
            appendChild(node) {
                (appended[node.tagName] ??= []).push(node);
                // The browser fires these; both loaders await them.
                setTimeout(() => node.onload?.(), 0);
                return node;
            },
        },
        addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
    };

    const toolLink = element("a");
    toolLink.dataset.tool = "gating";

    return {
        __listeners: listeners,
        __toolLink: toolLink,
        console,
        Promise, Object, Array, Map, Set, JSON, String, Error,
        setTimeout, clearTimeout,
        document,
        fetch: async () => ({ json: async () => PAYLOAD, ok: true }),
        window: {
            flaskVariables: { datasource: "probe_datasource" },
            PLEXORA_BASE_URL: "",
            __plexoraReady: Promise.resolve(),
            Plexora: { plugins: new Map([["gating", { name: "gating" }]]) },
            __plexora: {
                activatePlugin: async () => ({ sidebarController: { onShow() {} } }),
            },
        },
    };
}

const ctx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8"), ctx);

// The click handlers are bound on DOMContentLoaded, so fire it the way a
// browser would rather than reaching past it into openTool().
ctx.__listeners.get("document:DOMContentLoaded")?.();
await ctx.__listeners.get("a:click")?.({ preventDefault() {} });

// appendChild's onload is deferred by a tick, and the loader awaits each asset
// in turn -- so this has to wait for four chained timer hops. A fixed 20 ms
// wait was enough on a Linux CI box and raced the loader on Windows, where a
// setTimeout(0) can take a full ~15 ms timer tick: the probe reported the last
// script as "never arrived" at random, on unmodified code. Wait for the count
// to stop moving instead, with a ceiling so a genuinely dropped asset still
// reports rather than hanging here.
const EXPECTED = PAYLOAD.scripts.length + PAYLOAD.styles.length;
for (const deadline = Date.now() + 3000; Date.now() < deadline; ) {
    await new Promise((resolve) => setTimeout(resolve, 10));
    if (appended.script.length + appended.link.length >= EXPECTED) break;
}

const arrived = {
    scripts: appended.script.map((n) => n.src),
    styles: appended.link.filter((n) => n.rel === "stylesheet").map((n) => n.href),
};

const missing = {
    scripts: PAYLOAD.scripts.filter((url) => !arrived.scripts.includes(url)),
    styles: PAYLOAD.styles.filter((url) => !arrived.styles.includes(url)),
};

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    sent: { scripts: PAYLOAD.scripts, styles: PAYLOAD.styles },
    arrived,
    missing,
    fragments_injected: slotsFilled.length,
};

console.error(JSON.stringify(report, null, 2));
process.exit(missing.scripts.length + missing.styles.length ? 1 : 0);
