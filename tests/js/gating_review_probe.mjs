/**
 * Thresholding reviewing an automatic gate, client side: the channels the
 * gate was decided with switched on beside the marker
 * (gatingReviewChannels.js), the agent's notes over the viewer
 * (gatingReviewNotes.js), and the controller hooks between them
 * (gatingSidebarController.js: setGateMarker, syncReview, the provenance
 * line's AI Notes link and the "AI supporting channels" switch).
 *
 * What is worth pinning is what must never happen: a slot of the user's
 * written over or left changed, a supporting channel saved into the
 * project's channel list, a channel the user put away coming back, the notes
 * of one marker shown on another.
 *
 * The real classes and controller methods, against a sidebar that behaves
 * like core's in the ways that matter here: switching a slot on fetches its
 * channel once (later switches reuse it), a channel switched on in one slot
 * goes off in any other, colours only ever become the user's, auto-levelling
 * saves on its own after a delay, and saving is held while suspended.
 *
 * Run directly: `node tests/js/gating_review_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "gating", "static");

// -- a clock and a page ---------------------------------------------------------

const clock = { now: 0, timers: [], seq: 0 };
const settle = async () => {
    for (let i = 0; i < 25; i += 1) await Promise.resolve();
};
async function advance(ms) {
    const end = clock.now + ms;
    for (;;) {
        await settle();
        clock.timers.sort((a, b) => a.at - b.at || a.id - b.id);
        const next = clock.timers[0];
        if (!next || next.at > end) break;
        clock.timers.shift();
        clock.now = next.at;
        next.fn();
    }
    clock.now = end;
    await settle();
}

class FakeClassList {
    constructor(el) { this.el = el; }
    names() { return new Set(this.el.className.split(/\s+/).filter(Boolean)); }
    write(names) { this.el.className = [...names].join(" "); }
    add(...names) { const s = this.names(); names.forEach((n) => s.add(n)); this.write(s); }
    remove(...names) { const s = this.names(); names.forEach((n) => s.delete(n)); this.write(s); }
    toggle(name, force) {
        const s = this.names();
        const on = force === undefined ? !s.has(name) : Boolean(force);
        if (on) s.add(name); else s.delete(name);
        this.write(s);
        return on;
    }
    contains(name) { return this.names().has(name); }
}

class FakeElement {
    constructor(tag = "div") {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parent = null;
        this.className = "";
        this.classList = new FakeClassList(this);
        this.dataset = {};
        this.hidden = false;
        this.title = "";
        this.type = "";
        this.checked = false;
        this.offsetWidth = 0;
        this.attributes = {};
        this.listeners = {};
        this._text = "";
        const props = {};
        this.style = { setProperty: (k, v) => { props[k] = String(v); },
                       getPropertyValue: (k) => props[k] || "" };
    }
    get textContent() {
        return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
    }
    set textContent(value) { this.children = []; this._text = String(value); }
    // Markup is kept as its text: what a test reads is the words, and the
    // icon class (`innerHTML`).
    set innerHTML(value) {
        this.children = [];
        this.html = String(value);
        this._text = this.html.replace(/<[^>]*>/g, "");
    }
    get innerHTML() { return this.html || ""; }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k] ?? null; }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    append(...children) { children.forEach((c) => this.appendChild(c)); }
    replaceChildren(...children) { this.children = []; this._text = ""; this.append(...children); }
    get childElementCount() { return this.children.length; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type, extra = {}) {
        (this.listeners[type] || []).forEach((fn) => fn({ target: this, stopPropagation() {},
                                                          preventDefault() {}, ...extra }));
    }
    contains(node) {
        for (let at = node; at; at = at.parent) if (at === this) return true;
        return false;
    }
    *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
    querySelector(selector) {
        for (const node of this.walk()) {
            if (selector.startsWith(".") && node.classList.contains(selector.slice(1))) return node;
            if (selector === "[data-ai-notes]" && "aiNotes" in node.dataset) return node;
        }
        return null;
    }
    focus() { sandbox.document.activeElement = this; }
    remove() {
        if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
        this.parent = null;
    }
}

const windowListeners = {};
const elements = {};
const storage = {
    data: {}, broken: false,
    getItem(key) { if (this.broken) throw new Error("denied"); return key in this.data ? this.data[key] : null; },
    setItem(key, value) { if (this.broken) throw new Error("denied"); this.data[key] = String(value); },
};
let leased = false;
const sandbox = {
    console,
    Promise,
    setTimeout: (fn, ms) => {
        clock.seq += 1;
        clock.timers.push({ id: clock.seq, at: clock.now + Math.max(0, Number(ms) || 0), fn });
        return clock.seq;
    },
    clearTimeout: (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); },
    localStorage: storage,
    PlexoraAgentBridge: { hasLease: () => leased },
    document: {
        activeElement: null,
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => new FakeElement(tag),
        querySelector: (selector) => elements.gate_provenance?.querySelector(selector) || null,
        addEventListener() {},
        removeEventListener() {},
    },
    addEventListener: (type, fn) => (windowListeners[type] = windowListeners[type] || []).push(fn),
    removeEventListener: (type, fn) => {
        windowListeners[type] = (windowListeners[type] || []).filter((f) => f !== fn);
    },
};
sandbox.window = sandbox;
createContext(sandbox);
runInContext(readFileSync(join(STATIC, "gatingReviewChannels.js"), "utf8")
    + "\nglobalThis.GateReviewChannels = GateReviewChannels;", sandbox);
runInContext(readFileSync(join(STATIC, "gatingReviewNotes.js"), "utf8")
    + "\nglobalThis.GateReviewNotes = GateReviewNotes;", sandbox);
runInContext(readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;", sandbox);
const proto = sandbox.GatingSidebarController.prototype;

// -- the project ------------------------------------------------------------------

// Table columns are short names; the image's channels (and what the server's
// review names) are full ones.
const FULL = { DNA: "00_DNA", CD3: "03_CD3", CD4: "04_CD4", CD8: "08_CD8", CD20: "20_CD20",
               Ki67: "67_Ki67" };
const SHORT = Object.fromEntries(Object.entries(FULL).map(([s, f]) => [f, s]));
const IMAGE = new Set(Object.values(FULL));
const MARKERS = ["DNA", "CD3", "CD4", "CD8", "CD20", "Ki67", "FOXP3"];
const DATA = { getFullChannelName: (n) => FULL[n] || n, getShortChannelName: (n) => SHORT[n] || n };
const OVERLAP = "#22e6e6";
const EXCLUSION = "#ec4899";

const support = (marker, kind, relation, extra = {}) => ({
    marker, channel: FULL[marker] ?? null, kind, relation, roles: ["tile_expected"],
    display: true, looks: 1, influence: `${marker}: shown beside the cells in 1 look`, ...extra });

function provenance() {
    return {
        CD8: { method: "ai_panel", status: "accepted", confidence: "moderate", state: "accepted",
               session_id: "s1",
               review: { version: 1, marker: "CD8", needs_review: false, uncertainty: [],
                         notes: "Decided by 2 blind looks at numbered cells. Parent lineage CD3.",
                         supporting: [support("CD3", "parent_child", "subset"),
                                      support("CD4", "mutual_exclusion", "exclusive"),
                                      support("DNA", "coexpression", "coexpressed"),
                                      support("CD8", "parent_child", "subset")] } },
        CD4: { method: "needs_review", status: "accepted", confidence: "manual_review",
               state: "manual_review_recommended", session_id: "s1",
               review: { version: 1, marker: "CD4", needs_review: true,
                         uncertainty: ["the reads leave 6.4 (P 0.55) and 6.9 (P 0.35)"],
                         notes: "Written for review at the best gate reached (6.42).",
                         supporting: [support("CD3", "parent_child", "subset"),
                                      support("CD8", "mutual_exclusion", "exclusive")] } },
        Ki67: { method: "ai_accepted", status: "accepted", confidence: "high", state: "accepted",
                session_id: "s1",
                review: { version: 1, marker: "Ki67", needs_review: false, uncertainty: [],
                          notes: "Decided by 1 look at cells around the gate.",
                          supporting: [{ ...support("FOXP3", "coexpression", "coexpressed"),
                                         channel: "99_FOXP3" }] } },
        CD20: { method: "ai_accepted", status: "accepted", confidence: "moderate",
                state: "accepted", session_id: "s0", reason: "accepted at T2" },
        DNA: { method: "manual", status: "accepted" },
    };
}

const DEFAULTS = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#14b8a6", "#e11d48", "#64748b"];

function slot(index, name, extra = {}) {
    return { index, name, enabled: false, visible: true, colorHex: DEFAULTS[index],
             userColorChanged: false, range: [0, 255], userRangeChanged: false,
             autoLeveled: true, autoLeveling: false, rangeSource: null, curve: null, ...extra };
}

/** Core's sidebar, as far as the review touches it. */
function makeSidebar({ slots, max = 7 } = {}) {
    const sidebar = {
        channelSlots: slots || [
            slot(0, "DNA", { enabled: true }),
            slot(1, "", { visible: true }),
            slot(2, "CD20", { userColorChanged: true, colorHex: "#ff0000", range: [5, 200],
                              userRangeChanged: true, rangeSource: "user" }),
            slot(3, "", { visible: false }),
            slot(4, "", { visible: false }),
        ],
        maxChannelSlots: max,
        columns: Object.keys(FULL),
        cache: new Set(), fetches: [], activations: [], saves: 0, saved: [], suspended: 0,
        activate(s) {
            this.activations.push(s.name);
            if (!this.cache.has(s.name)) { this.cache.add(s.name); this.fetches.push(s.name); }
        },
        autoLevel(s) {
            if (s.autoLeveled || s.autoLeveling || s.userRangeChanged) return;
            sandbox.setTimeout(() => {
                s.autoLeveling = true;
                sandbox.setTimeout(() => {          // pass 1, silent when asked
                    if (s.autoSilent) s.autoSilent = false;
                    else this.scheduleSaveChannels();
                    sandbox.setTimeout(() => {      // pass 2 saves regardless
                        this.scheduleSaveChannels();
                        s.autoLeveled = true;
                        s.autoLeveling = false;
                    }, 300);
                }, 20);
            }, 0);
        },
        disableDuplicateChannels(name, at) {
            this.channelSlots.forEach((s) => {
                if (s.index !== at && s.enabled && s.name === name) s.enabled = false;
            });
        },
        setSlotMarker(i, name, options = {}) {
            const s = this.channelSlots[i];
            if (!s || !name) return;
            const changed = s.name !== name;
            if (!changed && !(options.enable && !s.enabled) && !(options.reveal && !s.visible)
                && !options.force) return;
            delete s.transient;
            this.disableDuplicateChannels(name, i);
            s.name = name;
            if (changed) Object.assign(s, { range: [0, 255], userRangeChanged: false,
                                            autoLeveled: false, rangeSource: null });
            if (!options.keepColor) this.setSlotColor(i, this.getDefaultColor(i).hex, false);
            if (options.enable) s.enabled = true;
            if (options.reveal || options.enable) s.visible = true;
            if (s.enabled) { this.activate(s); this.autoLevel(s); }
            this.scheduleSaveChannels();
        },
        setSlotEnabled(i, enabled) {
            const s = this.channelSlots[i];
            if (!s || !s.name) return;
            const was = s.enabled;
            delete s.transient;
            s.enabled = enabled;
            s.visible = true;
            if (enabled) {
                this.disableDuplicateChannels(s.name, i);
                this.activate(s);
                if (!was) this.autoLevel(s);
            }
            this.scheduleSaveChannels();
        },
        setSlotColor(i, hex, user) {
            const s = this.channelSlots[i];
            delete s.transient;
            s.colorHex = hex;
            s.userColorChanged = Boolean(user || s.userColorChanged);
            this.scheduleSaveChannels();
        },
        createAdditionalSlot(used) {
            if (this.channelSlots.length >= this.maxChannelSlots) return null;
            const index = this.channelSlots.length;
            const s = slot(index, this.columns.find((c) => !used.includes(c)) || "",
                           { autoLeveled: false });
            this.channelSlots.push(s);
            return s;
        },
        removeChannelSlot(i) {
            delete this.channelSlots[i].transient;
            Object.assign(this.channelSlots[i], { name: "", enabled: false, visible: false,
                                                  colorHex: DEFAULTS[i], userColorChanged: false,
                                                  range: [0, 255], userRangeChanged: false,
                                                  autoLeveled: false, rangeSource: null });
            this.scheduleSaveChannels();
        },
        slotShowing(name) { return this.channelSlots.find((s) => s.enabled && s.name === name) || null; },
        getDefaultColor(i) { return { hex: DEFAULTS[i % DEFAULTS.length] }; },
        syncSlotDom() {},
        // As core: holding saves drops the one waiting.
        suspendPersistence() { this.suspended += 1; sandbox.clearTimeout(this.timer); },
        resumePersistence() { this.suspended = Math.max(0, this.suspended - 1); },
        scheduleSaveChannels() {
            if (this.suspended) return;
            sandbox.clearTimeout(this.timer);
            this.timer = sandbox.setTimeout(() => {
                this.saves += 1;
                this.saved.push(this.persisted());
            }, 400);
        },
        // What core's persistChannelList writes: the rows the user can see,
        // never one a plugin holds for a moment.
        persisted() {
            return this.channelSlots.filter((s) => s.name && s.visible && !s.transient)
                .map((s) => `${s.name}:${s.enabled ? "on" : "off"}`);
        },
    };
    return sidebar;
}

let live = null;
const docked = [];

/** A panel: the real controller methods over stand-ins for what they draw. */
function panel({ slots, max, rows, marker = null } = {}) {
    live?.reviewChannels?.dispose();
    live?.reviewNotes?.dispose();
    for (const id of ["gate_provenance", "gate_ai_evidence", "gate_ai_evidence_count",
                      "gate_ai_evidence_toggle", "gate_ai_evidence_fold", "gate_ai_evidence_chips"]) {
        elements[id] = new FakeElement(id.endsWith("toggle") || id.endsWith("fold") ? "button" : "div");
    }
    elements.gate_ai_evidence.hidden = true;
    elements.gate_ai_evidence_toggle.setAttribute("aria-pressed", "true");
    elements.gate_ai_evidence_fold.setAttribute("aria-expanded", "true");
    const sidebar = makeSidebar({ slots, max });
    const self = Object.create(proto);
    Object.assign(self, {
        ctx: {
            dataset: { image: { has: (full) => IMAGE.has(full) } },
            view: { dock(el, options) {
                docked.push({ el, options });
                return () => el.remove();
            } },
        },
        sidebar, dataLayer: DATA, provenance: rows ?? provenance(), gateMarker: marker,
        gatingList: { columns: [...MARKERS], gating_channels: {}, selections: {} },
        _shown: true, _keysArmed: false, _reviewDismissed: new Set(), _provenanceSeq: 0,
        gateMarkerSelect: null, planes: null, classifier: null, contrast: null,
        paintMarkerSteps() {}, ensureGateSelection() {}, syncGateSlider() {}, syncAutoButton() {},
        drawGateDistribution() {}, paintConsistency() {}, paintQcNote() {}, scheduleSaveGating() {},
        api: { getGateProvenance: async () => self.provenance },
    });
    self.reviewChannels = new sandbox.GateReviewChannels(self);
    self.reviewNotes = new sandbox.GateReviewNotes(self);
    live = self;
    return { self, sidebar };
}

const claimOf = (self, channel) => self.reviewChannels.claims.find((c) => c.channel === channel);
const notesEl = (self) => self.reviewNotes.el;
const chips = (self) => notesEl(self).querySelector(".gate-ai-notes-chips").children;
const chip = (self, marker) => chips(self).find((c) => c.textContent.includes(marker));
const plain = (value) => JSON.parse(JSON.stringify(value));
const link = () => elements.gate_provenance.children.find((c) => "aiNotes" in c.dataset) || null;

const passed = [];
async function check(label, fn) {
    leased = false;
    storage.data = {};
    storage.broken = false;
    await fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

// -- the channels -------------------------------------------------------------------

await check("supporting channels come on in slots from 2 up, each in its own colour", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    const cd3 = claimOf(self, "CD3");
    const cd4 = claimOf(self, "CD4");
    assert.ok(cd3.index >= 2 && cd4.index >= 2);
    assert.equal(sidebar.channelSlots[cd3.index].enabled, true);
    assert.equal(sidebar.channelSlots[cd3.index].colorHex, OVERLAP);
    assert.equal(sidebar.channelSlots[cd4.index].colorHex, EXCLUSION);
    assert.notEqual(cd3.index, cd4.index);
});

await check("the marker's own channel and slots 0 and 1 are never touched", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    const [first, second] = plain(sidebar.channelSlots.slice(0, 2));
    assert.equal(first.name, "DNA");
    assert.equal(first.enabled, true);
    assert.equal(first.colorHex, DEFAULTS[0]);
    assert.equal(second.name, "CD8");
    assert.equal(second.colorHex, DEFAULTS[1]);
    assert.ok(self.reviewChannels.claims.every((c) => c.index >= 2 && c.channel !== "CD8"));
    assert.equal(sidebar.channelSlots.filter((s) => s.name === "CD8" && s.enabled).length, 1);
});

await check("a channel already on screen is the user's and is left as it is", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    assert.equal(claimOf(self, "DNA"), undefined);
    assert.equal(sidebar.channelSlots.filter((s) => s.name === "DNA").length, 1);
    assert.equal(chip(self, "DNA").style.getPropertyValue("--chip-color"), DEFAULTS[0]);
});

await check("moving to the next marker releases what the last one needed and keeps what both "
            + "do, without re-enabling it", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    const cd3 = claimOf(self, "CD3").index;
    self.setGateMarker("CD4");
    await advance(1000);
    assert.equal(claimOf(self, "CD3").index, cd3, "the shared channel kept its slot");
    assert.equal(sidebar.activations.filter((n) => n === "CD3").length, 1, "switched on once");
    assert.ok(claimOf(self, "CD8"), "the last marker is now a partner");
    assert.equal(claimOf(self, "CD4"), undefined);
    assert.equal(sidebar.slotShowing("CD4").index, 1, "the marker is slot 2's alone");
    assert.deepEqual(sidebar.fetches, [...new Set(sidebar.fetches)], "nothing fetched twice");
});

await check("a released slot is put back exactly as it was", async () => {
    const slots = [slot(0, "DNA", { enabled: true }), slot(1, "CD8", { enabled: true }),
                   slot(2, "CD20", { userColorChanged: true, colorHex: "#ff0000", range: [5, 200],
                                     userRangeChanged: true, rangeSource: "user" }),
                   slot(3, "Ki67", { visible: false, colorHex: "#abcdef", range: [1, 2] })];
    const { self, sidebar } = panel({ slots, max: 4 });
    const before = plain(sidebar.channelSlots);
    self.setGateMarker("CD8");
    assert.equal(claimOf(self, "CD3").index, 3, "a hidden row, never the user's row that is off");
    assert.deepEqual(plain(sidebar.channelSlots[2]), before[2]);
    assert.equal(sidebar.channelSlots[3].colorHex, OVERLAP);
    assert.match(chip(self, "CD4").title, /no free channel slot/);
    await advance(1000);
    self.setGateMarker("Ki67");
    await advance(1000);
    assert.deepEqual(plain(sidebar.channelSlots.slice(2)), before.slice(2));
    // Ki67 is fetched once, by the marker's own mirror into slot 2.
    assert.equal(sidebar.activations.filter((n) => n === "Ki67").length, 1);
    assert.ok(!sidebar.activations.includes("CD20"), "the user's row is never switched on");
});

await check("a save while supporting channels are on leaves them out", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    // The marker's own mirror and auto-levelling save after the review has
    // let saving go again; so does a user's edit elsewhere.
    sidebar.scheduleSaveChannels();
    await advance(500);
    assert.deepEqual(plain(sidebar.saved.at(-1)), ["DNA:on", "CD8:on", "CD20:off"]);
    // A taken row the user recolours is theirs, and saved.
    const at = claimOf(self, "CD3").index;
    sidebar.setSlotColor(at, "#123456", true);
    await advance(500);
    assert.ok(sidebar.saved.at(-1).includes("CD3:on"));
    assert.ok(!sidebar.saved.at(-1).includes("CD4:on"));
});

await check("a claimed slot the user recoloured is left alone", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    const at = claimOf(self, "CD3").index;
    sidebar.setSlotColor(at, "#123456", true);
    self.setGateMarker("Ki67");
    const mine = sidebar.channelSlots[at];
    assert.deepEqual([mine.name, mine.enabled, mine.colorHex], ["CD3", true, "#123456"]);
});

await check("a claimed slot the user switched off, or gave another channel, is theirs again",
            async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    const off = claimOf(self, "CD3").index;
    const other = claimOf(self, "CD4").index;
    sidebar.setSlotEnabled(off, false);
    sidebar.setSlotMarker(other, "Ki67", { keepColor: true, enable: true });
    self.syncReview();
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.deepEqual([sidebar.channelSlots[off].name, sidebar.channelSlots[off].enabled],
                     ["CD3", false]);
    assert.equal(sidebar.channelSlots[other].name, "Ki67");
    assert.ok(chip(self, "CD3").classList.contains("is-missing"));
    assert.match(chip(self, "CD3").title, /you changed or switched off its slot/);
});

await check("with no free slot the channel is skipped and said so", async () => {
    const slots = [slot(0, "DNA", { enabled: true }), slot(1, "CD8", { enabled: true }),
                   slot(2, "CD20", { enabled: true })];
    const { self, sidebar } = panel({ slots, max: 3 });
    self.setGateMarker("CD8");
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.equal(sidebar.channelSlots.length, 3);
    assert.ok(chip(self, "CD3").classList.contains("is-missing"));
    assert.match(chip(self, "CD3").textContent, /· not shown$/);
    assert.match(chip(self, "CD3").title, /no free channel slot/);
});

await check("a supporting channel the image does not have is skipped", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("Ki67");
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.ok(!sidebar.channelSlots.some((s) => s.name === "FOXP3"));
    assert.match(chip(self, "FOXP3").title, /not an image channel/);
});

await check("a gate with no review changes nothing", async () => {
    const { self, sidebar } = panel({ marker: "CD3" });
    sidebar.channelSlots[1].name = "CD3";
    sidebar.channelSlots[1].enabled = true;
    const before = plain(sidebar.channelSlots);
    self.setGateMarker("FOXP3");
    assert.deepEqual(plain(sidebar.channelSlots), before);
    assert.equal(self.reviewNotes.el, null, "no notes were ever built");
});

await check("the marker's own save still goes through, and leaves the supporting channels out",
            async () => {
    const slots = [slot(0, "DNA", { enabled: true }), slot(1, "", { visible: true }),
                   slot(2, "", { visible: false }), slot(3, "", { visible: false })];
    const { self, sidebar } = panel({ slots, marker: "DNA" });
    self.setGateMarker("CD8");
    assert.equal(self.reviewChannels.claims.length, 2);
    await advance(5000);
    assert.ok(sidebar.saves >= 1, "the mirror's save was not dropped");
    assert.deepEqual(plain(sidebar.saved.at(-1)), ["DNA:on", "CD8:on"]);
    self.reviewChannels.release();
    await advance(1000);
    assert.deepEqual(plain(sidebar.saved.at(-1)), ["DNA:on", "CD8:on"]);
    assert.equal(sidebar.suspended, 0);
    assert.ok(sidebar.channelSlots.slice(2).every((s) => !s.visible && !s.enabled));
});

await check("the same marker followed twice is a no-op", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    const before = plain(sidebar.channelSlots);
    const activations = sidebar.activations.length;
    self.syncReview();
    self.syncReview();
    await advance(1000);
    assert.deepEqual(plain(sidebar.channelSlots), before);
    assert.equal(sidebar.activations.length, activations);
    assert.equal(sidebar.suspended, 0);
});

// -- the notes ----------------------------------------------------------------------

await check("the notes show the reasoning and a chip in each channel's colour", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    const el = notesEl(self);
    assert.equal(el.hidden, false);
    assert.deepEqual(plain(docked.at(-1).options), { corner: "top-center", order: 30 });
    assert.equal(el.getAttribute("role"), "status");
    assert.equal(el.querySelector(".gate-ai-notes-marker").textContent, "CD8");
    assert.equal(el.querySelector(".gate-ai-notes-text").textContent,
                 provenance().CD8.review.notes);
    assert.equal(chip(self, "CD3").textContent, "within CD3");
    assert.equal(chip(self, "CD3").style.getPropertyValue("--chip-color"), OVERLAP);
    assert.equal(chip(self, "CD4").textContent, "excludes CD4");
    assert.equal(chip(self, "CD4").style.getPropertyValue("--chip-color"), EXCLUSION);
    assert.equal(el.querySelector(".gate-ai-notes-uncertainty").hidden, true);
    assert.equal(el.querySelector(".gate-ai-notes-flag").hidden, true);
});

await check("dismissed, the notes go and an AI Notes link appears under the gate", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    assert.equal(link(), null);
    notesEl(self).querySelector(".gate-ai-notes-dismiss").fire("click");
    await advance(250);
    assert.equal(notesEl(self).hidden, true);
    assert.equal(link().textContent, "AI Notes");
    assert.ok(link().classList.contains("gate-provenance-action"));
});

await check("the link brings the notes back", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    notesEl(self).fire("keydown", { key: "Escape" });
    await advance(250);
    assert.ok(link());
    self.reviewNotes.reopen();
    assert.equal(notesEl(self).hidden, false);
    assert.equal(link(), null);
});

await check("a new marker gets its own notes; a dismissed marker stays dismissed", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    self.reviewNotes.dismiss();
    await advance(250);
    self.setGateMarker("CD4");
    const el = notesEl(self);
    assert.equal(el.hidden, false);
    assert.equal(el.querySelector(".gate-ai-notes-marker").textContent, "CD4");
    assert.equal(el.dataset.needsReview, "true");
    // "Needs review" beside the name, and below only what the notes do not say.
    assert.equal(el.querySelector(".gate-ai-notes-flag").hidden, false);
    assert.equal(el.querySelector(".gate-ai-notes-flag").textContent, "Needs review");
    assert.equal(el.querySelector(".gate-ai-notes-uncertainty").textContent,
                 "the reads leave 6.4 (P 0.55) and 6.9 (P 0.35)");
    self.setGateMarker("CD8");
    assert.equal(el.hidden, true);
    assert.ok(link());
});

await check("provenance that lands after a marker switch applies to the marker on screen",
            async () => {
    const { self, sidebar } = panel({ rows: {} });
    self.setGateMarker("CD3");
    const answers = [];
    self.api.getGateProvenance = () => new Promise((resolve) => answers.push(resolve));
    const older = self.loadProvenance();
    self.setGateMarker("CD8");
    const newer = self.loadProvenance();
    self.setGateMarker("CD4");
    const rows = provenance();
    answers[1](rows);
    await newer;
    answers[0]({ CD3: { method: "ai_accepted", reason: "an older answer" } });
    await older;
    assert.equal(self.provenance, rows);
    assert.equal(notesEl(self).querySelector(".gate-ai-notes-marker").textContent, "CD4");
    assert.ok(claimOf(self, "CD8") && claimOf(self, "CD3"));
    assert.equal(sidebar.slotShowing("CD4").index, 1);
});

await check("the viewer given back by an agent: stale claims are forgotten and the marker "
            + "followed again", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    const cd3 = claimOf(self, "CD3").index;
    const cd4 = claimOf(self, "CD4").index;
    // Core put back the slots the agent's lease began with: one of ours is
    // something else now.
    Object.assign(sidebar.channelSlots[cd3], { name: "CD20", enabled: true });
    const waits = [];
    for (const fn of windowListeners["plexora:agent-restore"] || []) {
        fn({ detail: { wait: (p) => waits.push(p) } });
    }
    await Promise.all(waits);
    await advance(1000);
    assert.deepEqual([sidebar.channelSlots[cd3].name, sidebar.channelSlots[cd3].enabled],
                     ["CD20", true], "the rebuilt slot is left alone");
    assert.equal(claimOf(self, "CD4").index, cd4);
    const again = claimOf(self, "CD3");
    assert.ok(again && again.index !== cd3 && sidebar.channelSlots[again.index].enabled);
});

await check("hiding the panel releases the channels; showing it brings them back", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    self.onHide();
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.ok(sidebar.channelSlots.slice(3).every((s) => !s.enabled));
    assert.equal(notesEl(self).hidden, true);
    self.onShow();
    assert.equal(self.reviewChannels.claims.length, 2);
    assert.equal(notesEl(self).hidden, false);
});

await check("an older row without a review falls back to its reason and no channels", async () => {
    const { self } = panel();
    self.setGateMarker("CD20");
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.equal(notesEl(self).querySelector(".gate-ai-notes-text").textContent, "accepted at T2");
    assert.equal(notesEl(self).querySelector(".gate-ai-notes-chips").hidden, true);
});

await check("an agent moving the panel switches nothing on", async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    self.setGateMarker("CD4", { persist: false });
    assert.equal(self.reviewChannels.claims.length, 0, "the last marker's channels went back");
    assert.equal(notesEl(self).hidden, true);
    // Holding the view, the agent's slots are left as they are.
    leased = true;
    const before = plain(sidebar.channelSlots);
    self.setGateMarker("CD8", { persist: false });
    self.syncReview();
    assert.deepEqual(plain(sidebar.channelSlots.slice(2)), before.slice(2));
    assert.equal(self.reviewChannels.claims.length, 0);
});

// -- the AI evidence row ----------------------------------------------------------------

const evidence = () => elements.gate_ai_evidence;
const evidenceChips = () => elements.gate_ai_evidence_chips.children;

await check("the evidence row is hidden with no gates or only hand gates", async () => {
    const { self } = panel({ rows: {} });
    self.setGateMarker("CD8");
    assert.equal(evidence().hidden, true);
    self.provenance = { DNA: { method: "manual", status: "accepted" } };
    self.syncReview();
    assert.equal(evidence().hidden, true);
});

await check("the evidence row shows for an AI gate, and is muted but not hidden on a hand gate "
            + "beside AI gates", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    assert.equal(evidence().hidden, false);
    assert.equal(evidence().classList.contains("is-muted"), false);
    self.setGateMarker("DNA");
    assert.equal(evidence().hidden, false);
    assert.equal(evidence().classList.contains("is-muted"), true);
    assert.equal(elements.gate_ai_evidence_count.hidden, true);
    assert.equal(evidenceChips().length, 0);
});

await check("the evidence row counts the channels and wears each one's colour; folded, the chips "
            + "go", async () => {
    const { self } = panel();
    self.setGateMarker("CD8");
    assert.equal(elements.gate_ai_evidence_count.textContent, "4 channels");
    const byName = Object.fromEntries(evidenceChips().map((c) => [c.textContent, c]));
    assert.equal(byName.CD3.style.getPropertyValue("--chip-color"), OVERLAP);
    assert.equal(byName.CD4.style.getPropertyValue("--chip-color"), EXCLUSION);
    assert.equal(byName.DNA.style.getPropertyValue("--chip-color"), DEFAULTS[0], "the user's slot");
    assert.match(byName.CD3.title, /shown beside the cells/);
    self.setGateMarker("Ki67");
    assert.equal(elements.gate_ai_evidence_count.textContent, "1 channel");
    assert.ok(evidenceChips()[0].classList.contains("is-missing"));
    assert.match(evidenceChips()[0].title, /not an image channel/);
    self.setReviewEvidenceFolded(true);
    assert.equal(elements.gate_ai_evidence_chips.hidden, true);
    assert.equal(elements.gate_ai_evidence_fold.getAttribute("aria-expanded"), "false");
    assert.ok(evidence().classList.contains("is-folded"));
    self.setReviewEvidenceFolded(false);
    assert.equal(elements.gate_ai_evidence_chips.hidden, false);
});

await check("the eye shut releases the channels and keeps the notes; opened, it brings them back",
            async () => {
    const { self, sidebar } = panel();
    self.setGateMarker("CD8");
    await advance(1000);
    self.setReviewChannelsEnabled(false);
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.ok(sidebar.channelSlots.slice(3).every((s) => !s.enabled));
    assert.equal(notesEl(self).hidden, false);
    assert.ok(chip(self, "CD3").classList.contains("is-off"));
    assert.ok(!chip(self, "CD3").textContent.includes("not shown"));
    assert.ok(evidenceChips().every((c) => c.classList.contains("is-off")));
    const eye = elements.gate_ai_evidence_toggle;
    assert.equal(eye.getAttribute("aria-pressed"), "false");
    assert.match(eye.innerHTML, /fa-eye-slash/);
    self.setGateMarker("CD4");
    assert.equal(self.reviewChannels.claims.length, 0, "off for every marker");
    self.setReviewChannelsEnabled(true);
    assert.equal(self.reviewChannels.claims.length, 2);
    assert.equal(eye.getAttribute("aria-pressed"), "true");
    assert.match(eye.innerHTML, /fa-eye"/);
});

await check("the eye is remembered across controllers (localStorage stub)", async () => {
    const first = panel().self;
    first.setGateMarker("CD8");
    first.setReviewChannelsEnabled(false);
    assert.equal(storage.data["plexora.gating.reviewChannels"], "off");
    const { self } = panel();
    assert.equal(self.reviewChannelsEnabled(), false);
    self.setGateMarker("CD8");
    assert.equal(self.reviewChannels.claims.length, 0);
    assert.equal(elements.gate_ai_evidence_toggle.getAttribute("aria-pressed"), "false");
    // No storage at all (a private window): on, and a change holds for the page.
    storage.broken = true;
    const bare = panel().self;
    assert.equal(bare.reviewChannelsEnabled(), true);
    bare.setReviewChannelsEnabled(false);
    assert.equal(bare.reviewChannelsEnabled(), false);
});

console.log(`${passed.length} checks passed`);
