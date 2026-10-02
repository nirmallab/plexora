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

function makePage({ wrapper = true, reduced = false, requirements = null, bridgeSession = "view_1",
                   typing = false, paid = false } = {}) {
    const dom = makeDom();
    let wrapperNode = null;
    if (wrapper) {
        wrapperNode = dom.node("div");
        wrapperNode.id = "openseadragon_wrapper";
        dom.byId.set("openseadragon_wrapper", wrapperNode);
        dom.body.appendChild(wrapperNode);
    }
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
                       explain: (args) => { explained.push(args); return Promise.resolve(null); } },
        open: (target) => { opened.push(target); },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init && init.detail; }
        },
    };
    const listeners = {};
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
        g, dom, wrapper: wrapperNode, paints, rafQueue, fetches, restores, enlarged, collected, toasts, media,
        explained, opened, onFetch: null,
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
    check("started mounts the panel under the viewer wrapper, active, with an orb",
        root && root.parentNode === page.wrapper && root.classList.contains("is-active")
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
    const hide = byClass(root, "plx-agent-hide");
    hide.click();
    const chip = page.chip();
    const collapsed = root.hidden === true && chip.hidden === false
        && /keeps working/.test(hide.title) && /keeps working/.test(chip.title)
        && /Agent · /.test(byClass(chip, "plx-agent-chip-text").textContent)
        && P.current().collapsed === true;
    await tick(5);
    const chipOrb = byClass(chip, "plx-agent-orb");
    const chipSized = chipOrb.width === 40;
    chip.click();
    check("Hide collapses to a chip that says the agent keeps working, and the chip opens it again",
        collapsed && chipSized && root.hidden === false && chip.hidden === true,
        { collapsed, chipSized, width: chipOrb.width });
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
    byClass(root, "plx-agent-hide").click();          // collapsed when it ends
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
    const closed = page.root() === null && page.chip() === null && P.current() === null;
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
    const bare = makePage({ wrapper: false });
    bare.send("issued", { marker: "CD3", subject: "CD3", phase: "inspecting" }, "gs_a");  // a reloaded tab
    const first = bare.root();
    const attachedLate = first && first.parentNode === bare.dom.body;
    bare.send("finished", { reason: "closed", summary: {} }, "gs_unknown");
    const ignoredUnknown = bare.root() === first && bare.panel.current().id === "gs_a";
    bare.send("started", { phase: "planning" }, "gs_b");
    const panels = bare.dom.body.children.filter((c) => c.classList.contains("plx-agent-panel"));
    check("a new started replaces the panel; with no viewer wrapper it mounts on body",
        attachedLate && ignoredUnknown && panels.length === 1 && panels[0] !== first && first.removed === true
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
                                             check: { done: 1, total: 6 },
                                             cells: { done: 0, total: 9 } },
                                   bulk: { job_id: "job_abc", state: "deciding" } } });
    const afterStage = byClass(qc.root(), "plx-agent-progress").textContent;
    check("by_type.channel drives \"N of M channels\" (not every unit); a running bulk pass "
        + "shows its own stage, and checks/cell counts appear once it hands off",
        beforeStage === "0 of 40 channels"
        && duringStage === "Scanning channels · scanned CD3 (120/482)"
        && afterStage === "2 of 40 channels · checks 1/6 · cell checks 0/9"
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
    const noChip = sparkOf(free).hidden === true && !cornerChipOf(free);
    await free.panel.openLauncher();
    const shown = free.panel.launcher();
    const asked = free.fetches.filter((f) => f.url.includes("ai/v1/")).length;
    byAction(free.dom.body, "ai-explain").click();
    check("Plexora AI on Free: the header sparkle stays hidden; opened anyway, both buttons are disabled with the "
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
    const posted = ai.fetches.find((f) => f.url.endsWith("ai/v1/runs") && f.method === "POST");
    const closed = ai.panel.launcher() === null;
    ai.send("started", { phase: "planning", progress: { units_done: 0, units_total: 5 } }, "gs_ai");
    const chipHidden = Boolean(chip && chip.hidden === false && expanded && toggledShut);
    check("Plexora AI: the header sparkle opens the launcher and shuts it again; each button carries the "
        + "estimate before a start (\"About 125 credits · 5 markers\"); Gate posts /ai/v1/runs for the open "
        + "project; the sparkle stays while the session runs, and there is no corner chip",
        chipShown && shown && !shown.buttons.gating.disabled && !shown.buttons.qc.disabled
        && shown.buttons.gating.note === "About 125 credits · 5 markers"
        && shown.buttons.qc.note === "About 480 credits · 40 channels"
        && shown.buttons.gating.label === "Gate with Plexora AI" && shown.buttons.qc.label === "QC with Plexora AI"
        && shown.balance === "500 credits available"
        && Boolean(balance) && /project=demo/.test(balance.url)
        && Boolean(posted) && posted.body.kind === "gating" && posted.body.project === "demo" && closed && chipHidden,
        { chipShown, shown, balance: balance && balance.url, posted, closed, chipHidden });

    const poor = makePage({ paid: true });
    poor.onFetch = (input) => (input.includes("ai/v1/balance") ? json(200, Object.assign({}, BALANCE, {
        available_credits: 100,
        estimates: { gating: { units: 5, unit: "marker", credits: 125, affordable: false },
                     qc: { units: 0, unit: "channel", credits: 0, unavailable: "this sample has no image to check" } },
    })) : null);
    await poor.panel.openLauncher();
    const p2 = poor.panel.launcher();
    check("Plexora AI: a run the balance cannot pay for, or a project it cannot run on, is disabled with why",
        p2.buttons.gating.disabled && /You have 100 credits/.test(p2.buttons.gating.note)
        && p2.buttons.qc.disabled && p2.buttons.qc.note === "this sample has no image to check",
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

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
