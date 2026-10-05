/**
 * Runs the real services/orbDriver.js and views/agentPanel.js in one `vm`
 * context against a stand-in viewer page: a node stand-in with classList and
 * dataset, a canvas whose 2D context records what is drawn, a captured rAF, a
 * `matchMedia` toggle, a fetch spy, and stubs for the bridge
 * (`sessionId`, `restore`, `showEvidence`) and the requirements modal
 * (`collect`). The orb engine is a stub module handed to
 * `PlexoraOrb.configure({load})` -- a module script cannot run under vm --
 * whose STATE_TO_MODE is read out of the vendored orbs.js, so the panel's orb
 * states are checked against the engine that ships.
 *
 *     node tests/js/agent_panel_probe.mjs
 *
 * Prints one `PASS <name>` / `FAIL <name>` line per check;
 * tests/test_agent_client_probes.py lists the names.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ORB = join(REPO, "plexora/client/src/js/services/orbDriver.js");
const PANEL = join(REPO, "plexora/client/src/js/views/agentPanel.js");
const ENGINE = join(REPO, "plexora/client/external/thinking-orbs-0.3.2/orbs.js");

const failures = [];
function check(name, condition, detail) {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}`);
    if (!condition) {
        failures.push(name);
        if (detail !== undefined) console.log("   ", JSON.stringify(detail));
    }
}
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

// The vendored engine's state -> mode table, read from its source.
const STATE_TO_MODE = (() => {
    const block = readFileSync(ENGINE, "utf8").match(/const STATE_TO_MODE = \{([^}]*)\}/);
    const table = {};
    for (const [, key, value] of (block ? block[1] : "").matchAll(/(\w+):\s*"(\w+)"/g)) table[key] = value;
    return table;
})();

// -- a small DOM ----------------------------------------------------------------

function makeDom() {
    const byId = new Map();
    function node(tag) {
        const n = {
            tag, children: [], parentNode: null, style: {}, dataset: {}, attributes: {},
            listeners: {}, hidden: false, disabled: false, title: "", type: "", src: "", alt: "",
            _text: "", className: "",
            get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
            set textContent(value) { this._text = String(value); this.children = []; },
            get classList() {
                const self = this;
                const list = () => self.className.split(/\s+/).filter(Boolean);
                const api = {
                    contains: (name) => list().includes(name),
                    add: (...names) => { self.className = [...new Set([...list(), ...names])].join(" "); },
                    remove: (...names) => { self.className = list().filter((c) => !names.includes(c)).join(" "); },
                    toggle: (name, force) => {
                        const on = force === undefined ? !list().includes(name) : Boolean(force);
                        if (on) api.add(name); else api.remove(name);
                        return on;
                    },
                };
                return api;
            },
            appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
            append(...children) { children.forEach((child) => this.appendChild(child)); },
            remove() {
                if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
                this.parentNode = null;
                this.removed = true;
            },
            setAttribute(name, value) { this.attributes[name] = String(value); },
            getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
            addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
            click() { (this.listeners.click || []).forEach((fn) => fn({ type: "click" })); },
        };
        if (tag === "canvas") {
            n.width = 0;
            n.height = 0;
            n.getContext = () => (n._ctx = n._ctx || { canvas: n, setTransform() {}, clearRect() {} });
        }
        return n;
    }
    const body = node("body");
    const documentListeners = {};
    const document = {
        body,
        documentElement: node("html"),
        visibilityState: "visible",
        createElement: node,
        getElementById: (id) => byId.get(id) || null,
        addEventListener(type, fn) { (documentListeners[type] = documentListeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            documentListeners[type] = (documentListeners[type] || []).filter((f) => f !== fn);
        },
    };
    return { node, body, document, byId, documentListeners };
}

const find = (root, predicate) => {
    if (!root) return null;
    if (predicate(root)) return root;
    for (const child of root.children || []) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
};
const byClass = (root, name) => find(root, (n) => n.classList && n.classList.contains(name));
const byAction = (root, action) => find(root, (n) => n.dataset && n.dataset.action === action);

// -- the page -------------------------------------------------------------------

function makePage({ wrapper = true, dock = true, reduced = false, requirements = null, bridgeSession = "view_1",
                   typing = false, paid = false, layers = null } = {}) {
    const dom = makeDom();
    const byIdNode = (tag, id, parent, className = "") => {
        const n = dom.node(tag);
        n.id = id;
        n.className = className;
        dom.byId.set(id, n);
        parent.appendChild(n);
        return n;
    };
    // index.html's shell: the sidebar (its footer holding the AI dock) and
    // the viewer; the two buttons toggle `sidebar-collapsed` as
    // viewerSidebar.js does.
    const shell = byIdNode("div", "bodyDiv", dom.body, "viewer-shell");
    const sidebar = byIdNode("aside", "viewer_sidebar", shell, "viewer-sidebar");
    const sidebarClicks = [];
    const collapseButton = byIdNode("button", "sidebar_collapse_button", sidebar);
    collapseButton.addEventListener("click", () => { sidebarClicks.push("collapse"); shell.classList.toggle("sidebar-collapsed"); });
    let dockNode = null;
    if (dock) {
        const footer = dom.node("div");
        footer.className = "sidebar-footer";
        sidebar.appendChild(footer);
        dockNode = byIdNode("div", "plexora_ai_dock", footer, "plx-agent-dock");
        dockNode.hidden = true;
    }
    let wrapperNode = null;
    if (wrapper) wrapperNode = byIdNode("div", "openseadragon_wrapper", shell);
    const expandButton = byIdNode("button", "sidebar_expand_button", wrapperNode || shell);
    expandButton.addEventListener("click", () => { sidebarClicks.push("expand"); shell.classList.toggle("sidebar-collapsed"); });
    // The sidebar header's Plexora AI sparkle (index.html), hidden until the panel shows it.
    const sparkNode = dom.node("button");
    sparkNode.id = "plexora_ai_button";
    sparkNode.hidden = true;
    dom.byId.set("plexora_ai_button", sparkNode);
    dom.body.appendChild(sparkNode);
    const paints = [];
    const rafQueue = [];
    let rafRequests = 0;
    const fetches = [];
    const restores = [];
    const enlarged = [];
    const collected = [];
    const toasts = [];
    const explained = [];
    const choices = [];
    const licensed = [];
    const chats = [];
    const opened = [];
    const media = { reduced };
    const fakeEngine = {
        S: STATE_TO_MODE,
        M: new Proxy({}, { get: (_, mode) => (size, t) => ({ mode, size, t, dots: [], lines: [] }) }),
        r: (state, size) => ({ mode: STATE_TO_MODE[state], speed: 1, opts: {} }),
        p: (ctx, frame) => paints.push({ canvas: ctx.canvas, mode: frame.mode, size: frame.size, t: frame.t }),
    };
    const g = {
        console, Math, Number, JSON, Date, Promise, Object, Array, String, Boolean, Error,
        Map, Set, Infinity, setTimeout, clearTimeout, encodeURIComponent,
        document: dom.document,
        devicePixelRatio: 3,
        performance: { now: () => 1000 },
        flaskVariables: { datasource: "demo" },
        plexoraUrl: (path) => "/base/" + String(path).replace(/^\/+/, ""),
        getComputedStyle: () => ({ getPropertyValue: (name) => (name === "--accent-channel" ? " #4cc2ff " : "") }),
        matchMedia: (query) => ({ matches: /reduce/.test(query) && media.reduced }),
        requestAnimationFrame(fn) { rafRequests += 1; rafQueue.push(fn); return rafQueue.length; },
        cancelAnimationFrame() {},
        fetch: async (input, init = {}) => {
            fetches.push({ url: String(input), method: (init.method || "GET").toUpperCase(),
                           body: init.body ? JSON.parse(init.body) : null });
            if (page.onFetch) {
                const answer = page.onFetch(String(input), init);
                if (answer) return answer;
            }
            if (/\/requirements$/.test(String(input))) {
                return { ok: true, status: 200, json: async () => page.requirementsPayload };
            }
            if (page.failControl) return { ok: false, status: 409, json: async () => ({}) };
            return { ok: true, status: 200, json: async () => ({ success: true }) };
        },
        PlexoraAgentBridge: {
            sessionId: () => bridgeSession,
            restore: async (options) => { restores.push(options); return { had_lease: restores.length === 1 }; },
            showEvidence: (args) => { enlarged.push(args); return { shown: true }; },
        },
        PlexoraRequirements: requirements === false ? undefined
            : { collect: async (ds, form) => { collected.push({ ds, form }); return true; } },
        PlexoraToast: { show: (options) => toasts.push(options) },
        PlexoraPaid: { allows: (entitlement) => paid && (entitlement === "ai" || entitlement.startsWith("ai:")),
                       explain: (args) => { explained.push(args); return Promise.resolve(null); },
                       goToLicense: () => { licensed.push(true); } },
        PlexoraConfirm: { choose: (args) => { choices.push(args); return Promise.resolve(page.confirmAnswer); } },
        PlexoraChatPanel: { open: () => { chats.push(true); } },
        open: (target) => { opened.push(target); },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
    };
    const listeners = {};
    // The live layer stack (main.js `__plexora.layers`), when a check gives one.
    if (layers) g.__plexora = { layers: { layers: () => layers } };
    g.window = g;
    g.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
    g.dispatchEvent = (event) => { (listeners[event.type] || []).forEach((fn) => fn(event)); return true; };
    const ctx = createContext(g);
    runInContext(readFileSync(ORB, "utf8"), ctx, { filename: ORB });
    g.PlexoraOrb.configure({ load: async () => fakeEngine });
    runInContext(readFileSync(PANEL, "utf8"), ctx, { filename: PANEL });
    // Lines are read whole below; check 14 turns typing back on.
    g.PlexoraAgentPanel.configure({ typing });
    const page = {
        g, dom, wrapper: wrapperNode, dock: dockNode, shell, sidebarClicks, paints, rafQueue, fetches, restores, enlarged, collected, toasts, media,
        explained, opened, choices, licensed, chats, confirmAnswer: null, onFetch: null,
        requirementsPayload: { success: true, missing: [], confirm: [], optional: [] },
        failControl: false,
        panel: g.PlexoraAgentPanel,
        rafRequests: () => rafRequests,
        /** Run one frame of every loop that asked for one. */
        frame() { const due = rafQueue.splice(0); due.forEach((fn) => fn()); },
        send(event, payload = {}, sid = "gs_1") {
            g.dispatchEvent(new g.CustomEvent("plexora:agent-state-changed", { detail: {
                plugin: "gating", kind: "gating.session",
                payload: Object.assign({ session_id: sid, event,
                                         control: { url: `plugins/gating/agent_session/${sid}/control`,
                                                    actions: ["pause", "resume", "stop", "take_over"] } },
                                       payload),
            } }));
        },
        root: () => byClass(page.dom.body, "plx-agent-panel"),
        bar: () => byClass(page.dom.body, "plx-agent-bar"),
        chip: () => byClass(page.dom.body, "plx-agent-chip"),
        lastMode(canvas) {
            const mine = paints.filter((p) => !canvas || p.canvas === canvas);
            return mine.length ? mine.at(-1).mode : null;
        },
    };
    return page;
}

// -- 1. the vocabulary ------------------------------------------------------------

{
    const page = makePage();
    const phases = page.panel.PHASES;
    const names = ["planning", "analyzing", "inspecting", "thinking", "validating", "waiting", "summarizing"];
    check("every phase has a label and an orb state the vendored engine draws",
        JSON.stringify(Object.keys(phases)) === JSON.stringify(names)
        && names.every((n) => phases[n].label && STATE_TO_MODE[phases[n].orb])
        && Object.keys(STATE_TO_MODE).length === 9,
        { phases, STATE_TO_MODE });
}

// -- 2-10: one session, start to finish ------------------------------------------------

const page = makePage();
const P = page.panel;

page.send("started", { phase: "planning", progress: { units_done: 0, units_total: 9 },
                       order: ["CD45"], images: ["demo"], mode: "apply", view_id: "view_1" });
await tick(5);
{
    const root = page.root();
    const orbCanvas = byClass(root, "plx-agent-orb");
    check("started mounts the panel in the sidebar dock (the viewer wrapper, then body, without one), active, with an orb",
        root && root.parentNode === page.dock && page.dock.hidden === false && root.classList.contains("is-docked")
        && root.classList.contains("is-active")
        && root.getAttribute("role") === "region" && byClass(root, "plx-agent-live").getAttribute("aria-live") === "polite"
        && P.isAttached() === true && orbCanvas.width === 64 && orbCanvas.style.width === "28px" && orbCanvas.getAttribute("data-orb") === "live"
        && page.lastMode(orbCanvas) === STATE_TO_MODE.weaving,
        { root: Boolean(root), width: orbCanvas && orbCanvas.width, orb: orbCanvas && orbCanvas.attributes,
          mode: page.lastMode() });
}

page.send("issued", { packet_id: "p1", kind: "t1_strip", marker: "CD45", markers: ["CD45"], subject: "CD45",
                      project: "demo", phase: "inspecting", progress: { units_done: 3, units_total: 9 } });
page.frame();
{
    const root = page.root();
    const orbCanvas = byClass(root, "plx-agent-orb");
    const head = byClass(root, "plx-agent-phase").textContent;
    check("issued says \"AI agent inspecting · CD45\", draws searching and shows the progress",
        head === "AI agent inspecting · CD45" && root.dataset.phase === "inspecting"
        && page.lastMode(orbCanvas) === STATE_TO_MODE.searching
        && byClass(root, "plx-agent-progress").textContent === "3 of 9 markers",
        { head, mode: page.lastMode(orbCanvas), progress: byClass(root, "plx-agent-progress").textContent });
}

page.send("phase", { phase: "thinking" });
page.frame();
check("a phase event moves the orb to thinking's state (breathing)",
    page.lastMode(byClass(page.root(), "plx-agent-orb")) === STATE_TO_MODE.breathing
    && byClass(page.root(), "plx-agent-phase-name").textContent === "AI agent thinking");

{
    const said = "I'm evaluating CD45 expression across the tissue and at its current threshold.";
    page.send("issued", { packet_id: "p2", kind: "t2_confirm", marker: "CD45", markers: ["CD45"],
                          subject: "CD45", project: "demo", phase: "thinking", narration: said,
                          question: "CD45 (demo): do the cells above the gate carry real membrane staining?" });
    for (let i = 0; i < 40; i += 1) page.frame();
    const line = byClass(page.root(), "plx-agent-narration");
    const text = line.textContent;
    check("issued shows the narration for the user, never the agent's question",
        !line.hidden && text === said && !page.root().textContent.includes("carry real membrane"),
        { hidden: line.hidden, text });
}

{
    page.send("limit_reached", { marker: "CD57", project: "demo", why: "budget",
                                 words: "its allowance of looks for this marker", phase: "waiting" });
    for (let i = 0; i < 40; i += 1) page.frame();
    const card = byClass(page.root(), "plx-agent-limit");
    const shown = !card.hidden && byClass(card, "plx-agent-limit-text").textContent.includes("CD57");
    page.fetches.length = 0;
    byClass(card, "plx-button-primary").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const posted = page.fetches.at(-1) || {};
    check("a marker at its limit asks in the panel; Keep going posts the answer and closes it",
        shown && posted.method === "POST" && posted.body && posted.body.action === "limit"
        && posted.body.marker === "CD57" && posted.body.decision === "continue" && card.hidden
        && byClass(page.root(), "plx-agent-phase-name").textContent === "AI agent waiting for you",
        { shown, posted, hidden: card.hidden,
          head: byClass(page.root(), "plx-agent-phase-name").textContent });
    page.send("limit_reached", { marker: "CD16", project: "demo", why: "rounds", words: "its rounds" });
    page.frame();
    const reopened = !card.hidden;
    page.send("limit_answered", { answers: { CD16: "stop" }, by: "agent" });
    page.frame();
    check("an answer given elsewhere (the agent, another tab) closes the question here",
        reopened && card.hidden, { reopened, hidden: card.hidden });
    page.send("phase", { phase: "thinking" });
    page.frame();
}

{
    const fed = P.showEvidence({ src: "/base/agent/v1/captures/art_9", caption: "CD45 <b>strip</b>",
                                 title: "CD45", subject: "CD45", kind: "t1_strip" });
    const figure = byClass(page.root(), "plx-agent-evidence");
    const thumb = byClass(figure, "plx-agent-thumb");
    const caption = byClass(figure, "plx-agent-caption");
    byClass(figure, "plx-agent-thumb-button").click();
    check("evidence feeds the thumbnail, the caption is text, and the thumb enlarges through the bridge",
        fed === true && figure.hidden === false && thumb.src === "/base/agent/v1/captures/art_9"
        && caption.textContent === "CD45 <b>strip</b>" && caption.children.length === 0
        && page.enlarged.length === 1 && page.enlarged[0].src === "/base/agent/v1/captures/art_9"
        && byClass(figure, "plx-agent-thumb-button").title === "Enlarge",
        { enlarged: page.enlarged, caption: caption.textContent });
}

{
    page.send("issued", { packet_id: "p3", kind: "artifact_confirm", subject: "c7 · excessive background · CD3",
                          phase: "inspecting", narration: "I'm checking a suspected excessive background in CD3.",
                          evidence: [{ artifact_id: "bad id/..", caption: "never shown" },
                                     { artifact_id: "art_c7", caption: "excessive background candidate, look 1",
                                       title: "the suspected region at three scales", width: 900, height: 300 },
                                     { artifact_id: "art_c7b", caption: "second" }] });
    for (let i = 0; i < 40; i += 1) page.frame();
    const figure = byClass(page.root(), "plx-agent-evidence");
    const thumb = byClass(figure, "plx-agent-thumb");
    const caption = byClass(figure, "plx-agent-caption").textContent;
    const subject = byClass(page.root(), "plx-agent-subject").textContent;
    page.send("answered", { packet_id: "p3", kind: "artifact_confirm", outcome_state: "confirmed",
                            narration: "c7 confirmed: excessive background, warn.", phase: "analyzing" });
    for (let i = 0; i < 40; i += 1) page.frame();
    const said = byClass(page.root(), "plx-agent-narration").textContent;
    check("issued evidence shows the packet's image from the captures route, and answered narrates the outcome",
        !figure.hidden && thumb.src === "/base/agent/v1/captures/art_c7"
        && caption === "excessive background candidate, look 1 (+1 more)"
        && subject === " · c7 · excessive background · CD3"
        && said === "c7 confirmed: excessive background, warn.",
        { src: thumb.src, caption, subject, said });
}

page.send("unit_closed", { marker: "CD45", project: "demo", state: "accepted", confidence: "moderate",
                           low: 812.5, reason: "clear bimodal split" });
{
    const progress = byClass(page.root(), "plx-agent-progress");
    const first = progress.textContent;
    const why = progress.title;
    page.send("answered", { packet_id: "p1", kind: "t1_strip", marker: "CD45", outcome_state: "accepted",
                            phase: "analyzing", progress: { units_done: 4, units_total: 9 } });
    page.send("unit_closed", { marker: "CD8", project: "demo", state: "no_positive_population" });
    check("unit_closed reads as a few words: \"4 of 9 markers · CD45 accepted, moderate\"",
        first === "4 of 9 markers · CD45 accepted, moderate" && why === "clear bimodal split"
        && /CD8 no positive cells/.test(progress.textContent),
        { first, now: progress.textContent });
}

{
    const root = page.root();
    const pause = byAction(root, "pause");
    page.fetches.length = 0;
    pause.click();
    await tick(5);
    page.frame();
    const posted = page.fetches.at(-1) || {};
    const pausedOk = posted.method === "POST" && posted.url === "/base/plugins/gating/agent_session/gs_1/control"
        && posted.body.action === "pause" && root.classList.contains("is-paused")
        && byClass(root, "plx-agent-phase-name").textContent === "AI agent paused"
        && pause.textContent === "Resume agent" && page.rafQueue.length === 0;
    page.send("control", { paused: false, paused_by: null });
    await tick(5);
    const resumed = !root.classList.contains("is-paused") && pause.textContent === "Pause agent"
        && page.rafQueue.length === 1;
    page.failControl = true;
    pause.click();
    await tick(5);
    const reverted = pause.textContent === "Pause agent" && page.toasts.length === 1;
    page.failControl = false;
    check("Pause posts {action:\"pause\"} and flips the label, the button and the orb; a control event resumes",
        pausedOk && resumed && reverted, { posted, pausedOk, resumed, reverted, raf: page.rafQueue.length });
}

{
    const root = page.root();
    const toggle = byClass(root, "plx-agent-toggle");
    const head = byClass(root, "plx-agent-phase").textContent;
    toggle.click();
    const bar = page.bar();
    const words = byClass(bar, "plx-agent-bar-text").textContent;
    const collapsed = root.hidden === true && bar.hidden === false && bar.parentNode === page.dock
        && /keeps working/.test(toggle.title) && toggle.getAttribute("aria-expanded") === "true"
        && words === head && /^AI agent /.test(words)
        && byClass(bar, "plx-agent-bar-watch").hidden === true      // attached: nothing to re-attach
        && page.chip().hidden === true                               // the sidebar is open
        && P.current().collapsed === true && P.current().attached === true;
    await tick(5);
    const barOrb = byClass(bar, "plx-agent-orb");
    const barSized = barOrb.width === 40;
    const barToggle = byClass(bar, "plx-agent-toggle");
    barToggle.click();
    check("the chevron minimizes to a bar in the dock that still names the phase, and the bar's chevron opens it again",
        collapsed && barSized && barToggle.getAttribute("aria-expanded") === "false"
        && root.hidden === false && bar.hidden === true && P.current().collapsed === false,
        { collapsed, barSized, width: barOrb.width, words, head });
}

{
    const root = page.root();
    page.fetches.length = 0;
    byAction(root, "stop").click();
    await tick(5);
    const posted = page.fetches.at(-1) || {};
    check("Stop posts {action:\"stop\"} and gives the viewer back at once",
        posted.body && posted.body.action === "stop" && page.restores.length === 1
        && page.restores[0].reason === "stopped" && byAction(root, "stop").disabled === true,
        { posted, restores: page.restores });
}

{
    const root = page.root();
    byClass(root, "plx-agent-toggle").click();        // minimized when it ends
    page.send("finished", { reason: "stopped", state: "stopped", summary: {
        units_total: 9, units_done: 9, accepted: 6, accepted_low_confidence: 2, review: 2, empty: 1,
        failed: 0, skipped: 0, written: 7, proposed: 0, questions: 0, by_state: {} } });
    const summary = byClass(root, "plx-agent-summary");
    const visibleButtons = root.children.find((c) => c.classList.contains("plx-agent-actions"))
        .children.filter((b) => !b.hidden).map((b) => b.textContent);
    const text = summary.textContent;
    const done = root.dataset.phase === "done" && !root.classList.contains("is-active") && summary.hidden === false
        && text === "9 markers · 6 accepted (2 low confidence) · 2 need review · 1 no positive cells"
            + " · 7 gates written · Stopped by you"
        && JSON.stringify(visibleButtons) === JSON.stringify(["Close"])
        && root.hidden === false && page.chip().hidden === true
        && byClass(root, "plx-agent-hint").hidden === false && P.isAttached() === false;
    await tick(5);
    const restoresAfterFirst = page.restores.length;
    const restoredWith = page.restores.at(-1);
    page.send("finished", { reason: "closed", summary: { units_total: 1 } });
    await tick(5);
    const idempotent = summary.textContent === text && page.restores.length === restoresAfterFirst;
    page.send("report", { paths: ["/tmp/report.html"] });
    const report = byClass(root, "plx-agent-report");
    const reported = report.hidden === false && report.textContent === "Report written"
        && report.title === "/tmp/report.html";
    byAction(root, "close").click();
    const closed = page.root() === null && page.chip() === null && page.bar() === null
        && page.dock.hidden === true && P.current() === null;
    check("finished: a summary, only Close, a second finished ignored, report appended, a collapsed panel reopened",
        done && idempotent && reported && closed && restoresAfterFirst === 2
        && restoredWith.reason === "stopped",
        { text, visibleButtons, done, idempotent, reported, closed, restores: page.restores });
}

// -- 11. the setup question ------------------------------------------------------------

{
    const needs = { tool: "gating", project: "demo", keys: ["features"],
                    requirements: [{ key: "features", kind: "features", label: "Expression values",
                                     optional: true, role: "features" }] };
    page.requirementsPayload = { success: true, missing: [], confirm: [], optional: [], columns: ["a"] };
    page.send("needs_setup", { phase: "planning", view_id: "view_1", needs }, "gs_2");
    await tick(5);
    const forced = page.collected.at(-1);
    const forcedOk = forced && forced.ds === "demo"
        && forced.form.confirm.length === 1 && forced.form.confirm[0].key === "features"
        && forced.form.confirm[0].optional === false && forced.form.columns[0] === "a"
        && page.fetches.some((f) => f.url === "/base/demo/tools/gating/requirements");
    const listed = { success: true, missing: [], confirm: [{ key: "features", optional: false, label: "x" }],
                     optional: [] };
    page.requirementsPayload = listed;
    page.send("needs_setup", { phase: "planning", view_id: "view_1", needs }, "gs_2");
    await tick(5);
    const asIs = page.collected.at(-1);
    const asIsOk = page.collected.length === 2 && asIs.form.confirm[0].label === "x";
    page.send("needs_setup", { phase: "planning", view_id: "view_other", needs }, "gs_2");
    await tick(5);
    const planning = byClass(page.root(), "plx-agent-phase-name").textContent === "AI agent planning";
    check("needs_setup opens the requirements form with features to confirm (both payload shapes); another tab's is ignored",
        forcedOk && asIsOk && page.collected.length === 2 && planning,
        { forced, asIs, count: page.collected.length });
}

// -- 12. reduced motion ---------------------------------------------------------------

{
    const still = makePage({ reduced: true });
    still.send("started", { phase: "planning", progress: { units_done: 0, units_total: 2 } });
    await tick(5);
    const orbCanvas = byClass(still.root(), "plx-agent-orb");
    const afterStart = still.paints.filter((p) => p.canvas === orbCanvas).length;
    still.send("issued", { marker: "CD3", subject: "CD3", phase: "inspecting" });
    still.send("answered", { phase: "inspecting", progress: { units_done: 1, units_total: 2 } });
    still.send("phase", { phase: "thinking" });
    const modes = still.paints.filter((p) => p.canvas === orbCanvas).map((p) => p.mode);
    check("reduced motion paints one still frame per state and never asks for a frame",
        afterStart === 1 && still.rafRequests() === 0
        && JSON.stringify(modes) === JSON.stringify([STATE_TO_MODE.weaving, STATE_TO_MODE.searching,
                                                     STATE_TO_MODE.breathing])
        && still.paints.every((p) => p.t === 0.6),
        { afterStart, raf: still.rafRequests(), modes });
}

// -- 13. replacing, and no wrapper ---------------------------------------------------------

{
    const corner = makePage({ dock: false });
    corner.send("started", { phase: "planning" }, "gs_w");
    const inWrapper = corner.root() && corner.root().parentNode === corner.wrapper
        && !corner.root().classList.contains("is-docked");
    const bare = makePage({ wrapper: false, dock: false });
    bare.send("issued", { marker: "CD3", subject: "CD3", phase: "inspecting" }, "gs_a");  // a reloaded tab
    const first = bare.root();
    const attachedLate = first && first.parentNode === bare.dom.body;
    bare.send("finished", { reason: "closed", summary: {} }, "gs_unknown");
    const ignoredUnknown = bare.root() === first && bare.panel.current().id === "gs_a";
    bare.send("started", { phase: "planning" }, "gs_b");
    const panels = bare.dom.body.children.filter((c) => c.classList.contains("plx-agent-panel"));
    check("a new started replaces the panel; with no dock it mounts under the viewer wrapper, and with neither on body",
        inWrapper && attachedLate && ignoredUnknown && panels.length === 1 && panels[0] !== first && first.removed === true
        && bare.panel.current().id === "gs_b",
        { attachedLate, ignoredUnknown, panels: panels.length });
}

// -- 14. typing -----------------------------------------------------------------------

{
    const typed = makePage({ typing: true });
    typed.send("started", { phase: "planning", progress: { units_done: 2, units_total: 9 } });
    await tick(80);
    typed.send("issued", { marker: "CD45", subject: "CD45", phase: "thinking",
                           progress: { units_done: 2, units_total: 9 } });
    const root = typed.root();
    const name = byClass(root, "plx-agent-phase-name");
    const live = byClass(root, "plx-agent-live");
    const early = name.textContent;
    const typingNow = name.classList.contains("is-typing");
    const spokenWhole = /^AI agent thinking · CD45\. /.test(live.textContent);
    await tick(40);
    const middle = name.textContent;
    await tick(1600);
    const done = name.textContent === "AI agent thinking" && !name.classList.contains("is-typing")
        && byClass(root, "plx-agent-subject").textContent === " · CD45";
    const progress = byClass(root, "plx-agent-progress");
    const before = progress.textContent;
    typed.send("unit_closed", { marker: "CD45", state: "accepted", confidence: "moderate" });
    const kept = progress.textContent;
    await tick(1600);
    check("text is typed in a token at a time, keeps a shared prefix, and the live region gets whole lines",
        early.length < "AI agent thinking".length && typingNow && spokenWhole && middle.length > early.length
        && done && before === "2 of 9 markers" && kept.length < before.length
        && progress.textContent === "3 of 9 markers · CD45 accepted, moderate",
        { early, middle, spoken: live.textContent, before, kept, after: progress.textContent });
}

// -- 15. QC's by_type progress, and the bulk pass's own stage -------------------------

{
    const qc = makePage();
    qc.send("started", { phase: "planning", job_id: "job_abc", labels: { unit_noun: "channel" },
                         progress: { units_done: 0, units_total: 89,
                                     by_type: { channel: { done: 0, total: 40 } },
                                     bulk: { job_id: "job_abc", state: "bulk_running" } } });
    await tick(5);
    const beforeStage = byClass(qc.root(), "plx-agent-progress").textContent;
    qc.send("phase", { phase: "analyzing",
                       progress: { units_done: 0, units_total: 89,
                                   by_type: { channel: { done: 0, total: 40 } },
                                   bulk: { job_id: "job_abc", state: "bulk_running",
                                          stage: "scanning", message: "scanned CD3",
                                          done: 120, total: 482 } } });
    const duringStage = byClass(qc.root(), "plx-agent-progress").textContent;
    qc.send("phase", { phase: "analyzing",
                       progress: { units_done: 2, units_total: 89,
                                   by_type: { channel: { done: 2, total: 40 },
                                             check: { done: 1, total: 6 } },
                                   bulk: { job_id: "job_abc", state: "deciding" } } });
    const afterStage = byClass(qc.root(), "plx-agent-progress").textContent;
    check("by_type.channel drives \"N of M channels\" (not every unit); a running bulk pass "
        + "shows its own stage, and check counts appear once it hands off",
        beforeStage === "0 of 40 channels"
        && duringStage === "Scanning channels · scanned CD3 (120/482)"
        && afterStage === "2 of 40 channels · checks 1/6"
        && qc.panel.current().job === "job_abc"
        && qc.panel.current().bulk.state === "deciding",
        { beforeStage, duringStage, afterStage, current: qc.panel.current() });
}

// -- Plexora AI: the launcher, the usage line, the credit card -------------------------

const json = (status, body) => ({ ok: status < 400, status, json: async () => body });
const BALANCE = { success: true, available_micro: 5_000_000, available_credits: 500, estimates: {
    gating: { units: 5, unit: "marker", credits: 125, affordable: true },
    qc: { units: 40, unit: "channel", credits: 480, affordable: true } } };
const sparkOf = (page) => page.dom.byId.get("plexora_ai_button");
const cornerChipOf = (page) => find(page.dom.body, (n) => n.classList && n.classList.contains("plx-ai-launch"));

{
    const free = makePage();
    const spark = sparkOf(free);
    const visible = spark.hidden === false && !cornerChipOf(free);
    spark.click();
    await tick(5);
    const notice = free.choices[0];
    const told = Boolean(notice) && /under development and needs a licence/.test(notice.body.join(" "))
        && notice.choices.map((c) => c.label).join("|") === "Close|Enter License…"
        && free.panel.launcher() === null && free.licensed.length === 0;
    free.confirmAnswer = "license";
    spark.click();
    await tick(5);
    const toLicense = free.licensed.length === 1 && free.panel.launcher() === null;
    check("Plexora AI on Free: the header AI button is always shown; clicked, it says Plexora AI is under "
        + "development and needs a licence, offers Close and Enter License (no trial), and Enter License goes there",
        visible && told && toLicense, { visible, notice, licensed: free.licensed });
    const noChip = !cornerChipOf(free);
    await free.panel.openLauncher();
    const shown = free.panel.launcher();
    const asked = free.fetches.filter((f) => f.url.includes("ai/v1/")).length;
    byAction(free.dom.body, "ai-explain").click();
    check("Plexora AI on Free: opened anyway, both buttons are disabled with the "
        + "reason, nothing is asked of the gateway, and About Plexora AI explains",
        noChip && shown && shown.buttons.gating.disabled && shown.buttons.qc.disabled
        && /Paid licence that includes AI/.test(shown.buttons.gating.note) && asked === 0
        && free.explained.length === 1 && free.explained[0].entitlement === "ai",
        { noChip, shown, asked, explained: free.explained });
}

{
    const ai = makePage({ paid: true });
    ai.onFetch = (input, init) => {
        if (input.includes("ai/v1/balance")) return json(200, BALANCE);
        if (input.endsWith("ai/v1/runs") && init.method === "POST") {
            return json(201, { success: true, run_id: "air_1", job_id: "job_1" });
        }
        return null;
    };
    const chip = sparkOf(ai);
    const chipShown = Boolean(chip && !chip.hidden) && !cornerChipOf(ai);
    chip.click();
    await tick(5);
    const shown = ai.panel.launcher();
    const expanded = chip.getAttribute("aria-expanded") === "true";
    chip.click();
    const toggledShut = ai.panel.launcher() === null && chip.getAttribute("aria-expanded") === "false";
    chip.click();
    await tick(5);
    const balance = ai.fetches.find((f) => f.url.includes("ai/v1/balance"));
    byAction(ai.dom.body, "ai-gating").click();
    await tick(5);
    const asked = ai.panel.launcher()?.context;
    const notYet = !ai.fetches.some((f) => f.url.endsWith("ai/v1/runs") && f.method === "POST");
    byAction(ai.dom.body, "ai-context-start").click();
    await tick(5);
    const posted = ai.fetches.find((f) => f.url.endsWith("ai/v1/runs") && f.method === "POST");
    const closed = ai.panel.launcher() === null;
    ai.send("started", { phase: "planning", progress: { units_done: 0, units_total: 5 } }, "gs_ai");
    const chipHidden = Boolean(chip && chip.hidden === false && expanded && toggledShut);
    chip.click();
    await tick(5);
    byAction(ai.dom.body, "ai-chat").click();
    check("Plexora AI: the launcher's Chat with Plexora AI closes it and opens the chat panel",
        ai.chats.length === 1 && ai.panel.launcher() === null, { chats: ai.chats.length });
    check("Plexora AI: the header sparkle opens the launcher and shuts it again; each button carries the "
        + "estimate before a start (\"~125 credits · 5 markers\"); Gate posts /ai/v1/runs for the open "
        + "project, mirrored into this tab; the sparkle stays while the session runs, and there is no corner chip",
        chipShown && shown && !shown.buttons.gating.disabled && !shown.buttons.qc.disabled
        && shown.buttons.gating.note === "~125 credits · 5 markers"
        && shown.buttons.qc.note === "~480 credits · 40 channels"
        && shown.buttons.gating.label === "Gate with Plexora AI" && shown.buttons.qc.label === "QC with Plexora AI"
        && shown.balance === "500 credits available"
        && Boolean(balance) && /project=demo/.test(balance.url)
        && Boolean(posted) && posted.body.kind === "gating" && posted.body.project === "demo"
        && posted.body.start_options && posted.body.start_options.mirror === true
        && posted.body.start_options.view_id === "view_1" && closed && chipHidden
        && Boolean(asked) && asked.kind === "gating" && notYet && !("context" in posted.body),
        { chipShown, shown, balance: balance && balance.url, posted, closed, chipHidden, asked, notYet });

    {
        // A reload: the run is still going but says nothing for a while, so
        // the card is put back from the run's snapshot, not the next event.
        const page = makePage({ paid: true });
        page.onFetch = (input) => {
            if (/ai\/v1\/runs\?limit=/.test(input)) {
                return json(200, { success: true, runs: [
                    { run_id: "air_other", project: "elsewhere", status: "running", session_id: "gs_x", kind: "gating" },
                    { run_id: "air_7", project: "demo", status: "running", session_id: "gs_7", kind: "gating" }] });
            }
            if (input.endsWith("ai/v1/runs/air_7")) {
                return json(200, { success: true, run_id: "air_7", status: "running", session: {
                    session_id: "gs_7", control: { url: "plugins/gating/agent_session/gs_7/control" },
                    phase: "validating", progress: { units_done: 3, units_total: 9 }, paused: true, stopped: false } });
            }
            return null;
        };
        page.panel.configure({ typing: false });
        const back = await page.panel._reattach();
        const card = page.dock.children.find((c) => c.classList.contains("plx-agent-panel"));
        const line = card && byClass(card, "plx-agent-progress").textContent;
        const shown = page.panel.current();
        check("Plexora AI: after a reload, a run still going on this project has its card put back at once "
            + "(phase, \"3 of 9 markers\", paused) from GET /ai/v1/runs, not from the next event",
            back === "gs_7" && Boolean(card) && shown.id === "gs_7" && shown.paused && shown.phase === "validating"
            && line === "3 of 9 markers" && byAction(card, "resume") !== null,
            { back, line, shown });
    }

    {
        // Start puts the corner card up at once, before any session event;
        // the first event adopts that card rather than building another.
        const page = makePage({ paid: true });
        page.onFetch = (input, init) => {
            if (input.includes("ai/v1/balance")) return json(200, BALANCE);
            if (input.endsWith("ai/v1/runs") && init.method === "POST") {
                return json(201, { success: true, run_id: "air_9", job_id: "job_9" });
            }
            return null;
        };
        page.panel.configure({ typing: false });
        await page.panel.openLauncher();
        byAction(page.dom.body, "ai-gating").click();
        await tick(5);
        byClass(page.dom.body, "plx-ai-context-input").value = "melanoma";
        byAction(page.dom.body, "ai-context-start").click();
        await tick(5);
        const cards = () => page.dom.body.children.concat(page.wrapper ? page.wrapper.children : [],
                                                          page.dock ? page.dock.children : [])
            .filter((c) => c.classList && c.classList.contains("plx-agent-panel"));
        const early = cards()[0];
        const head = early && byClass(early, "plx-agent-phase-name").textContent;
        const line = early && byClass(early, "plx-agent-progress").textContent;
        const held = early && byAction(early, "pause").disabled && byAction(early, "stop").disabled;
        page.send("started", { phase: "planning", progress: { units_done: 0, units_total: 9 } }, "gs_9");
        const after = cards();
        check("Plexora AI: Start gating puts the card up at once (\"AI agent starting\", \"Reading your note\", "
            + "Pause and Stop held), and the first event takes that same card over",
            Boolean(early) && head === "AI agent starting" && line === "Reading your note" && held
            && after.length === 1 && after[0] === early && page.panel.current().id === "gs_9"
            && !byAction(early, "stop").disabled,
            { head, line, held, cards: after.length });
    }

    {
        // Gate asks for an optional note first, in the same card; what is
        // typed is sent as written, Back returns to the tools, Enter starts.
        const page = makePage({ paid: true });
        page.onFetch = (input, init) => {
            if (input.includes("ai/v1/balance")) return json(200, BALANCE);
            if (input.endsWith("ai/v1/runs") && init.method === "POST") {
                return json(201, { success: true, run_id: "air_2", job_id: "job_2" });
            }
            return null;
        };
        await page.panel.openLauncher();
        byAction(page.dom.body, "ai-gating").click();
        const step = page.panel.launcher().context;
        const input = byClass(page.dom.body, "plx-ai-context-input");
        const shape = Boolean(step) && step.toolsHidden && step.start === "Start gating" && !step.disabled
            && step.note === "~125 credits · 5 markers" && /melanoma skin sample/.test(step.placeholder)
            && /only gate CD3 and CD8/.test(step.help) && page.panel.launcher().title === "AI Gating"
            && Boolean(byClass(page.dom.body, "plx-ai-context-label"))
            && /optional/.test(byClass(page.dom.body, "plx-ai-context-label").textContent);
        byAction(page.dom.body, "ai-context-back").click();
        const back = page.panel.launcher().context === null && page.panel.launcher().title === "Plexora AI"
            && page.panel.launcher().modalities.includes("multiplex") && !byClass(page.dom.body, "plx-ai-context");
        byAction(page.dom.body, "ai-gating").click();
        const again = byClass(page.dom.body, "plx-ai-context-input");
        again.value = "  this is melnoma skn sample gate imune and tumor cells ";
        let prevented = false;
        (again.listeners.keydown || []).forEach((fn) => fn({ key: "Enter", shiftKey: false,
                                                            preventDefault: () => { prevented = true; } }));
        await tick(5);
        const sent = page.fetches.find((f) => f.url.endsWith("ai/v1/runs") && f.method === "POST");
        const newline = { key: "Enter", shiftKey: true };
        check("Plexora AI: Gate opens an optional context step in the launcher (label, field with an example, "
            + "a muted scope line, Back, Start gating with the estimate); Back returns to the tools; Enter "
            + "starts and the note is sent as typed, trimmed",
            shape && back && prevented && Boolean(sent)
            && sent.body.context === "this is melnoma skn sample gate imune and tumor cells"
            && sent.body.kind === "gating" && page.panel.launcher() === null && Boolean(input) && Boolean(newline),
            { step, back, prevented, sent: sent && sent.body });

        page.send("started", { phase: "planning", progress: { units_done: 0, units_total: 9 } }, "gs_ctx");
        page.send("ai_context", { units: 9, interpretation: {
            original_text: "this is melnoma skn sample gate imune and tumor cells",
            normalized_text: "This is a melanoma skin tissue sample.",
            context: { tissue: "skin", disease: "melanoma" },
            gating_scope: { mode: "all_markers", requested_markers: [] }, ambiguities: [] } }, "gs_ctx");
        await tick(5);
        const line = byClass(page.dom.body, "plx-agent-context");
        const first = line ? line.textContent : "";
        page.send("ai_context", { units: 9, interpretation: {
            original_text: "only gate CD3 and CD8", context: {},
            gating_scope: { mode: "selected_markers", requested_markers: ["CD3", "CD8"] },
            ambiguities: ["'SOX10' (requested) is not a marker of this panel"] } }, "gs_ctx");
        await tick(5);
        const line2 = byClass(page.dom.body, "plx-agent-context");
        check("Plexora AI: the running panel says how the note was read (\"Context: melanoma · skin · all 9 "
            + "markers\"), a restriction names its markers, and the note as typed and what was unclear are its title",
            Boolean(line) && first === "Context: melanoma · skin · all 9 markers"
            && line2.textContent === "Context: only CD3, CD8 · see note"
            && /You wrote: only gate CD3 and CD8/.test(line2.title) && /Unclear: 'SOX10'/.test(line2.title),
            { first, line2: line2 && line2.textContent, title: line2 && line2.title });
    }

    {
        // A centred modal on the page, closed by Escape, by its backdrop, and by the sparkle.
        const modal = makePage({ paid: true });
        modal.onFetch = (input) => (input.includes("ai/v1/balance") ? json(200, BALANCE) : null);
        sparkOf(modal).click();
        await tick(5);
        const centred = modal.panel.launcher()?.modal === true && Boolean(byClass(modal.dom.body, "plx-ai-backdrop"));
        (modal.dom.documentListeners.keydown || []).forEach((fn) => fn({ key: "Escape" }));
        const escaped = modal.panel.launcher() === null && !byClass(modal.dom.body, "plx-ai-backdrop");
        sparkOf(modal).click();
        await tick(5);
        byClass(modal.dom.body, "plx-ai-backdrop").click();
        const backdropShut = modal.panel.launcher() === null && (modal.dom.documentListeners.keydown || []).length === 0;
        check("Plexora AI: the launcher is a centred modal on a backdrop; Escape and the backdrop close it, "
            + "and its key listener goes with it",
            centred && escaped && backdropShut, { centred, escaped, backdropShut });
    }

    {
        // By data modality: what the open project holds decides the sections.
        const sectionsOf = (page) => {
            const out = [];
            const walk = (n) => {
                if (n.classList && n.classList.contains("plx-ai-modality")) out.push(n);
                (n.children || []).forEach(walk);
            };
            walk(page.dom.body);
            return out;
        };
        const mixed = makePage({ paid: true, layers: [{ id: "__image__", spec: { modality: "multiplex" } },
            { id: "tx_1", spec: { modality: "transcripts" } }] });
        mixed.onFetch = (input) => (input.includes("ai/v1/balance") ? json(200, BALANCE) : null);
        await mixed.panel.openLauncher();
        const both = mixed.panel.launcher();
        const mixedSections = sectionsOf(mixed).map((n) => n.dataset.modality);

        const he = makePage({ paid: true, layers: [{ id: "__image__", spec: { modality: "he" } }] });
        he.onFetch = (input) => (input.includes("ai/v1/balance") ? json(200, BALANCE) : null);
        await he.panel.openLauncher();
        const heOnly = he.panel.launcher();
        const heAsked = he.fetches.filter((f) => f.url.includes("ai/v1/balance")).length;
        check("Plexora AI: the launcher shows the detected modality's tools only -- a multiplexed image with "
            + "transcripts shows Multiplexed imaging (gating, QC) and no section for data with no tool; an "
            + "H&E-only project shows no tool, says so, and asks the gateway for no estimate",
            JSON.stringify(mixedSections) === JSON.stringify(["multiplex"]) && !both.empty
            && JSON.stringify(Object.keys(both.buttons).sort()) === JSON.stringify(["gating", "qc"])
            && JSON.stringify(heOnly.modalities) === JSON.stringify([]) && heOnly.empty
            && Object.keys(heOnly.buttons).length === 0 && heAsked === 0,
            { mixedSections, buttons: Object.keys(both.buttons), he: heOnly, heAsked });
    }

    const poor = makePage({ paid: true });
    poor.onFetch = (input) => (input.includes("ai/v1/balance") ? json(200, Object.assign({}, BALANCE, {
        available_credits: 2000.4,
        estimates: { gating: { units: 5, unit: "marker", credits: 125, affordable: false },
                     qc: { units: 0, unit: "channel", credits: 0, unavailable: "this sample has no image to check" } },
    })) : null);
    await poor.panel.openLauncher();
    const p2 = poor.panel.launcher();
    check("Plexora AI: a run the balance cannot pay for is disabled and says what there is; a tool the "
        + "project's data cannot run is left out; the balance reads \"2,000 credits available\"",
        p2.buttons.gating.disabled && p2.buttons.gating.note === "~125 credits · 5 markers · you have 2,000"
        && !("qc" in p2.buttons) && p2.balance === "2,000 credits available" && !p2.empty,
        p2);
}

{
    const ai = makePage({ paid: true });
    ai.onFetch = (input, init) => (input.endsWith("ai/v1/runs") && init.method === "POST"
        ? json(201, { success: true, run_id: "air_2", job_id: "job_2" }) : null);
    const sid = "qs_ai";
    ai.send("started", { phase: "planning", progress: { units_done: 0, units_total: 9 } }, sid);
    ai.send("ai_run", { run_id: "air_1", kind: "qc", quote_credits: 480 }, sid);
    const quoted = byClass(ai.root(), "plx-agent-usage").textContent;
    ai.send("ai_usage", { run_id: "air_1", usage: { packets: 12, charged_credits: 3.44, cache_read_share: 0.874 } }, sid);
    const usage = byClass(ai.root(), "plx-agent-usage");
    const line = usage.textContent;
    ai.send("ai_paused", { run_id: "air_1", reason: "insufficient_credits", message: "top up",
                           top_up_url: "https://license.example/top-up",
                           usage: { packets: 13, charged_credits: 3.6, cache_read_share: 0.88 },
                           resume: { kind: "qc", project: "demo", resume_session: sid } }, sid);
    const card = byClass(ai.root(), "plx-agent-credit");
    const shownCard = card.hidden === false;
    const text = byClass(card, "plx-agent-limit-text").textContent;
    const other = byAction(card, "ai-top-up");
    if (other) other.click();
    byAction(card, "ai-resume").click();
    await tick(5);
    const resumed = ai.fetches.find((f) => f.url.endsWith("ai/v1/runs") && f.method === "POST");
    const cardAfter = card.hidden;
    check("Plexora AI: ai_usage draws \"Plexora AI · 12 packets · 3.4 credits · 87% from cache\"; a credit "
        + "pause shows the two-button card (Resume, Add credits opens the top-up page); Resume posts the "
        + "session to resume and the card closes",
        quoted === "Plexora AI · at most 480 credits" && !usage.hidden && shownCard
        && line === "Plexora AI · 12 packets · 3.4 credits · 87% from cache"
        && /credits ran out/.test(text) && Boolean(other) && other.textContent === "Add credits"
        && ai.opened[0] === "https://license.example/top-up"
        && Boolean(resumed) && resumed.body.resume_session === sid && resumed.body.kind === "qc" && cardAfter === true,
        { quoted, line, text, opened: ai.opened, resumed, cardAfter });

    const gw = makePage({ paid: true });
    gw.send("started", { phase: "planning" }, "gs_gw");
    gw.send("ai_finished", { status: "failed", reason: "bad_request",
                             resume: { kind: "gating", project: "demo", resume_session: "gs_gw" } }, "gs_gw");
    const gwCard = byClass(gw.root(), "plx-agent-credit");
    const gwShown = gwCard.hidden === false;
    const gwText = byClass(gwCard, "plx-agent-limit-text").textContent;
    byAction(gwCard, "ai-later").click();
    check("Plexora AI: a gateway error shows the same card with Resume and Not now, which dismisses it",
        gwShown && /could not continue: bad_request/.test(gwText) && gwCard.hidden === true,
        { gwShown, gwText, hidden: gwCard.hidden });
}

// -- the viewer apart from the agent: background, watch, take over ---------------------

async function running(options = {}, marker = "CD3") {
    const pg = makePage(options);
    pg.send("started", { phase: "planning", progress: { units_done: 0, units_total: 3 }, view_id: "view_1" });
    pg.send("issued", { packet_id: "p1", kind: "t1_strip", marker, markers: [marker], subject: marker,
                        phase: "inspecting" });
    await tick(5);
    return pg;
}

{
    const pg = await running();
    const root = pg.root();
    const attachedAtStart = pg.panel.current().attached === true;
    const button = byAction(root, "detach");
    const label = button && button.textContent;
    pg.fetches.length = 0;
    button.click();
    const restoredEarly = pg.restores.length;          // not before the server has answered
    const minimized = root.hidden === true && pg.bar().hidden === false;
    await tick(5);
    const posted = pg.fetches.at(-1) || {};
    const watch = byClass(pg.bar(), "plx-agent-bar-watch");
    const detached = attachedAtStart && label === "Continue in background" && restoredEarly === 0 && minimized
        && posted.body && posted.body.action === "detach_viewer" && pg.restores.length === 1
        && pg.restores[0].reason === "detached" && pg.panel.current().attached === false
        && pg.panel.current().paused === false && watch.hidden === false && watch.textContent === "Watch in viewer"
        && byAction(root, "attach") === button && button.textContent === "Watch in viewer"
        && root.classList.contains("is-detached");

    const refused = await running();
    refused.failControl = true;
    byAction(refused.root(), "detach").click();
    await tick(5);
    const reverted = refused.panel.current().attached === true && refused.root().hidden === false
        && byAction(refused.root(), "detach") !== null && refused.restores.length === 0 && refused.toasts.length === 1;
    check("Continue in background posts {action:\"detach_viewer\"}, gives the viewer back after the post, "
        + "minimizes to the bar, and the bar offers Watch in viewer; a refused detach puts the toggle back",
        detached && reverted, { detached, reverted, posted, restores: pg.restores });

    // Watch in viewer, from the bar.
    pg.fetches.length = 0;
    watch.click();
    await tick(5);
    const attach = pg.fetches.at(-1) || {};
    const watching = attach.body && attach.body.action === "attach_viewer" && attach.body.view_id === "view_1"
        && pg.panel.current().attached === true && watch.hidden === true && pg.panel.current().collapsed === true
        && byAction(root, "detach") === button && pg.restores.length === 1;
    // Detached again, expanding the card does not attach it.
    byClass(pg.bar(), "plx-agent-toggle").click();
    byAction(root, "detach").click();
    await tick(5);
    pg.fetches.length = 0;
    byClass(pg.bar(), "plx-agent-toggle").click();
    await tick(5);
    const expandedOnly = root.hidden === false && pg.panel.current().attached === false
        && !pg.fetches.some((f) => f.body && f.body.action === "attach_viewer");
    const noBridge = await running({ bridgeSession: null });
    noBridge.panel.current();
    noBridge.send("control", { paused: false, viewer_attached: false, view_id: "view_1" });
    noBridge.fetches.length = 0;
    byAction(noBridge.root(), "attach").click();
    await tick(5);
    const cannot = noBridge.fetches.length === 0 && noBridge.toasts.length === 1;
    check("Watch in viewer posts {action:\"attach_viewer\"} with this tab's view id, the bar's button goes, "
        + "and expanding the bar does not re-attach",
        watching && expandedOnly && cannot, { watching, expandedOnly, cannot, attach });
}

{
    const pg = await running({}, "CD8");
    const root = pg.root();
    const visible = root.children.find((c) => c.classList.contains("plx-agent-actions"))
        .children.filter((b) => !b.hidden).map((b) => b.textContent);
    const row = JSON.stringify(visible) === JSON.stringify(["Continue in background", "Pause agent", "Stop agent"])
        && byAction(root, "take_over") === null;
    // Taken over in the plugin's pill: the server says so, and the card follows.
    pg.send("control", { paused: true, paused_by: "viewer", viewer_attached: false, view_id: "view_1",
                         taken_over: true });
    const taken = pg.panel.current().paused === true && pg.panel.current().attached === false
        && root.hidden === false && byClass(root, "plx-agent-phase-name").textContent === "AI agent paused"
        && byAction(root, "attach") !== null;
    pg.fetches.length = 0;
    byAction(root, "resume").click();
    await tick(5);
    const resumed = pg.fetches.at(-1) || {};
    pg.send("control", { paused: false, paused_by: null, viewer_attached: false, view_id: "view_1" });
    const background = resumed.body && resumed.body.action === "resume" && pg.panel.current().paused === false
        && pg.panel.current().attached === false && byAction(root, "attach") !== null && pg.restores.length === 0;
    check("the actions are one row (Continue in background, Pause, Stop) with no Take over of the panel's own; "
        + "a take-over from the plugin pauses and detaches the card, and Resume keeps the viewer detached",
        row && taken && background, { visible, row, taken, background, resumed });
}

{
    const pg = await running();
    const root = pg.root();
    pg.send("control", { paused: false, paused_by: null, viewer_attached: false, view_id: "view_1" });
    const off = pg.panel.current().attached === false && byAction(root, "attach") !== null;
    pg.send("control", { paused: false, paused_by: null, viewer_attached: true, view_id: "view_other" });
    const elsewhere = pg.panel.current().attached === false;
    pg.send("control", { paused: false, paused_by: null, viewer_attached: true, view_id: "view_1" });
    const here = pg.panel.current().attached === true && byAction(root, "detach") !== null;
    const other = makePage();
    other.send("started", { phase: "planning", view_id: "view_other", viewer_attached: true });
    const notMine = other.panel.current().attached === false;
    // A run this tab first hears mid-way (an MCP session started with
    // mirror=false, say) is not assumed to be mirrored into it.
    const late = makePage();
    late.send("issued", { phase: "analyzing", subject: "CD3" });
    const lateCard = late.root();
    const lateOff = late.panel.current().attached === false && byAction(lateCard, "attach") !== null
        && byAction(lateCard, "detach") === null;

    const reload = makePage({ paid: true });
    reload.onFetch = (input) => {
        if (/ai\/v1\/runs\?limit=/.test(input)) {
            return json(200, { success: true, runs: [
                { run_id: "air_8", project: "demo", status: "running", session_id: "gs_8", kind: "gating" }] });
        }
        if (input.endsWith("ai/v1/runs/air_8")) {
            return json(200, { success: true, run_id: "air_8", status: "running", session: {
                session_id: "gs_8", control: { url: "plugins/gating/agent_session/gs_8/control" },
                phase: "analyzing", progress: { units_done: 1, units_total: 9 }, paused: false,
                paused_by: null, stopped: false, viewer_attached: false, view_id: "view_1" } });
        }
        return null;
    };
    reload.panel.configure({ typing: false });
    await reload.panel._reattach();
    const card = reload.root();
    const restored = reload.panel.current() && reload.panel.current().attached === false
        && byAction(card, "attach") !== null;
    check("a control event's viewer_attached drives the toggle (another tab's view id does not attach this one); "
        + "the reload snapshot restores a detached run; a run first heard mid-way is not assumed attached",
        off && elsewhere && here && notMine && restored && lateOff,
        { off, elsewhere, here, notMine, restored, lateOff });
}

{
    const pg = makePage();
    pg.shell.classList.add("sidebar-collapsed");
    pg.send("started", { phase: "planning", progress: { units_done: 0, units_total: 3 }, view_id: "view_1" });
    await tick(5);
    const chip = pg.chip();
    const shown = chip.hidden === false && chip.parentNode === pg.wrapper && /working here/.test(chip.title)
        && /^Agent · /.test(byClass(chip, "plx-agent-chip-text").textContent);
    await tick(5);
    const sized = byClass(chip, "plx-agent-orb").width === 40;
    byClass(pg.root(), "plx-agent-toggle").click();           // minimized, then opened from the chip
    chip.click();
    const opened = !pg.shell.classList.contains("sidebar-collapsed") && pg.sidebarClicks.includes("expand")
        && chip.hidden === true && pg.root().hidden === false && pg.panel.current().collapsed === false;
    const open = makePage();
    open.send("started", { phase: "planning", view_id: "view_1" });
    const none = open.chip().hidden === true;
    check("with the sidebar collapsed a live session shows the chip under the expand button; clicking it "
        + "opens the sidebar and the panel",
        shown && sized && opened && none, { shown, sized, opened, none });
}

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
