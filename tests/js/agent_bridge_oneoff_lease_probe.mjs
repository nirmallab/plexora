/**
 * Runs the real services/agentBridge.js against a stand-in viewer page and a
 * stand-in `/agent/v1` server, for the lease a ONE-OFF command takes.
 *
 *     node tests/js/agent_bridge_oneoff_lease_probe.mjs
 *
 * An external agent (Claude Code through the plexora MCP server) that calls
 * viewer_navigate or viewer_open_tool once sends a single mutating command
 * and never a `restore_viewer` -- that is only sent by the session mirrors and
 * the agent panel. The command leases the viewer, and the lease suspends the
 * sidebar's channel saves. What it holds: once the server has reported the
 * agent detached for longer than the attach TTL (agent/v1's ATTACH_TTL_S,
 * 120 s), the tab no longer holds the lease, and a channel edit the user then
 * makes is saved to the project as any edit is.
 *
 * The clock is a stand-in (Date, performance and long timers), so "longer
 * than the attach TTL" costs no wall-clock time. Prints PASS/FAIL per check,
 * and exits non-zero on any failure.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/agentBridge.js");
const CODE = readFileSync(SOURCE, "utf8");

const results = [];
function check(name, condition, detail) {
    results.push({ name, ok: Boolean(condition), detail });
}
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function respond(status, data) {
    return { status, ok: status >= 200 && status < 300, json: async () => data };
}

/** The `/agent/v1` wire, as the existing agent_bridge_probe stands it in. */
function makeServer() {
    const server = { sessions: new Map(), acks: [], polls: [], pollQueue: [], nextId: 1 };
    server.fetch = async (input, init = {}) => {
        const url = new URL(String(input), "http://host");
        const method = (init.method || "GET").toUpperCase();
        const query = Object.fromEntries(url.searchParams);
        if (url.pathname === "/base/config") return respond(200, {});
        const match = url.pathname.match(/^\/base\/agent\/v1\/viewer\/sessions(?:\/([^/]+)(?:\/(\w+))?)?$/);
        if (!match) return respond(404, { success: false });
        const [, sid, action] = match;
        if (!sid && method === "POST") {
            const body = JSON.parse(init.body);
            const id = body.session_id || `view_${server.nextId++}`;
            server.sessions.set(id, body);
            return respond(200, { success: true, session: { view_id: id, resumed: false },
                                  idle_poll_s: 5, held_poll_s: 15 });
        }
        if (sid && !action && method === "GET") {
            return server.sessions.has(sid) ? respond(200, { success: true }) : respond(404, {});
        }
        if (action === "commands") {
            server.polls.push(query);
            if (!server.sessions.has(sid)) return respond(404, { success: false });
            const next = server.pollQueue.shift() || { commands: [], events: [], attached: false };
            return respond(200, Object.assign({ success: true, held: false, budget_exhausted: false,
                                                commands: [], events: [] }, next));
        }
        if (action === "acks") {
            server.acks.push(JSON.parse(init.body));
            return respond(200, { success: true });
        }
        if (action === "leave") return respond(200, { success: true });
        return respond(404, {});
    };
    return server;
}

function makeStorage() {
    const data = new Map();
    return {
        getItem: (key) => (data.has(key) ? data.get(key) : null),
        setItem: (key, value) => { data.set(key, String(value)); },
        removeItem: (key) => { data.delete(key); },
    };
}

/** A clock the probe moves: Date, performance.now and any timer of a second
 *  or more run on it; shorter timers are real (the page's own small waits). */
function makeClock() {
    const RealDate = Date;
    const clock = { offset: 0, timers: [], nextTimer: 1 };
    class FakeDate extends RealDate {
        constructor(...args) {
            if (args.length) super(...args);
            else super(RealDate.now() + clock.offset);
        }
        static now() { return RealDate.now() + clock.offset; }
    }
    clock.Date = FakeDate;
    clock.performance = { now: () => performance.now() + clock.offset };
    clock.setTimeout = (fn, ms = 0, ...rest) => {
        if (Number(ms) < 1000) return setTimeout(fn, ms, ...rest);
        const id = { fake: clock.nextTimer++ };
        clock.timers.push({ id, due: FakeDate.now() + Number(ms), fn: () => fn(...rest) });
        return id;
    };
    clock.clearTimeout = (id) => {
        if (id && id.fake) clock.timers = clock.timers.filter((t) => t.id !== id);
        else clearTimeout(id);
    };
    clock.setInterval = (fn, ms) => {
        const id = { fake: clock.nextTimer++ };
        const arm = () => clock.timers.push({ id, due: FakeDate.now() + Number(ms),
                                              fn: () => { arm(); fn(); } });
        arm();
        return id;
    };
    clock.clearInterval = clock.clearTimeout;
    clock.advance = (ms) => {
        clock.offset += ms;
        const now = FakeDate.now();
        const due = clock.timers.filter((t) => t.due <= now);
        clock.timers = clock.timers.filter((t) => t.due > now);
        due.forEach((t) => t.fn());
    };
    return clock;
}

/** A ViewerSidebar-shaped stand-in whose suspend/resume is the real counter
 *  and whose scheduleSaveChannels is guarded the way viewerSidebar.js's is. */
function makeSidebar(log) {
    const slot = (index, name, enabled) => ({
        index, name, enabled, visible: true, colorHex: "#2388ff", range: [0, 255],
        userRangeChanged: false, autoLeveled: true, autoLeveling: false,
    });
    return {
        columns: ["DAPI", "CD3", "CD8"],
        channelSlots: [slot(0, "DAPI", true), slot(1, "CD3", true)],
        _persistenceSuspended: 0,
        saves: 0,
        suspendPersistence() { this._persistenceSuspended += 1; },
        resumePersistence() { this._persistenceSuspended = Math.max(0, this._persistenceSuspended - 1); },
        scheduleSaveChannels() {
            if (this._persistenceSuspended) { log.push(["save:suppressed"]); return; }
            this.saves += 1;
            log.push(["save"]);
        },
        setSlotRange(index, range, user) {
            this.channelSlots[index].range = range;
            this.channelSlots[index].userRangeChanged = Boolean(user);
        },
        snapshotSlots() {
            return this.channelSlots.map((s) => ({ name: s.name, colorHex: s.colorHex,
                                                   enabled: s.enabled, visible: true, range: s.range }));
        },
        snapshotChannelMemory() { return {}; },
        restoreChannelMemory() {},
        async applyLaunchChannels() {},
    };
}

function makePage(server, clock) {
    const log = [];
    const windowListeners = new Map();
    const documentListeners = new Map();
    const sidebar = makeSidebar(log);
    const toolLoader = {
        loaded: [],
        async openTool(name) { if (!this.loaded.includes(name)) this.loaded.push(name); return { loaded: true }; },
        closeTool(name) { log.push(["closeTool", name]); this.loaded = this.loaded.filter((n) => n !== name); },
        isToolOpen(name) { return this.loaded.includes(name); },
        loadedTools() { return [...this.loaded]; },
        activeTool() { return this.loaded[0] || null; },
    };
    const g = {
        console, Math, Number, JSON, Promise, Object, Array, String, Boolean, Error,
        Map, Set, URLSearchParams, URL, Blob,
        Date: clock.Date, performance: clock.performance,
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
        setInterval: clock.setInterval, clearInterval: clock.clearInterval,
        sessionStorage: makeStorage(),
        fetch: server.fetch,
        plexoraUrl: (path) => "/base/" + String(path).replace(/^\/+/, ""),
        flaskVariables: { datasource: "demo", notebook_mode: false },
        location: { href: "http://host/base/demo", reload() {} },
        navigator: { sendBeacon: () => true },
        __plexora: {
            viewerSidebar: sidebar,
            layers: { subscribe: () => () => {} },
            seaDragonViewer: {
                viewer: { addHandler() {}, world: { getItemCount: () => 0 } },
                config: {},
                viewTransform: { get: () => ({ degrees: 0, flipH: false, flipV: false }), subscribe() {} },
            },
            viewerControls: { mode: "none", async selectMode(mode) { this.mode = mode; } },
            plugins: new Map(),
        },
        __plexoraReady: Promise.resolve(),
        PlexoraViewerScene: {
            panTo: () => true,
            zoomTo: () => true,
            fitRegion: (v, region) => { log.push(["scene.fitRegion", region]); return true; },
            currentViewport: () => ({ x: 100, y: 200, w: 800, h: 600 }),
            scaleOf: () => 0.5,
            referenceItem: () => ({ source: { dimensions: { x: 4000, y: 3000 } } }),
            levelScale: () => 1,
        },
        PlexoraToolLoader: toolLoader,
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
        document: {
            readyState: "loading",
            title: "demo - Plexora",
            visibilityState: "visible",
            body: { appendChild() {} },
            createElement: () => ({ style: {}, appendChild() {}, addEventListener() {}, setAttribute() {} }),
            querySelectorAll: (selector) => (selector === "a[data-tool]"
                ? ["gating", "roi"].map((tool) => ({ dataset: { tool } })) : []),
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
    return { g, log, sidebar, bridge: g.PlexoraAgentBridge };
}

let counter = 0;
function command(type, args = {}) {
    counter += 1;
    return { command_id: `cmd_${counter}`, seq: counter, type, arguments: args, expected_revision: null };
}

/**
 * One agent, one look, then gone: the poll that carries the command says
 * attached; every poll after it, over ten minutes of the stand-in clock,
 * says not. Then the user edits a channel.
 */
async function oneLookThenGone(label, look) {
    const server = makeServer();
    const clock = makeClock();
    const page = makePage(server, clock);
    await page.bridge._register();
    page.bridge._arm();
    server.pollQueue.push({ commands: [look], attached: true });
    await page.bridge._pollOnce();
    const ack = server.acks.find((a) => a.command_id === look.command_id) || null;
    check(`${label}: the agent's one command is run and acknowledged done`,
        ack && ack.status === "done", ack);
    // The agent never sends restore_viewer. Ten minutes of polls, each one
    // the server's "nobody is attached", 5 s apart (the idle interval).
    for (let elapsed = 0; elapsed < 600000; elapsed += 5000) {
        clock.advance(5000);
        server.pollQueue.push({ commands: [], events: [], attached: false });
        await page.bridge._pollOnce();
        await tick(0);
    }
    await tick(20);
    check(`${label}: once the agent has been detached past the attach TTL, the tab holds no lease`,
        page.bridge.hasLease() === false, { hasLease: page.bridge.hasLease() });
    // The user drags a contrast slider: the sidebar's own setter, then the
    // save every channel edit schedules.
    page.sidebar.saves = 0;
    page.sidebar.setSlotRange(0, [5, 100], true);
    page.sidebar.scheduleSaveChannels();
    check(`${label}: a channel edit the user makes afterwards is saved to the project`,
        page.sidebar.saves === 1,
        { saves: page.sidebar.saves, suspended: page.sidebar._persistenceSuspended,
          log: page.log.slice(-3) });
    page.bridge.stop();
}

// viewer_navigate with bounds (agent/core/viewer.py navigate -> fit_region).
await oneLookThenGone("viewer_navigate", command("fit_region", { x: 5, y: 6, width: 70, height: 80 }));
// viewer_navigate with a cell id (-> focus_cell).
await oneLookThenGone("viewer_navigate to a cell",
    command("focus_cell", { cell_id: 7, x: 300, y: 400, width: 200 }));
// viewer_open_tool (agent/core/viewer.py open_tool; video_studio's capability too).
await oneLookThenGone("viewer_open_tool", command("open_tool", { tool: "roi" }));

for (const r of results) {
    console.log(`${r.ok ? "PASS" : "FAIL"} ${r.name}`);
    if (!r.ok) console.log(`    ${JSON.stringify(r.detail)}`);
}
process.exit(results.every((r) => r.ok) ? 0 : 1);
