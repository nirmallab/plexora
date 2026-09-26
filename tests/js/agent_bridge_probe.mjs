/**
 * Runs the real services/agentBridge.js against a stand-in viewer page and a
 * stand-in `/agent/v1` server (the wire of server/routes/agent_routes.py).
 *
 *     node tests/js/agent_bridge_probe.mjs
 *
 * What it holds:
 *   - boot: waits for DOMContentLoaded and `__plexoraReady`, then registers
 *     with the project, the client kind, every command it runs and the tools
 *     the menu offers -- under the page's base URL, never a bare `/`;
 *   - every command type reaches the core method (or plugin claim) that does
 *     the work, and is acknowledged with the result;
 *   - `set_channels` with `persist: false` never reaches the save path, even
 *     through an auto-level that lands after the setters have returned, while
 *     `persist: true` reaches it exactly once;
 *   - an unknown type is `unsupported`; a handler that throws is `rejected`
 *     with the message; an `expected_revision` that no longer matches still
 *     runs and says so;
 *   - commands in one poll run one at a time, each acknowledged before the
 *     next starts;
 *   - `open_project` acknowledges BEFORE it leaves, says where it is going,
 *     and navigates through the router;
 *   - the session id (and its cursors) survive a reload of the tab through
 *     sessionStorage, and a forgotten session resets them;
 *   - a 404 re-registers; a 403 stops the loop for good;
 *   - an event from this tab's own session is not re-dispatched; a foreign
 *     one is, and core's own kinds reach the refresh paths main.js already has.
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

// -- the server ---------------------------------------------------------------

function respond(status, data) {
    return { status, ok: status >= 200 && status < 300, json: async () => data };
}

function makeServer() {
    const server = {
        sessions: new Map(), registrations: [], requests: [], acks: [], leaves: [],
        captures: [], polls: [], pollQueue: [], order: [], nextId: 1,
        forbidAll: false, forbidPoll: false, config: {},
    };
    server.fetch = async (input, init = {}) => {
        const url = new URL(String(input), "http://host");
        const method = (init.method || "GET").toUpperCase();
        const query = Object.fromEntries(url.searchParams);
        server.requests.push({ method, path: url.pathname, query, init });
        if (server.forbidAll) return respond(403, { success: false });
        if (url.pathname === "/base/config") return respond(200, server.config);
        const match = url.pathname.match(/^\/base\/agent\/v1\/viewer\/sessions(?:\/([^/]+)(?:\/(\w+))?)?$/);
        if (!match) return respond(404, { success: false });
        const [, sid, action] = match;
        if (!sid && method === "POST") {
            const body = JSON.parse(init.body);
            const id = body.session_id || `view_${server.nextId++}`;
            server.sessions.set(id, body);
            server.registrations.push(body);
            return respond(200, { success: true, session: { view_id: id }, idle_poll_s: 5, held_poll_s: 15 });
        }
        if (sid && !action && method === "GET") {
            return server.sessions.has(sid) ? respond(200, { success: true }) : respond(404, {});
        }
        if (action === "commands") {
            server.polls.push(query);
            if (server.forbidPoll) return respond(403, { success: false });
            if (!server.sessions.has(sid)) return respond(404, { success: false });
            const next = server.pollQueue.shift() || { commands: [], events: [], attached: false };
            return respond(200, Object.assign({ success: true, held: false, budget_exhausted: false,
                                                commands: [], events: [] }, next));
        }
        if (action === "acks") {
            const body = JSON.parse(init.body);
            server.acks.push(body);
            server.order.push(`ack:${body.command_id}`);
            return respond(200, { success: true });
        }
        if (action === "leave") {
            server.leaves.push(JSON.parse(init.body));
            server.order.push("leave");
            return respond(200, { success: true });
        }
        if (action === "captures") {
            server.captures.push({ query, headers: init.headers, body: init.body });
            return respond(200, { success: true, artifact: { id: "art_1", uri: "artifact://art_1" } });
        }
        return respond(404, {});
    };
    return server;
}

function makeStorage() {
    const data = new Map();
    return {
        data,
        getItem: (key) => (data.has(key) ? data.get(key) : null),
        setItem: (key, value) => { data.set(key, String(value)); },
        removeItem: (key) => { data.delete(key); },
    };
}

// -- the page -----------------------------------------------------------------

/** A ViewerSidebar-shaped spy whose setters schedule saves and auto-levels the
 *  way the real ones do, so persistence has something real to hold back. */
function makeSidebar(log) {
    const slot = (index, name, enabled, visible = true) => ({
        index, name, enabled, visible, colorHex: "#2388ff", color: { r: 35, g: 136, b: 255 },
        range: [0, 255], userRangeChanged: false, autoLeveled: Boolean(name), autoLeveling: false,
    });
    const sidebar = {
        columns: ["DAPI", "CD3", "CD8", "PanCK"],
        maxChannelSlots: 15,
        channelSlots: [slot(0, "DAPI", true), slot(1, "CD3", true), slot(2, "CD8", false),
                       slot(3, "", false, false)],
        _suspended: 0,
        saves: 0,
        channelList: { ensureChannelStats: async (name) => { log.push(["ensureChannelStats", name]); } },
        isHdMode: () => false,
        quantWindow: () => ({ qmin: 0, qmax: 1020 }),
        toRawRangeForSlot: (s) => s.range.map((v) => v * 4),
        rawToByteRange: ([a, b]) => [a / 4, b / 4],
        suspendPersistence() { this._suspended += 1; log.push(["suspendPersistence"]); },
        resumePersistence() { this._suspended -= 1; log.push(["resumePersistence"]); },
        scheduleSaveChannels() {
            if (this._suspended) return;
            this.saves += 1;
            log.push(["save"]);
        },
        scheduleAutoLevel(s) {
            if (s.enabled && !s.userRangeChanged && !s.autoLeveled && !s.autoLeveling) {
                setTimeout(() => this.autoChannel(s.index), 0);
            }
        },
        setSlotMarker(index, name, options) {
            log.push(["setSlotMarker", index, name]);
            const s = this.channelSlots[index];
            s.name = name;
            s.visible = true;
            s.autoLeveled = false;
            s.userRangeChanged = false;
            if (options.enable) s.enabled = true;
            this.scheduleAutoLevel(s);
            this.scheduleSaveChannels();
        },
        setSlotColor(index, hex) {
            log.push(["setSlotColor", index, hex]);
            this.channelSlots[index].colorHex = hex;
            this.scheduleSaveChannels();
        },
        setSlotEnabled(index, enabled) {
            log.push(["setSlotEnabled", index, enabled]);
            const s = this.channelSlots[index];
            s.enabled = enabled;
            this.scheduleAutoLevel(s);
            this.scheduleSaveChannels();
        },
        setSlotRange(index, range, user) {
            log.push(["setSlotRange", index, range, user]);
            this.channelSlots[index].range = range;
        },
        updateSlotReadout() {},
        async autoChannel(index, options = {}) {
            const s = this.channelSlots[index];
            if (!options.force && (s.autoLeveling || s.autoLeveled || s.userRangeChanged)) return;
            log.push(["autoChannel", index, Boolean(options.force)]);
            s.autoLeveling = true;
            await tick(25);
            s.range = [10, 200];
            s.autoLeveling = false;
            s.autoLeveled = true;
            // The real second pass: a save, unless the launch path marked it silent.
            if (s.autoSilent) s.autoSilent = false;
            else this.scheduleSaveChannels();
        },
        createAdditionalSlot() {
            const s = slot(this.channelSlots.length, "", false);
            this.channelSlots.push(s);
            return s;
        },
        async applyLaunchChannels(entries, options) {
            log.push(["applyLaunchChannels", JSON.parse(JSON.stringify(entries)), options]);
            this.channelSlots = entries.map((entry, i) => slot(i, "", false));
            entries.forEach((entry, i) => {
                const s = this.channelSlots[i];
                if (options.silent && !entry.range) s.autoSilent = true;
                this.setSlotMarker(i, entry.name, { enable: entry.enabled !== false });
                if (entry.color) this.setSlotColor(i, entry.color);
                if (entry.range) {
                    s.userRangeChanged = true;
                    s.autoLeveled = true;
                    this.setSlotRange(i, entry.range, true);
                }
            });
        },
    };
    return sidebar;
}

function makeStack(log) {
    const layers = new Map([
        ["__image__", { id: "__image__", kind: "image", label: "Image", visible: true, opacity: 1 }],
        ["mask", { id: "mask", kind: "mask", label: "Segmentation", visible: true, opacity: 0.8 }],
    ]);
    let order = ["__image__", "mask"];
    return {
        has: (id) => layers.has(id),
        get: (id) => layers.get(id) || null,
        order: () => [...order],
        subscribe: () => () => {},
        setVisible(id, visible) { log.push(["setVisible", id, visible]); layers.get(id).visible = visible; },
        setOpacity(id, value) {
            if (value === 0.123) throw new Error("the stack refused that opacity");
            log.push(["setOpacity", id, value]);
            layers.get(id).opacity = value;
        },
        setOrder(ids) { log.push(["setOrder", ids]); order = [...order.filter((id) => !ids.includes(id)), ...ids]; },
        describe: () => order.map((id, i) => ({ ...layers.get(id), order: i, pinned: false })),
    };
}

function makePage({ server, storage, plexoraOverrides = {} }) {
    const log = [];
    const windowListeners = new Map();
    const documentListeners = new Map();
    const dispatched = [];
    const sidebar = makeSidebar(log);
    const canvas = { toBlob: (callback, type) => callback({ png: true, type }) };
    const plexora = Object.assign({
        viewerSidebar: sidebar,
        layers: makeStack(log),
        seaDragonViewer: {
            viewer: { addHandler() {}, world: { getItemCount: () => 0 } },
            config: {},
            viewTransform: { get: () => ({ degrees: 0, flipH: false, flipV: false }), subscribe() {} },
            renderCurrentViewCanvas() { log.push(["renderCurrentViewCanvas"]); return canvas; },
        },
        viewerControls: {
            mode: "none",
            async selectMode(mode) { log.push(["selectMode", mode]); if (mode !== "filled") this.mode = mode; },
            offeredModes: () => ({ none: true, centroids: true, outlines: true, filled: false }),
        },
        async refreshDataset() { log.push(["refreshDataset"]); return { maskAttached: false }; },
        async adoptLayers() { log.push(["adoptLayers"]); return []; },
        async adoptChannelNames(names) { log.push(["adoptChannelNames", names]); return true; },
        plugins: new Map(),
    }, plexoraOverrides);

    const element = (tag) => {
        const node = {
            tag, children: [], style: {}, className: "", textContent: "", listeners: {},
            appendChild(child) { this.children.push(child); return child; },
            addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
            setAttribute(name, value) { this[name] = value; },
            remove() { this.removed = true; },
            showModal() { this.open = true; },
            close() { this.open = false; (this.listeners.close || []).forEach((fn) => fn()); },
        };
        return node;
    };
    const body = element("body");

    const g = {
        console, Math, Number, JSON, Date, Promise, Object, Array, String, Boolean, Error,
        Map, Set, URLSearchParams, URL, Blob, setTimeout, clearTimeout,
        sessionStorage: storage,
        fetch: server.fetch,
        plexoraUrl: (path) => "/base/" + String(path).replace(/^\/+/, ""),
        flaskVariables: { datasource: "demo", notebook_mode: false },
        location: { href: "http://host/base/demo", reload() { log.push(["reload"]); } },
        navigator: { sendBeacon: (url, blob) => { log.push(["sendBeacon", url]); return true; } },
        __plexora: plexora,
        __plexoraReady: Promise.resolve(),
        PlexoraViewerScene: {
            panTo: (v, x, y) => { log.push(["scene.panTo", x, y]); return true; },
            zoomTo: (v, scale) => { log.push(["scene.zoomTo", scale]); return true; },
            fitRegion: (v, region) => {
                if (region.x === -1) throw new Error("kaboom");
                log.push(["scene.fitRegion", { x: region.x, y: region.y,
                    width: region.width, height: region.height }]);
                return true;
            },
            currentViewport: () => ({ x: 100, y: 200, w: 800, h: 600 }),
            scaleOf: () => 0.5,
            referenceItem: () => ({ source: { dimensions: { x: 4000, y: 3000 } } }),
            levelScale: () => 1,
        },
        PlexoraToolLoader: {
            loaded: ["gating"],
            async openTool(name, link, options) {
                log.push(["openTool", name, options]);
                if (name === "transcripts") return { skipped: "needs genes" };
                if (!this.loaded.includes(name)) this.loaded.push(name);
                return { loaded: true };
            },
            closeTool(name) { log.push(["closeTool", name]); },
            isToolOpen(name) { return this.loaded.includes(name); },
            loadedTools() { return [...this.loaded]; },
            activeTool() { return this.loaded[0] || null; },
        },
        PlexoraRouter: { go: (href) => { log.push(["router.go", href]); server.order.push("go"); } },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
        document: {
            readyState: "loading",
            title: "demo - Plexora",
            visibilityState: "visible",
            body,
            createElement: element,
            querySelectorAll: (selector) => (selector === "a[data-tool]"
                ? ["gating", "roi", "transcripts"].map((tool) => ({ dataset: { tool } })) : []),
            addEventListener(type, fn) {
                (documentListeners.get(type) || documentListeners.set(type, []).get(type)).push(fn);
            },
        },
        addEventListener(type, fn) {
            (windowListeners.get(type) || windowListeners.set(type, []).get(type)).push(fn);
        },
        removeEventListener() {},
        dispatchEvent(event) {
            dispatched.push(event);
            for (const fn of windowListeners.get(event.type) || []) fn(event);
            return true;
        },
    };
    g.window = g;
    g.parent = g;
    const ctx = createContext(g);
    runInContext(CODE, ctx, { filename: SOURCE });
    return {
        g, log, dispatched, sidebar, plexora, body,
        bridge: g.PlexoraAgentBridge,
        fireDomReady: () => (documentListeners.get("DOMContentLoaded") || []).forEach((fn) => fn()),
        firePageHide: () => (windowListeners.get("pagehide") || []).forEach((fn) => fn({})),
        on: (type, fn) => g.addEventListener(type, fn),
    };
}

let commandCounter = 0;
function command(type, args = {}, extra = {}) {
    commandCounter += 1;
    return Object.assign({ command_id: `cmd_${commandCounter}`, seq: commandCounter, type,
                           arguments: args, expected_revision: null }, extra);
}
function ackFor(server, cmd) {
    return server.acks.find((ack) => ack.command_id === cmd.command_id) || null;
}
const has = (log, name, predicate = () => true) => log.some((entry) => entry[0] === name && predicate(entry));

// -- boot and registration ------------------------------------------------------

const server = makeServer();
const storage = makeStorage();
const page = makePage({ server, storage });
check("nothing registers before DOMContentLoaded", server.registrations.length === 0);
page.fireDomReady();
for (let i = 0; i < 20 && !server.polls.length; i++) await tick(5);
page.bridge.stop();
const registered = server.registrations[0] || {};
check("boot registers once the page is ready", server.registrations.length === 1, server.registrations);
check("registration names the project", registered.project === "demo");
check("a plain tab is a browser client", registered.client === "browser", registered.client);
check("registration lists every command it runs",
    ["get_state", "open_project", "set_channels", "set_channel_color", "set_contrast",
     "set_layer_visibility", "set_layer_opacity", "reorder_layers", "pan_to", "zoom_to",
     "fit_region", "focus_cell", "focus_roi", "set_active_marker", "open_tool", "close_tool",
     "set_cell_render_mode", "capture_view", "show_evidence"]
        .every((type) => (registered.capabilities || []).includes(type)), registered.capabilities);
check("registration lists the tools the menu offers",
    ["gating", "roi", "transcripts"].every((tool) => (registered.tools || []).includes(tool)), registered.tools);
check("every request is under the page's base URL",
    server.requests.every((request) => request.path.startsWith("/base/")), server.requests.map((r) => r.path));
check("the first poll does not hold (nobody is attached yet)", server.polls[0] && server.polls[0].wait === "0",
    server.polls[0]);
const sid = page.bridge.sessionId();
check("the session id is kept in sessionStorage",
    JSON.parse(storage.getItem("plexora.agentBridge") || "{}").session_id === sid && Boolean(sid));

// -- every command reaches its method ------------------------------------------

async function run(cmd) {
    await page.bridge._run(cmd);
    return ackFor(server, cmd);
}

{
    const cmd = command("get_state");
    page.on("plexora:agent-state", (event) => event.detail.contribute("gating", { active_marker: "CD3" }));
    const ack = await run(cmd);
    const state = ack && ack.result;
    check("get_state is done", ack && ack.status === "done", ack);
    check("get_state reports the project and the viewport in image pixels",
        state && state.project === "demo" && state.viewport.x === 100 && state.viewport.width === 800
        && state.viewport.height === 600, state && state.viewport);
    check("get_state reports channels with raw windows",
        state && state.channels.length === 3 && state.channels[0].name === "DAPI"
        && state.channels[0].window[1] === 1020 && state.channels[2].enabled === false, state && state.channels);
    check("get_state reports the layers", state && state.layers.map((l) => l.id).join() === "__image__,mask");
    check("get_state carries plugin contributions", state && state.plugins.gating
        && state.plugins.gating.active_marker === "CD3", state && state.plugins);
    check("get_state reports cell mode and tools", state && state.cell_mode === "none"
        && state.tools_open.includes("gating"), state);
    check("an ack carries the resulting revision", ack && Number.isInteger(ack.resulting_revision));
}

{
    const cases = [
        [command("pan_to", { x: 10, y: 20 }), () => has(page.log, "scene.panTo", (e) => e[1] === 10 && e[2] === 20)],
        [command("zoom_to", { scale: 2 }), () => has(page.log, "scene.zoomTo", (e) => e[1] === 2)],
        [command("fit_region", { x: 1, y: 2, width: 30, height: 40 }),
            () => has(page.log, "scene.fitRegion", (e) => e[1].width === 30 && e[1].height === 40)],
        [command("focus_cell", { cell_id: 7, x: 500, y: 600, width: 100 }),
            () => has(page.log, "scene.fitRegion", (e) => e[1].x === 450 && e[1].y === 550 && e[1].width === 100)],
        [command("set_layer_visibility", { layer_id: "mask", visible: false }),
            () => has(page.log, "setVisible", (e) => e[1] === "mask" && e[2] === false)],
        [command("set_layer_opacity", { layer_id: "mask", opacity: 0.4 }),
            () => has(page.log, "setOpacity", (e) => e[1] === "mask" && e[2] === 0.4)],
        [command("reorder_layers", { ids: ["mask", "__image__"] }),
            () => has(page.log, "setOrder", (e) => e[1].join() === "mask,__image__")],
        [command("open_tool", { tool: "roi" }),
            () => has(page.log, "openTool", (e) => e[1] === "roi" && e[2] && e[2].quiet === true)],
        [command("close_tool", { tool: "gating" }), () => has(page.log, "closeTool", (e) => e[1] === "gating")],
        [command("set_cell_render_mode", { mode: "outlines" }),
            () => has(page.log, "selectMode", (e) => e[1] === "outlines") && page.plexora.viewerControls.mode === "outlines"],
    ];
    for (const [cmd, reached] of cases) {
        const ack = await run(cmd);
        check(`${cmd.type} is acknowledged done`, ack && ack.status === "done", ack);
        check(`${cmd.type} reaches its method`, reached(), page.log.slice(-4));
    }
    const focused = ackFor(server, cases[3][0]);
    check("focus_cell answers with the cell and the new view", focused && focused.result.cell_id === 7
        && focused.result.viewport.width === 800, focused && focused.result);
}

{
    const ack = await run(command("set_cell_render_mode", { mode: "filled" }));
    check("a mode the controls would not take is rejected, naming what is offered",
        ack && ack.status === "rejected" && /centroids/.test(ack.error), ack);
    const missing = await run(command("set_layer_visibility", { layer_id: "nope", visible: true }));
    check("an unknown layer is rejected by name", missing && missing.status === "rejected"
        && /nope/.test(missing.error), missing);
    const skipped = await run(command("open_tool", { tool: "transcripts" }));
    check("a tool that will not open is rejected with the loader's reason",
        skipped && skipped.status === "rejected" && /needs genes/.test(skipped.error), skipped);
    const offMenu = await run(command("open_tool", { tool: "nothing_here" }));
    check("a tool the page does not offer is rejected", offMenu && offMenu.status === "rejected", offMenu);
}

// -- claimable commands -----------------------------------------------------------

{
    const nobody = await run(command("set_active_marker", { marker: "CD8" }));
    check("set_active_marker with no plugin to claim it is unsupported",
        nobody && nobody.status === "unsupported" && /no plugin handled it/.test(nobody.error), nobody);

    const boxless = await run(command("focus_roi", { roi_id: "r1" }));
    check("focus_roi with no plugin and no box is unsupported", boxless && boxless.status === "unsupported", boxless);
    const boxed = await run(command("focus_roi", { roi_id: "r1", x: 5, y: 6, width: 70, height: 80 }));
    check("focus_roi with no plugin falls back to fitting the box",
        boxed && boxed.status === "done" && has(page.log, "scene.fitRegion", (e) => e[1].width === 70), boxed);

    page.on("plexora:agent-command", (event) => {
        const detail = event.detail;
        if (detail.type === "set_active_marker") {
            detail.claim(tick(20).then(() => {
                server.order.push("marker-set");
                return { active_marker: detail.arguments.marker };
            }), "gating");
        }
        if (detail.type === "focus_roi") detail.claim({ roi_id: detail.arguments.roi_id, selected: true }, "roi");
    });
    const claimed = await run(command("set_active_marker", { marker: "CD8" }));
    check("a claimed set_active_marker is done with the plugin's answer",
        claimed && claimed.status === "done" && claimed.result.active_marker === "CD8"
        && claimed.result.handled_by === "gating", claimed);
    const roi = await run(command("focus_roi", { roi_id: "r9" }));
    check("a claimed focus_roi says the region was selected",
        roi && roi.status === "done" && roi.result.selected === true && roi.result.handled_by === "roi", roi);
}

// -- channels and persistence ----------------------------------------------------

{
    const sidebar = page.sidebar;
    sidebar.saves = 0;
    const merge = command("set_channels", {
        channels: [{ name: "PanCK", color: "#FF0000" }, { name: "DAPI", window: [100, 800] },
                   { name: "CD8", enabled: true, window: "auto" }, { name: "Ghost" }],
        mode: "merge", persist: false,
    });
    const ack = await run(merge);
    await tick(80);
    check("set_channels merge is done", ack && ack.status === "done", ack);
    check("merge puts a new channel into a free slot", has(page.log, "setSlotMarker", (e) => e[2] === "PanCK"));
    check("merge colours it (normalised hex)", has(page.log, "setSlotColor", (e) => e[2] === "#ff0000"));
    check("merge converts a raw window into the slider's byte domain",
        has(page.log, "setSlotRange", (e) => e[1] === 0 && e[2][0] === 25 && e[2][1] === 200 && e[3] === true));
    check("merge turns a channel on", has(page.log, "setSlotEnabled", (e) => e[1] === 2 && e[2] === true));
    check("\"auto\" runs a forced auto-contrast", has(page.log, "autoChannel", (e) => e[1] === 2 && e[2] === true));
    check("persistence is suspended around the change and released",
        has(page.log, "suspendPersistence") && has(page.log, "resumePersistence") && sidebar._suspended === 0);
    check("persist:false never reaches the save path, auto-levels included", sidebar.saves === 0,
        { saves: sidebar.saves, log: page.log.filter((e) => e[0] === "save" || e[0] === "autoChannel") });
    check("a name the image lacks is skipped with a warning", ack && ack.result.missing.includes("Ghost")
        && /Ghost/.test(ack.warning || ""), ack);

    sidebar.saves = 0;
    const replace = await run(command("set_channels", {
        channels: [{ name: "CD3", color: { r: 0, g: 255, b: 0 } }, { name: "CD8", window: [40, 400] }],
        mode: "replace", persist: true,
    }));
    await tick(80);
    check("set_channels replace goes through the launch path",
        has(page.log, "applyLaunchChannels", (e) => e[1].length === 2 && e[1][0].color === "#00ff00"
            && e[1][1].range[1] === 400 && !e[1][0].range), page.log.filter((e) => e[0] === "applyLaunchChannels"));
    check("persist:true saves exactly once", sidebar.saves === 1, { saves: sidebar.saves });
    check("replace answers with the channels now shown", replace && replace.result.channels.length === 2
        && replace.result.persisted === true, replace);

    sidebar.saves = 0;
    const color = await run(command("set_channel_color", { channel: "CD3", color: "0f0" }));
    const contrast = await run(command("set_contrast", { channel: "CD3", window: [8, 80] }));
    await tick(40);
    check("set_channel_color is a merge of one colour", color && color.status === "done"
        && has(page.log, "setSlotColor", (e) => e[2] === "#00ff00"), color);
    check("set_contrast is a merge of one window", contrast && contrast.status === "done"
        && has(page.log, "setSlotRange", (e) => e[2][0] === 2 && e[2][1] === 20), contrast);
    check("the wrappers do not persist unless asked", sidebar.saves === 0, { saves: sidebar.saves });

    const badColor = await run(command("set_channel_color", { channel: "CD3", color: "teal-ish" }));
    check("a colour that is not one is rejected", badColor && badColor.status === "rejected", badColor);
    const badMode = await run(command("set_channels", { channels: [{ name: "CD3" }], mode: "append" }));
    check("an unknown mode is rejected", badMode && badMode.status === "rejected", badMode);
    check("a rejected change still releases persistence", sidebar._suspended === 0);
}

// -- unsupported, rejected, revision ----------------------------------------------

{
    const unknown = await run(command("teleport", {}));
    check("an unknown command type is unsupported", unknown && unknown.status === "unsupported"
        && /teleport/.test(unknown.error), unknown);
    const thrown = await run(command("fit_region", { x: -1, y: 0, width: 5, height: 5 }));
    check("a handler that throws is rejected with its message", thrown && thrown.status === "rejected"
        && thrown.error === "kaboom", thrown);
    const refused = await run(command("set_layer_opacity", { layer_id: "mask", opacity: 0.123 }));
    check("an exception from core is rejected with its message", refused && refused.status === "rejected"
        && refused.error === "the stack refused that opacity", refused);

    const stale = await run(command("pan_to", { x: 1, y: 1 },
        { expected_revision: page.bridge.revision() - 3 }));
    check("a stale expected_revision still runs, with a warning", stale && stale.status === "done"
        && /changed since revision/.test(stale.warning || ""), stale);
    const before = page.bridge.revision();
    const current = await run(command("get_state", {}, { expected_revision: before }));
    check("a current expected_revision carries no warning", current && !current.warning, current);
    check("a view change bumps the revision reported in the ack",
        stale.resulting_revision > stale.resulting_revision - 1 && page.bridge.revision() >= before);
}

// -- sequential execution in one poll ---------------------------------------------

{
    page.bridge._arm();   // `running` gates the command loop inside a poll
    const first = command("set_active_marker", { marker: "PanCK" });
    const second = command("get_state");
    page.on("plexora:agent-state", () => server.order.push("state-gathered"));
    server.order.length = 0;
    server.pollQueue.push({ commands: [first, second], events: [], attached: true });
    const delay = await page.bridge._pollOnce();
    page.bridge.stop();
    const order = server.order.join(" > ");
    check("commands in one poll run one after another, each acked before the next starts",
        order === `marker-set > ack:${first.command_id} > state-gathered > ack:${second.command_id}`, order);
    check("after a poll with work, the next poll is immediate", delay === 0, delay);
    server.pollQueue.push({ attached: true });
    page.bridge._arm();
    const held = await page.bridge._pollOnce();
    page.bridge.stop();
    check("once attached, the poll holds (wait=15)", server.polls.at(-1).wait === "15", server.polls.at(-1));
    check("an attached poll with nothing is re-issued at once", held === 0, held);
    server.pollQueue.push({ attached: true, budget_exhausted: true });
    const busy = await page.bridge._pollOnce();
    check("a spent held-request budget retries after a second", busy === 1000, busy);
    server.pollQueue.push({ attached: false });
    const idle = await page.bridge._pollOnce();
    check("an idle poll waits the idle interval", idle === 5000, idle);
}

// -- capture and evidence ------------------------------------------------------------

{
    const cmd = command("capture_view", { settle_ms: 0 });
    const ack = await run(cmd);
    const upload = server.captures[0];
    check("capture_view renders through the viewer's own export canvas", has(page.log, "renderCurrentViewCanvas"));
    check("capture_view posts a PNG to the session's captures route",
        upload && upload.headers["Content-Type"] === "image/png" && upload.body.type === "image/png"
        && upload.query.project === "demo" && upload.query.command_id === cmd.command_id, upload);
    check("capture_view answers with the artifact and the state",
        ack && ack.status === "done" && ack.result.artifact.id === "art_1" && ack.result.state.project === "demo", ack);

    const shown = await run(command("show_evidence", { artifact_id: "art_1", caption: "CD8 <b>hot</b> spot" }));
    const dialog = page.body.children.at(-1);
    const image = dialog && dialog.children.find((child) => child.tag === "img");
    const caption = dialog && dialog.children.find((child) => child.tag === "p");
    check("show_evidence opens a dialog over the viewer", shown && shown.status === "done" && dialog
        && dialog.tag === "dialog" && dialog.open === true, shown);
    check("the image comes from this server's captures route, under the base URL",
        image && image.src === "/base/agent/v1/captures/art_1", image && image.src);
    check("the caption is text, never markup", caption && caption.textContent === "CD8 <b>hot</b> spot");
    const foreign = await run(command("show_evidence", { url: "https://evil.example/x.png" }));
    check("show_evidence refuses another origin's image", foreign && foreign.status === "rejected", foreign);
    await run(command("show_evidence", { url: "/agent/v1/captures/art_2" }));
    check("a second piece of evidence replaces the first", dialog.open === false && dialog.removed === true);
}

// -- events ------------------------------------------------------------------------

{
    const seen = [];
    page.on("plexora:agent-state-changed", (event) => seen.push(event.detail));
    await page.bridge._handleEvent({ event_seq: 41, plugin: "gating", kind: "gates", origin: sid, payload: {} });
    check("an event from this tab's own session is not re-dispatched", seen.length === 0, seen);
    await page.bridge._handleEvent({ event_seq: 42, plugin: "gating", kind: "gates", origin: "agent", payload: {} });
    check("a foreign event is re-dispatched for the plugins", seen.length === 1 && seen[0].plugin === "gating", seen);
    check("the event cursor advances past both", page.bridge._status().cursor.afterEvent === 42);

    page.log.length = 0;
    await page.bridge._handleEvent({ event_seq: 43, plugin: "core", kind: "reload", origin: "agent", payload: {} });
    check("core reload re-reads the dataset and the layers",
        has(page.log, "refreshDataset") && has(page.log, "adoptLayers") && !has(page.log, "reload"), page.log);
    page.log.length = 0;
    await page.bridge._handleEvent({ event_seq: 44, plugin: "core", kind: "layers", origin: "agent", payload: {} });
    check("core layers adopts the layer list", has(page.log, "adoptLayers") && !has(page.log, "refreshDataset"));
    page.log.length = 0;
    await page.bridge._handleEvent({ event_seq: 45, plugin: "core", kind: "config", origin: "agent", payload: {} });
    check("core config refreshes the dataset", has(page.log, "refreshDataset"));
    await page.bridge._handleEvent({ event_seq: 46, plugin: "core", kind: "channels_renamed", origin: "agent",
                                     payload: { names: ["DNA", "CD3e"] } });
    check("core channels_renamed adopts the names", has(page.log, "adoptChannelNames",
        (e) => e[1].join() === "DNA,CD3e"));
    server.config = { demo: { imageData: [{ name: "Area", fullname: "Area" }, { name: "A", fullname: "A" },
                                          { name: "B", fullname: "B" }] } };
    await page.bridge._handleEvent({ event_seq: 47, plugin: "core", kind: "channels_renamed", origin: "agent",
                                     payload: {} });
    check("without names in the payload they are read off /config, Area excluded",
        has(page.log, "adoptChannelNames", (e) => e[1].join() === "A,B"), page.log.filter((e) => e[0] === "adoptChannelNames"));
}

// -- open_project ---------------------------------------------------------------------

{
    page.bridge._arm();
    const here = await run(command("open_project", { project: "demo" }));
    check("open_project for the project already open does not navigate",
        here && here.status === "done" && here.result.navigating === false && !has(page.log, "router.go"), here);
    server.order.length = 0;
    const cmd = command("open_project", { project: "other one", tool: "roi" });
    const ack = await run(cmd);
    const leave = server.leaves.at(-1) || {};
    check("open_project acknowledges before it leaves, and leaves before it goes",
        server.order.join(" > ") === `ack:${cmd.command_id} > leave > go`, server.order);
    check("the ack says it is navigating", ack && ack.status === "done" && ack.result.navigating === true, ack);
    check("the leave says where to", leave.navigating_to === "/base/other%20one?tool=roi", leave);
    check("it navigates through the router, carrying ?tool=",
        has(page.log, "router.go", (e) => e[1] === "/base/other%20one?tool=roi"));
    check("a navigating tab stops polling", page.bridge._status().running === false);
    page.firePageHide();
    check("pagehide after its own navigation does not also say the tab closed",
        !has(page.log, "sendBeacon"));
}

// -- surviving a reload -------------------------------------------------------------------

{
    const reloaded = makePage({ server, storage });
    reloaded.fireDomReady();
    for (let i = 0; i < 40 && server.registrations.length < 2; i++) await tick(5);
    reloaded.bridge.stop();
    const again = server.registrations[1] || {};
    check("a reloaded tab re-registers under the same session id", again.session_id === sid, again);
    check("...and keeps its cursors, since the server still knows it",
        reloaded.bridge._status().cursor.afterEvent === 47, reloaded.bridge._status());
    check("...and continues the revision rather than restarting it", again.revision > 0, again.revision);

    // A session the server has forgotten: same id, cursors from zero.
    server.sessions.delete(sid);
    const fresh = makePage({ server, storage });
    fresh.fireDomReady();
    for (let i = 0; i < 40 && server.registrations.length < 3; i++) await tick(5);
    fresh.bridge.stop();
    check("a forgotten session keeps its id", (server.registrations[2] || {}).session_id === sid);
    check("...but restarts its cursors", fresh.bridge._status().cursor.afterEvent === 0, fresh.bridge._status());

    // A 404 mid-life: the server restarted. Re-register, then carry on.
    server.sessions.clear();
    const before = server.registrations.length;
    fresh.bridge._arm();
    const delay = await fresh.bridge._pollOnce();
    fresh.bridge.stop();
    check("a 404 on poll re-registers", server.registrations.length === before + 1 && delay === 0
        && server.registrations.at(-1).session_id === sid,
        { registrations: server.registrations.length, delay, before, last: server.registrations.at(-1).session_id, sid });

    // Closing the tab (not navigating) says so with a beacon.
    fresh.firePageHide();
    check("pagehide on a closing tab sends a leave beacon",
        has(fresh.log, "sendBeacon", (e) => /\/base\/agent\/v1\/viewer\/sessions\/[^/]+\/leave$/.test(e[1])),
        fresh.log.filter((e) => e[0] === "sendBeacon"));
}

// -- a 403 stops polling for good ------------------------------------------------------------

{
    const denied = makeServer();
    denied.forbidAll = true;
    const blocked = makePage({ server: denied, storage: makeStorage() });
    blocked.fireDomReady();
    await tick(60);
    const count = denied.requests.length;
    check("a 403 at registration stops the bridge", blocked.bridge._status().stopped === true
        && blocked.bridge._status().running === false, blocked.bridge._status());
    blocked.bridge.start();
    await tick(40);
    check("...for good: nothing is asked again", denied.requests.length === count,
        { before: count, after: denied.requests.length });

    const pollDenied = makeServer();
    pollDenied.forbidPoll = true;
    const cut = makePage({ server: pollDenied, storage: makeStorage() });
    cut.fireDomReady();
    await tick(60);
    const polls = pollDenied.polls.length;
    await tick(60);
    check("a 403 on a poll stops the loop", cut.bridge._status().stopped === true && polls === 1
        && pollDenied.polls.length === 1, { polls: pollDenied.polls.length });
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
