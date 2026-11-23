/**
 * An agent that only moves Thresholding's marker still gets the user's
 * channel slots back when it hands the viewer back.
 *
 *     node tests/js/agent_restore_marker_slot_probe.mjs
 *
 * The real services/agentBridge.js, the real gating listeners
 * (gatingAgentBridge.js) and the real GatingSidebarController.setGateMarker,
 * against a ViewerSidebar-shaped stand-in. `set_active_marker` (and
 * `preview_gate` on another marker) reach setGateMarker with only
 * `persist: false`, so the marker is mirrored into channel slot 2 (index 1),
 * over the user's own channel there. `restore_viewer` must give that slot
 * back, and the next channel save must not write the agent's marker into the
 * project's channel list.
 *
 * Prints `PASS <check>` per check that holds and `FAIL <check>` otherwise;
 * exits 1 on any failure.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const BRIDGE = join(REPO, "plexora/client/src/js/services/agentBridge.js");
const STATIC = join(REPO, "plexora/plugins/gating/static");

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const results = [];
function check(name, condition, detail) {
    results.push({ name, ok: Boolean(condition), detail });
}

function respond(status, data) {
    return { status, ok: status >= 200 && status < 300, json: async () => data };
}

/** Just enough of `/agent/v1` for a session to exist and acks to land. */
function makeServer() {
    const server = { acks: [], polls: 0 };
    server.fetch = async (input, init = {}) => {
        const url = new URL(String(input), "http://host");
        const method = (init.method || "GET").toUpperCase();
        if (url.pathname === "/base/config") return respond(200, {});
        const match = url.pathname.match(/^\/base\/agent\/v1\/viewer\/sessions(?:\/([^/]+)(?:\/(\w+))?)?$/);
        if (!match) return respond(404, {});
        const [, sid, action] = match;
        if (!sid && method === "POST") {
            return respond(200, { success: true, session: { view_id: "view_1", resumed: false },
                                  idle_poll_s: 5, held_poll_s: 15 });
        }
        if (sid && !action) return respond(200, { success: true });
        if (action === "commands") {
            server.polls += 1;
            return respond(200, { success: true, held: false, budget_exhausted: false,
                                  commands: [], events: [], attached: false });
        }
        if (action === "acks") {
            server.acks.push(JSON.parse(init.body));
            return respond(200, { success: true });
        }
        return respond(200, { success: true });
    };
    return server;
}

/** A ViewerSidebar stand-in: setters schedule a save the way the real ones
 *  do, and a save records the channel list it would write. */
function makeSidebar() {
    const slot = (index, name, enabled, visible = true) => ({
        index, name, enabled, visible, colorHex: ["#0000ff", "#00ff00", "#ff0000", "#ffffff"][index],
        range: [0, 255], userRangeChanged: false, autoLeveled: Boolean(name), autoLeveling: false,
    });
    return {
        channelSlots: [slot(0, "DAPI", true), slot(1, "CD3", true), slot(2, "CD8", false),
                       slot(3, "", false, false)],
        _suspended: 0,
        saved: [],
        suspendPersistence() { this._suspended += 1; },
        resumePersistence() { this._suspended -= 1; },
        scheduleSaveChannels() {
            if (this._suspended) return;
            this.saved.push(this.channelSlots.filter((s) => s.visible && s.name)
                .map((s) => s.name));
        },
        setSlotMarker(index, name, options = {}) {
            const s = this.channelSlots[index];
            s.name = name;
            s.visible = true;
            if (options.enable) s.enabled = true;
            this.scheduleSaveChannels();
        },
        setSlotColor(index, hex) {
            this.channelSlots[index].colorHex = hex;
            this.scheduleSaveChannels();
        },
        setSlotRange(index, range) { this.channelSlots[index].range = range; },
        snapshotSlots() {
            return this.channelSlots.filter((s) => s && s.visible && s.name).map((s) => ({
                name: s.name, colorHex: s.colorHex, enabled: Boolean(s.enabled), visible: true,
                range: s.enabled ? [...s.range] : null,
            }));
        },
        async applyLaunchChannels(entries) {
            this.channelSlots = entries.map((entry, i) => slot(i, "", false));
            entries.forEach((entry, i) => {
                this.setSlotMarker(i, entry.name, { enable: entry.enabled !== false });
                if (entry.color) this.setSlotColor(i, entry.color);
                if (entry.range) this.setSlotRange(i, entry.range);
            });
        },
    };
}

function makePage() {
    const server = makeServer();
    const sidebar = makeSidebar();
    const windowListeners = new Map();
    const documentListeners = new Map();
    const element = (tag) => ({
        tag, children: [], style: {}, dataset: {}, listeners: {},
        appendChild(child) { this.children.push(child); return child; },
        addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
        setAttribute(name, value) { this[name] = value; },
        remove() { this.removed = true; },
    });
    const g = {
        console, Math, Number, JSON, Date, Promise, Object, Array, String, Boolean, Error,
        Map, Set, URLSearchParams, URL, Blob, setTimeout, clearTimeout,
        sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        fetch: server.fetch,
        plexoraUrl: (path) => "/base/" + String(path).replace(/^\/+/, ""),
        flaskVariables: { datasource: "demo", notebook_mode: false },
        location: { href: "http://host/base/demo", reload() {} },
        navigator: { sendBeacon: () => true },
        __plexoraReady: Promise.resolve(),
        CSVGatingList: { events: { GATING_BRUSH_MOVE: "GATING_BRUSH_MOVE" } },
        PlexoraViewerScene: {
            fitRegion: () => true,
            currentViewport: () => ({ x: 100, y: 200, w: 800, h: 600 }),
        },
        PlexoraToolLoader: {
            loaded: ["gating"],
            async openTool() { return { loaded: true }; },
            closeTool() {},
            isToolOpen(name) { return this.loaded.includes(name); },
            loadedTools() { return [...this.loaded]; },
            activeTool() { return "gating"; },
        },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
        document: {
            readyState: "loading",
            title: "demo - Plexora",
            visibilityState: "visible",
            body: element("body"),
            createElement: element,
            createElementNS: (_ns, tag) => element(tag),
            getElementById: () => null,
            querySelectorAll: (selector) => (selector === "a[data-tool]"
                ? [{ dataset: { tool: "gating" } }] : []),
            addEventListener(type, fn) {
                (documentListeners.get(type) || documentListeners.set(type, []).get(type)).push(fn);
            },
            removeEventListener() {},
        },
        addEventListener(type, fn) {
            (windowListeners.get(type) || windowListeners.set(type, []).get(type)).push(fn);
        },
        removeEventListener() {},
        dispatchEvent(event) {
            for (const fn of windowListeners.get(event.type) || []) fn(event);
            return true;
        },
    };
    g.window = g;
    g.parent = g;
    const ctx = createContext(g);
    runInContext(readFileSync(BRIDGE, "utf8"), ctx, { filename: BRIDGE });
    runInContext(readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
        + "\nglobalThis.GatingSidebarController = GatingSidebarController;", ctx);
    runInContext(readFileSync(join(STATIC, "gatingAgentBridge.js"), "utf8"), ctx);

    // Thresholding's controller with the REAL setGateMarker; what it calls
    // that draws the panel is a no-op here.
    const noop = () => {};
    const controller = {
        gateMarker: "CD3",
        gateMarkerSelect: null,
        agentPreview: null,
        sidebar,
        ctx: { dataset: { image: new Set(["DAPI", "CD3", "CD8", "PanCK"]) } },
        dataLayer: { getFullChannelName: (name) => name },
        gatingList: { columns: ["CD3", "CD8", "PanCK"], gating_channels: {}, selections: {} },
        getGateMarkerNames() { return [...this.gatingList.columns]; },
        getGateRange: () => [0, 4000],
        getCustomGatedChannels: () => ({}),
        ensureGateSelection(name) { this.agentPreview = null; this.ensured = name; },
        // The real setGateRange writes both lists (minus drawing).
        setGateRange(values) {
            this.agentPreview = null;
            this.gatingList.gating_channels[this.gateMarker] = values;
            this.gatingList.selections = { [this.gateMarker]: values };
        },
        paintMarkerSteps: noop, syncGateSlider: noop, syncAutoButton: noop,
        drawGateDistribution: noop, paintConsistency: noop, paintProvenance: noop,
        paintQcNote: noop, paintReviewEvidence: noop, syncReview: noop, stageChannels: noop,
        scheduleSaveGating() { this.gatingSaves = (this.gatingSaves || 0) + 1; },
    };
    controller.setGateMarker = g.GatingSidebarController.prototype.setGateMarker;
    g.__plexora = {
        viewerSidebar: sidebar,
        seaDragonViewer: { viewer: { addHandler() {}, world: { getItemCount: () => 0 } }, config: {} },
        viewerControls: { mode: "none", async selectMode(mode) { this.mode = mode; } },
        plugins: new Map([["gating", { sidebarController: controller }]]),
    };
    return {
        g, server, sidebar, controller,
        bridge: g.PlexoraAgentBridge,
        fireDomReady: () => (documentListeners.get("DOMContentLoaded") || []).forEach((fn) => fn()),
    };
}

let counter = 0;
function command(type, args = {}) {
    counter += 1;
    return { command_id: `cmd_${counter}`, seq: counter, type, arguments: args, expected_revision: null };
}

async function boot(page) {
    page.fireDomReady();
    for (let i = 0; i < 40 && !page.server.polls; i++) await tick(5);
    page.bridge.stop();
}

async function run(page, cmd) {
    await page.bridge._run(cmd);
    return page.server.acks.find((ack) => ack.command_id === cmd.command_id) || null;
}

const slotNames = (sidebar) => sidebar.channelSlots.filter((s) => s.visible && s.name).map((s) => s.name);

// -- set_active_marker, then restore_viewer ------------------------------------
{
    const page = makePage();
    await boot(page);
    const before = slotNames(page.sidebar);
    const moved = await run(page, command("set_active_marker", { marker: "PanCK" }));
    check("set_active_marker is claimed by Thresholding and done",
        moved && moved.status === "done" && moved.result.active_marker === "PanCK", moved);
    check("...and mirrors the agent's marker into channel slot 2 over the user's CD3 (setup)",
        page.sidebar.channelSlots[1].name === "PanCK", slotNames(page.sidebar));
    const handedBack = [];
    page.g.addEventListener("plexora:agent-restore", (event) => handedBack.push(event.detail.plugins));
    const back = await run(page, command("restore_viewer", { reason: "closed" }));
    await tick(20);
    check("restore_viewer is done with the lease it gave back",
        back && back.status === "done" && back.result.had_lease === true, back);
    check("restore_viewer after set_active_marker puts the user's channel back in slot 2",
        page.sidebar.channelSlots[1].name === "CD3",
        { before, after: slotNames(page.sidebar), restored: back && back.result.restored });
    check("restore_viewer after set_active_marker returns the panel to the user's marker",
        page.controller.gateMarker === "CD3",
        { marker: page.controller.gateMarker, leased: handedBack, errors: back && back.result.errors });
    // The next save once saving is the user's again (any edit, a late
    // auto-level): it writes whatever the slots hold.
    page.sidebar.saved.length = 0;
    page.sidebar.scheduleSaveChannels();
    const written = page.sidebar.saved.at(-1) || [];
    check("the first channel save after set_active_marker's restore does not write the agent's marker",
        page.sidebar._suspended === 0 && !written.includes("PanCK") && written.includes("CD3"),
        { suspended: page.sidebar._suspended, written });
}

// -- preview_gate on another marker, then restore_viewer -----------------------
{
    const page = makePage();
    await boot(page);
    const before = slotNames(page.sidebar);
    const previewed = await run(page, command("preview_gate", { marker: "PanCK", low: 300, high: 4000 }));
    check("preview_gate on another marker is claimed by Thresholding and done",
        previewed && previewed.status === "done" && previewed.result.saved === false, previewed);
    check("...and mirrors that marker into channel slot 2 (setup)",
        page.sidebar.channelSlots[1].name === "PanCK", slotNames(page.sidebar));
    await run(page, command("restore_viewer", { reason: "closed" }));
    await tick(20);
    check("restore_viewer after preview_gate puts the user's channel back in slot 2",
        page.sidebar.channelSlots[1].name === "CD3", { before, after: slotNames(page.sidebar) });
}

let failed = 0;
for (const result of results) {
    if (result.ok) {
        console.log(`PASS ${result.name}`);
    } else {
        failed += 1;
        console.log(`FAIL ${result.name} :: ${JSON.stringify(result.detail)}`);
    }
}
console.log(`${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
