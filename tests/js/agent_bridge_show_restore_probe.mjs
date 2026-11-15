/**
 * A local restore during an `animate_contrast` show ends the show: nothing of
 * it, and nothing held behind it, lands after the viewer is given back.
 *
 *     node tests/js/agent_bridge_show_restore_probe.mjs
 *
 * Runs the real services/agentBridge.js against a stand-in viewer page (the
 * shape tests/js/agent_bridge_persist_restore_probe.mjs uses) whose sidebar
 * keeps a "disk" and drags a slot's window over time, reading the slot by
 * index at each call the way viewerSidebar.dragSlotRange does.
 *
 *   A. Stop mid-show: the show stages PanCK in the second slot over the
 *      user's CD3, and the agent panel's Stop restores locally while a drag
 *      is under way. The user's CD3 comes back with its own window, the
 *      show's later moves do not land on it, the queued CD8 is never staged,
 *      and the user's next save writes CD3's own window.
 *   B. "Continue in background" mid-show: a `set_channels` the session sent
 *      during the show is held; the panel's detach restores locally. Once the
 *      show is over the held look does not come back, no new lease is taken,
 *      and the user's next channel edit is saved.
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
    // Ranges are slider units (raw / 4), as the other probes keep them.
    const slot = (index, name, enabled, range = [0, 255]) => ({
        index, name, enabled, visible: true, colorHex: "#2388ff",
        range, userRangeChanged: false, autoLeveled: Boolean(name), autoLeveling: false,
    });
    const shown = (slots) => slots.filter((s) => s && s.visible && s.name)
        .map((s) => ({ name: s.name, enabled: Boolean(s.enabled), color: s.colorHex, range: s.range.slice() }));
    const log = [];
    return {
        log,
        columns: ["DAPI", "CD3", "CD8", "PanCK"],
        maxChannelSlots: 15,
        channelSlots: [slot(0, "DAPI", true), slot(1, "CD3", true, [10, 200]), slot(2, "CD8", false)],
        _suspended: 0,
        saves: 0,
        disk: null,
        curveMode: "linear",
        isHdMode: () => false,
        suspendPersistence() { this._suspended += 1; },
        resumePersistence() { this._suspended -= 1; },
        scheduleSaveChannels() {
            if (this._suspended) return;
            this.saves += 1;
            this.disk = shown(this.channelSlots);
        },
        shown() { return shown(this.channelSlots); },
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
        setSlotEnabled(index, enabled) {
            this.channelSlots[index].enabled = enabled;
            this.scheduleSaveChannels();
        },
        setSlotRange(index, range) {
            const s = this.channelSlots[index];
            if (s) s.range = range.slice();
        },
        updateSlotReadout() {},
        async autoChannel() {},
        snapshotSlots() {
            return this.channelSlots.filter((s) => s && s.visible && s.name).map((s) => ({
                name: s.name, colorHex: s.colorHex, enabled: Boolean(s.enabled), visible: true,
                range: s.range.map((v) => v * 4),
            }));
        },
        snapshotChannelMemory() { return {}; },
        restoreChannelMemory() {},
        createAdditionalSlot() {
            const s = slot(this.channelSlots.length, "", false);
            this.channelSlots.push(s);
            return s;
        },
        // New slot objects, like viewerSidebar.applyLaunchChannels.
        async applyLaunchChannels(entries) {
            log.push(["applyLaunchChannels", entries.map((e) => e.name)]);
            this.channelSlots = entries.map((entry, i) => slot(i, "", false));
            entries.forEach((entry, i) => {
                this.setSlotMarker(i, entry.name, { enable: entry.enabled !== false });
                if (entry.color) this.setSlotColor(i, entry.color);
                if (entry.range) this.setSlotRange(i, entry.range.map((v) => v / 4));
            });
        },
        // The contrast show's seams.
        stageContrastSlot(name, { index = 1 } = {}) {
            log.push(["stageContrastSlot", index, name]);
            this.suspendPersistence();
            try {
                this.setSlotMarker(index, name, { enable: true });
            } finally {
                this.resumePersistence();
            }
            const s = this.channelSlots[index];
            s.autoLeveled = true;
            return s;
        },
        revealContrastSlot() {},
        async contrastDomain(s, raw) { return [raw[0] / 4, raw[1] / 4]; },
        syncChannelSlider() {},
        syncSlotAutoButton() {},
        // viewerSidebar.dragSlotRange: the slot read by index at the call,
        // frames written over time, stopped when its ease token is taken or
        // the slot changes marker.
        dragSlotRange(index, to, { ms = 100 } = {}) {
            const s = this.channelSlots[index];
            if (!s || !s.name) return Promise.resolve(false);
            log.push(["dragSlotRange", index, s.name, to.slice()]);
            const token = {};
            const marker = s.name;
            s.rangeEase = token;
            const from = s.range.slice();
            const frames = 5;
            return new Promise((resolve) => {
                let n = 0;
                const step = () => {
                    if (s.rangeEase !== token || s.name !== marker) { resolve(false); return; }
                    n += 1;
                    const f = n / frames;
                    this.setSlotRange(index, [from[0] + (to[0] - from[0]) * f, from[1] + (to[1] - from[1]) * f]);
                    if (n >= frames) { s.rangeEase = null; resolve(true); return; }
                    setTimeout(step, Math.max(1, ms / frames));
                };
                setTimeout(step, Math.max(1, ms / frames));
            });
        },
    };
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
            panTo: () => true, zoomTo: () => true, fitRegion: () => true, glideTo: async () => true,
            currentViewport: () => ({ x: 100, y: 200, w: 800, h: 600 }),
            viewOf: () => ({ center: { x: 0, y: 0 }, zoom: 1 }),
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

async function openPage() {
    const page = makePage();
    page.fireDomReady();
    for (let i = 0; i < 40 && !sessions.size; i++) await tick(5);
    page.bridge.stop();
    return page;
}
const until = async (fn, ms = 4000) => {
    for (let waited = 0; waited < ms && !fn(); waited += 10) await tick(10);
    return fn();
};
// Long enough for the whole show (two markers, brisk) to have played out.
const SHOW_MS = 5000;

// -- A: Stop mid-show ----------------------------------------------------------

{
    const page = await openPage();
    const sidebar = page.sidebar;
    await page.bridge._run(command("animate_contrast", {
        channels: [{ name: "PanCK", window: [400, 2000] }, { name: "CD8", window: [100, 900] }],
        slot: 1, pace: "brisk" }));
    // Stop while PanCK is in the slot, its first drag under way.
    await until(() => sidebar.log.some((e) => e[0] === "dragSlotRange"));
    const back = await page.bridge.restore({ reason: "stopped" });
    const at = sidebar.log.length;
    await tick(SHOW_MS);
    const after = sidebar.log.slice(at);
    check("A: the local restore had a lease and rebuilt the channels",
        back.had_lease === true && back.restored.includes("channels"), back);
    check("A: the user's CD3 is back in the second slot with its own window",
        sidebar.channelSlots[1].name === "CD3"
        && JSON.stringify(sidebar.channelSlots[1].range) === JSON.stringify([10, 200]),
        { slot: sidebar.channelSlots[1] });
    check("A: no show move lands after the restore",
        !after.some((e) => e[0] === "dragSlotRange" || e[0] === "stageContrastSlot"), after);
    check("A: the queued CD8 is never staged",
        !sidebar.log.some((e) => e[0] === "stageContrastSlot" && e[2] === "CD8"), sidebar.log);
    check("A: saving is the user's again and no lease is held",
        sidebar._suspended === 0 && page.bridge.hasLease() === false,
        { suspended: sidebar._suspended, lease: page.bridge.hasLease() });
    sidebar.setSlotColor(0, "#ffffff");
    const cd3 = (sidebar.disk || []).find((row) => row.name === "CD3");
    check("A: the user's next save writes CD3's own window, not the show's",
        cd3 && JSON.stringify(cd3.range) === JSON.stringify([10, 200]), sidebar.disk);
}

// -- B: Continue in background mid-show --------------------------------------

{
    const page = await openPage();
    const sidebar = page.sidebar;
    await page.bridge._run(command("animate_contrast", {
        channels: [{ name: "PanCK", window: [400, 2000] }], slot: 1, pace: "brisk" }));
    await until(() => sidebar.log.some((e) => e[0] === "stageContrastSlot"));
    await page.bridge._run(command("set_channels", {
        channels: [{ name: "CD8", color: "#00ff00" }], mode: "replace" }));
    const held = acks.find((ack) => ack.command_id === `cmd_${counter}`);
    check("B: the session's set_channels during the show is held",
        held && held.result && held.result.held === "contrast_show", held);
    const back = await page.bridge.restore({ reason: "detached" });
    await tick(SHOW_MS);
    check("B: the detach restore had a lease", back.had_lease === true, back);
    check("B: the held look does not come back after the show",
        !sidebar.log.some((e) => e[0] === "applyLaunchChannels" && e[1].join() === "CD8"), sidebar.log);
    check("B: no new lease is taken once the show is over", page.bridge.hasLease() === false);
    check("B: channel saving is not left suspended", sidebar._suspended === 0, sidebar._suspended);
    const saves = sidebar.saves;
    sidebar.setSlotColor(0, "#ffffff");
    check("B: the user's next channel edit is saved", sidebar.saves === saves + 1,
        { before: saves, after: sidebar.saves });
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
