/**
 * A `set_channels` the agent was asked to SAVE (`persist: true`) outlives the
 * lease's restore.
 *
 *     node tests/js/agent_bridge_persist_restore_probe.mjs
 *
 * Runs the real services/agentBridge.js against a stand-in viewer page (the
 * same shape tests/js/agent_bridge_probe.mjs uses) whose sidebar keeps a
 * "disk": what the last save wrote. The run:
 *
 *   1. the agent replaces the channels with PanCK + CD8, `persist: true`
 *      (the lease is taken here, holding the user's DAPI + CD3 + CD8);
 *   2. the run ends: `restore_viewer` (the server's teardown);
 *   3. the user changes one colour by hand, which saves the list as shown.
 *
 * What it holds: the persisted arrangement is the project's from step 1 on --
 * the restore does not put the pre-session channels back on screen, so the
 * screen and the saved list agree, and the user's next save keeps PanCK + CD8
 * rather than writing the pre-session list over them.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/agentBridge.js");
const CODE = readFileSync(SOURCE, "utf8");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const respond = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });

// -- the server: registration, polls and acks only ---------------------------

const acks = [];
const sessions = new Set();
async function fetchStub(input, init = {}) {
    const url = new URL(String(input), "http://host");
    const method = (init.method || "GET").toUpperCase();
    const match = url.pathname.match(/^\/base\/agent\/v1\/viewer\/sessions(?:\/([^/]+)(?:\/(\w+))?)?$/);
    if (!match) return respond(404, {});
    const [, sid, action] = match;
    if (!sid && method === "POST") {
        const body = JSON.parse(init.body);
        const id = body.session_id || "view_1";
        sessions.add(id);
        return respond(200, { success: true, session: { view_id: id, resumed: false }, idle_poll_s: 5, held_poll_s: 15 });
    }
    if (action === "commands") {
        return respond(200, { success: true, held: false, budget_exhausted: false, commands: [], events: [],
                              attached: false });
    }
    if (action === "acks") { acks.push(JSON.parse(init.body)); return respond(200, { success: true }); }
    return respond(200, { success: true });
}

// -- the page: a ViewerSidebar-shaped stand-in with a disk -------------------

function makeSidebar() {
    const slot = (index, name, enabled, visible = true) => ({
        index, name, enabled, visible, colorHex: "#2388ff", color: { r: 35, g: 136, b: 255 },
        range: [0, 255], userRangeChanged: false, autoLeveled: Boolean(name), autoLeveling: false,
    });
    const shown = (slots) => slots.filter((s) => s && s.visible && s.name)
        .map((s) => ({ name: s.name, enabled: Boolean(s.enabled), color: s.colorHex }));
    const sidebar = {
        columns: ["DAPI", "CD3", "CD8", "PanCK"],
        maxChannelSlots: 15,
        channelSlots: [slot(0, "DAPI", true), slot(1, "CD3", true), slot(2, "CD8", false)],
        _suspended: 0,
        saves: 0,
        // What the project's saved channel list holds: written by every save
        // that is not held back (viewerSidebar.persistChannelList).
        disk: null,
        isHdMode: () => false,
        quantWindow: () => ({ qmin: 0, qmax: 1020 }),
        toRawRangeForSlot: (s) => s.range.map((v) => v * 4),
        rawToByteRange: ([a, b]) => [a / 4, b / 4],
        suspendPersistence() { this._suspended += 1; },
        resumePersistence() { this._suspended -= 1; },
        scheduleSaveChannels() {
            if (this._suspended) return;
            this.saves += 1;
            this.disk = shown(this.channelSlots);
        },
        shown() { return shown(this.channelSlots); },
        setSlotMarker(index, name, options) {
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
        setSlotEnabled(index, enabled) {
            this.channelSlots[index].enabled = enabled;
            this.scheduleSaveChannels();
        },
        setSlotRange(index, range) { this.channelSlots[index].range = range; },
        updateSlotReadout() {},
        async autoChannel() {},
        snapshotSlots() {
            return this.channelSlots.filter((s) => s && s.visible && s.name).map((s) => ({
                name: s.name, colorHex: s.colorHex, enabled: Boolean(s.enabled), visible: true,
                range: s.enabled ? s.range.map((v) => v * 4) : null,
            }));
        },
        snapshotChannelMemory() { return {}; },
        restoreChannelMemory() {},
        createAdditionalSlot() {
            const s = slot(this.channelSlots.length, "", false);
            this.channelSlots.push(s);
            return s;
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
    return sidebar;
}

function makePage() {
    const windowListeners = new Map();
    const documentListeners = new Map();
    const sidebar = makeSidebar();
    const plexora = {
        viewerSidebar: sidebar,
        layers: { has: () => false, get: () => null, order: () => [], subscribe: () => () => {}, describe: () => [] },
        seaDragonViewer: {
            viewer: { addHandler() {}, world: { getItemCount: () => 0 } },
            config: {},
            viewTransform: { get: () => ({ degrees: 0, flipH: false, flipV: false }), subscribe() {} },
        },
        viewerControls: { mode: "none", async selectMode(mode) { this.mode = mode; },
                          offeredModes: () => ({ none: true }) },
        plugins: new Map(),
    };
    const store = new Map();
    const g = {
        console, Math, Number, JSON, Date, Promise, Object, Array, String, Boolean, Error,
        Map, Set, URLSearchParams, URL, Blob, setTimeout, clearTimeout,
        sessionStorage: {
            getItem: (key) => (store.has(key) ? store.get(key) : null),
            setItem: (key, value) => { store.set(key, String(value)); },
            removeItem: (key) => { store.delete(key); },
        },
        fetch: fetchStub,
        plexoraUrl: (path) => "/base/" + String(path).replace(/^\/+/, ""),
        flaskVariables: { datasource: "demo", notebook_mode: false },
        location: { href: "http://host/base/demo", reload() {} },
        navigator: { sendBeacon: () => true },
        __plexora: plexora,
        __plexoraReady: Promise.resolve(),
        PlexoraViewerScene: {
            panTo: () => true, zoomTo: () => true, fitRegion: () => true,
            currentViewport: () => ({ x: 100, y: 200, w: 800, h: 600 }),
            scaleOf: () => 0.5,
            referenceItem: () => ({ source: { dimensions: { x: 4000, y: 3000 } } }),
            levelScale: () => 1,
        },
        PlexoraToolLoader: {
            loaded: [], async openTool() { return { loaded: true }; }, closeTool() {},
            isToolOpen: () => false, loadedTools: () => [], activeTool: () => null,
        },
        PlexoraRouter: { go() {} },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
        document: {
            readyState: "loading", title: "demo - Plexora", visibilityState: "visible",
            body: { appendChild(child) { return child; } },
            createElement: () => ({ style: {}, appendChild(c) { return c; }, addEventListener() {},
                                    setAttribute() {}, remove() {} }),
            querySelectorAll: () => [],
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
    runInContext(CODE, createContext(g), { filename: SOURCE });
    return {
        g, sidebar, bridge: g.PlexoraAgentBridge,
        fireDomReady: () => (documentListeners.get("DOMContentLoaded") || []).forEach((fn) => fn()),
    };
}

let counter = 0;
const command = (type, args = {}) => {
    counter += 1;
    return { command_id: `cmd_${counter}`, seq: counter, type, arguments: args, expected_revision: null };
};

// -- the run -----------------------------------------------------------------

const page = makePage();
page.fireDomReady();
for (let i = 0; i < 40 && !sessions.size; i++) await tick(5);
page.bridge.stop();
const run = async (cmd) => {
    await page.bridge._run(cmd);
    return acks.find((ack) => ack.command_id === cmd.command_id) || null;
};
const names = (rows) => (rows || []).filter((row) => row.enabled !== false).map((row) => row.name).join(",");
const sidebar = page.sidebar;

const saved = await run(command("set_channels", {
    channels: [{ name: "PanCK", color: "#ff0000" }, { name: "CD8", color: "#00ff00", window: [40, 400] }],
    mode: "replace", persist: true,
}));
await tick(20);
check("set_channels persist:true is done and says it persisted",
    saved && saved.status === "done" && saved.result.persisted === true, saved);
check("...and the project's channel list on disk is the agent's", names(sidebar.disk) === "PanCK,CD8",
    sidebar.disk);

const back = await run(command("restore_viewer", { reason: "finished" }));
await tick(20);
check("restore_viewer is done and had a lease", back && back.status === "done" && back.result.had_lease === true,
    back);
check("after the restore the screen still shows the arrangement the agent saved",
    names(sidebar.shown()) === "PanCK,CD8",
    { shown: sidebar.shown(), disk: sidebar.disk, restored: back && back.result.restored });
check("after the restore the screen and the saved channel list agree",
    JSON.stringify(sidebar.shown().map((r) => r.name)) === JSON.stringify((sidebar.disk || []).map((r) => r.name)),
    { shown: sidebar.shown(), disk: sidebar.disk });

// The user's next edit, by hand: one colour on the first channel shown.
sidebar.setSlotColor(0, "#ffffff");
check("the user's next save keeps the persisted channels (it does not write the pre-session list back)",
    names(sidebar.disk) === "PanCK,CD8", { disk: sidebar.disk });

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
