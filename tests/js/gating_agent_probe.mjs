/**
 * Thresholding and an agent's gating session, client side.
 *
 * What is worth pinning is what must NEVER happen: a gate an agent is only
 * showing on the slider (`preview_gate`) reaching the saved list -- through
 * `gating_channels` or through `selections`, which the server's save also
 * reads -- and a locked gate reverted by the server leaving the slider
 * showing the value that was refused.
 *
 * The real listeners (gatingAgentBridge.js) and the real controller methods
 * (gatingSidebarController.js), against hand-built stand-ins, the same
 * arrangement as gating_marker_keys_probe.mjs.
 *
 * Run directly: `node tests/js/gating_agent_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "gating", "static");

const windowListeners = {};
const documentListeners = {};
const toasts = [];
// A clock the shows run on (gatingAgentShow.js): `advance` plays a minute of
// pauses and frames in a moment.
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
const sandbox = {
    console,
    Promise,
    performance: { now: () => clock.now },
    setTimeout: (fn, ms) => {
        clock.seq += 1;
        clock.timers.push({ id: clock.seq, at: clock.now + Math.max(0, Number(ms) || 0), fn });
        return clock.seq;
    },
    clearTimeout: (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); },
    CSVGatingList: { events: { GATING_BRUSH_MOVE: "GATING_BRUSH_MOVE",
                               SELECTION_CHANGED: "SELECTION_CHANGED" } },
    PlexoraToast: { show: (options) => toasts.push(options) },
    document: {
        getElementById: (id) => sandbox.__elements?.[id] || null,
        createElement: (tag) => ({
            tag, children: [], dataset: {}, listeners: {}, textContent: "", title: "", className: "",
            addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
            appendChild(child) { this.children.push(child); return child; },
        }),
        addEventListener: (type, fn) => (documentListeners[type] = documentListeners[type] || [])
            .push(fn),
    },
    addEventListener: (type, fn) => (windowListeners[type] = windowListeners[type] || []).push(fn),
};
sandbox.window = sandbox;
createContext(sandbox);
runInContext(readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;"
    + "\nglobalThis.describeGateProvenance = describeGateProvenance;", sandbox);
runInContext(readFileSync(join(STATIC, "gatingAgentShow.js"), "utf8"), sandbox);
runInContext(readFileSync(join(STATIC, "gatingAgentBridge.js"), "utf8"), sandbox);
const proto = sandbox.GatingSidebarController.prototype;

/** A controller-shaped stand-in with the real methods under test. */
function controller() {
    const saved = [];
    const self = {
        gateMarker: "CD8",
        agentPreview: null,
        provenance: {},
        gatingList: { gating_channels: { CD8: [300, 4000], CD3: [200, 3000] }, selections: {} },
        dataLayer: { getFullChannelName: (name) => name },
        getGateMarkerNames: () => ["CD3", "CD8"],
        getGateRange: () => [0, 4000],
        setGateMarker(name) { this.gateMarker = name; this.agentPreview = null; },
        // The real setGateRange writes both, as this does (minus drawing).
        setGateRange(values, event) {
            this.agentPreview = null;
            this.gatingList.gating_channels[this.gateMarker] = values;
            this.gatingList.selections = { [this.gateMarker]: values };
            this.lastEvent = event;
        },
        reloaded: 0,
        async reloadFromServer() { this.reloaded += 1; },
        api: {
            reverted: [],
            async saveGatingList(channels, selections) {
                saved.push({ channels: JSON.parse(JSON.stringify(channels)),
                             selections: JSON.parse(JSON.stringify(selections)) });
                return { success: true, reverted: this.reverted };
            },
        },
    };
    self.persistGatingList = proto.persistGatingList;
    sandbox.__plexora = { plugins: { get: () => ({ sidebarController: self }) } };
    return { self, saved };
}

function offer(type, args) {
    let claimed = null;
    const detail = { type, arguments: args, claimed: false,
                     claim(value, by) { detail.claimed = true; claimed = { value, by }; } };
    for (const fn of windowListeners["plexora:agent-command"] || []) fn({ detail });
    return claimed;
}

/** Objects made inside the vm context have its prototypes; compare plainly. */
const plain = (value) => JSON.parse(JSON.stringify(value));

const passed = [];
async function check(label, fn) {
    await fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

await check("a preview moves the slider and draws, without a save event", async () => {
    const { self } = controller();
    const answer = offer("preview_gate", { marker: "CD8", low: 900, high: null });
    assert.equal(answer.by, "gating");
    assert.deepEqual(plain(await answer.value), { marker: "CD8", low: 900, high: 4000, saved: false });
    assert.equal(self.lastEvent, "GATING_BRUSH_MOVE");
    assert.deepEqual(plain(self.gatingList.selections.CD8), [900, 4000]);
});

await check("the previewed gate never enters the list the sidebar saves", async () => {
    const { self, saved } = controller();
    offer("preview_gate", { marker: "CD8", low: 900 });
    assert.deepEqual(plain(self.gatingList.gating_channels.CD8), [300, 4000]);
    await self.persistGatingList();
    assert.deepEqual(saved[0].channels.CD8, [300, 4000]);
    assert.deepEqual(saved[0].selections.CD8, [300, 4000]);
    assert.deepEqual(saved[0].channels.CD3, [200, 3000]);
});

await check("a user's own move ends the preview", async () => {
    const { self, saved } = controller();
    offer("preview_gate", { marker: "CD8", low: 900 });
    self.setGateRange([1200, 4000], "SELECTION_CHANGED");
    await self.persistGatingList();
    assert.deepEqual(saved[0].channels.CD8, [1200, 4000]);
});

await check("a preview on a marker the panel cannot gate is refused, not ignored", async () => {
    controller();
    const answer = offer("preview_gate", { marker: "NOPE", low: 1 });
    assert.equal(answer.by, "gating");
    await assert.rejects(answer.value, /not a marker/);
});

await check("a locked gate reverted by the server is re-read and said out loud", async () => {
    const { self } = controller();
    self.api.reverted = ["CD8"];
    toasts.length = 0;
    await self.persistGatingList();
    assert.equal(self.reloaded, 1);
    assert.equal(toasts.length, 1);
    assert.match(toasts[0].title, /CD8 is locked/);
});

await check("provenance reads as a few words", async () => {
    const say = sandbox.describeGateProvenance;
    assert.equal(say({ method: "gmm", status: "accepted", confidence: "high" }), "Auto · high");
    assert.equal(say({ method: "gmm", status: "accepted", confidence: "high" }, { long: true }),
                 "Set automatically · high confidence");
    assert.equal(say({ method: "ai_refined", status: "accepted", confidence: "low",
                       state: "manual_review_recommended" }),
                 "Agent-refined · low · needs review");
    assert.equal(say({ method: "manual", status: "locked" }), "Locked");
    assert.equal(say({ method: "transfer_aligned", status: "approved", confidence: "moderate" },
                     { long: true }),
                 "Approved · Carried from the reference image · moderate confidence");
    const conditional = { method: "ai_conditional", status: "accepted", confidence: "moderate",
                          condition: { within: "CD45", n_positive_outside: 34 } };
    assert.equal(say(conditional), "Agent, conditional · within CD45+ · moderate");
    assert.equal(say(conditional, { long: true }),
                 "Fitted by an agent within a partner · positive only within CD45+ (the gate "
                 + "alone also calls 34 CD45\u2212 cells) · moderate confidence");
});

/** The pill's element, and a way to send it a session event. */
function pillPage() {
    const pill = {
        hidden: true, children: [],
        replaceChildren() { this.children = []; },
        appendChild(child) { this.children.push(child); return child; },
    };
    sandbox.__elements = { gating_agent_pill: pill };
    const send = (payload) => {
        for (const fn of windowListeners["plexora:agent-state-changed"] || []) {
            fn({ detail: { plugin: "gating", kind: "gating.session", payload } });
        }
    };
    return { pill, send, text: () => pill.children[0]?.textContent,
             buttons: () => pill.children.filter((c) => c.tag === "button").map((c) => c.textContent) };
}

await check("the pill offers Take over only; pause and resume live in the agent panel", async () => {
    controller();
    const { pill, send, text, buttons } = pillPage();
    send({ session_id: "gs_1", event: "started", phase: "planning" });
    assert.equal(pill.hidden, false);
    assert.deepEqual(plain(buttons()), ["Take over"]);
    assert.equal(text(), "An agent is gating this image");
    send({ session_id: "gs_1", event: "control", paused: true, paused_by: "viewer" });
    assert.equal(text(), "Agent gating paused");
    assert.deepEqual(plain(buttons()), ["Take over"]);
    send({ session_id: "gs_1", event: "issued", marker: "CD3" });
    assert.equal(text(), "Agent gating paused", "an event without `paused` keeps the pause state");
    send({ session_id: "gs_1", event: "control", paused: false });
    assert.equal(text(), "An agent is gating this image");
    send({ session_id: "gs_1", event: "finished", reason: "closed" });
    assert.equal(pill.hidden, true);
    sandbox.__elements = {};
});

await check("a restore puts the marker and the stored gate back", async () => {
    const { self } = controller();
    const calls = [];
    self.setGateMarker = function (name, options = {}) {
        calls.push([name, plain(options)]);
        this.gateMarker = name;
        this.agentPreview = null;
    };
    offer("preview_gate", { marker: "CD8", low: 900 });
    assert.ok(self.agentPreview, "the preview is up");
    const waits = [];
    for (const fn of windowListeners["plexora:agent-restore"] || []) {
        fn({ detail: { reason: "closed", plugins: { gating: { active_marker: "CD3" } },
                       wait: (promise) => waits.push(promise) } });
    }
    assert.equal(waits.length, 1, "core is given something to wait for");
    const answer = await waits[0];
    assert.deepEqual(calls, [["CD8", { force: true, syncSlot: false }], ["CD3", { syncSlot: false }]]);
    assert.equal(self.agentPreview, null);
    assert.equal(plain(answer).active_marker, "CD3");
    // Nothing to put back: no preview, already on the leased marker.
    calls.length = 0;
    for (const fn of windowListeners["plexora:agent-restore"] || []) {
        fn({ detail: { plugins: { gating: { active_marker: "CD3" } }, wait: (p) => waits.push(p) } });
    }
    await waits.at(-1);
    assert.deepEqual(calls, []);
});

await check("a failed or empty marker reads as a gate at the maximum", async () => {
    const say = sandbox.describeGateProvenance;
    assert.equal(say({ method: "failed_marker", status: "accepted", state: "technically_failed" }),
                 "Failed stain");
    assert.equal(say({ method: "no_positive_population", state: "no_positive_population",
                       reason: "no cell above the negative control" }, { long: true }),
                 "No cell positive \u2014 gate at the maximum · no cell above the negative control");
    assert.equal(say({ method: "gmm", state: "no_positive_population" }), "Auto · no positives");
    assert.equal(say({ method: "gmm", status: "accepted", confidence: "high", reason: "clear split" }),
                 "Auto · high", "the short form leaves the reason out");
});

// -- an agent's look, played as a person would make it ------------------------

const show = sandbox.PlexoraGatingShow;
const CELLS = [{ x: 100, y: 100 }, { x: 160, y: 120 }, { x: 140, y: 190 }, { x: 90, y: 170 }];
const FIELD = { x: 0, y: 0, width: 300, height: 300 };
const spec = (seed, extra = {}) => Object.assign({
    seed, look: 1, target: [900, 4000], from: 300, range: [0, 4000],
    stops: [600, 750, 1100, 1500], cells: CELLS, field: FIELD }, extra);
const gates = (beats) => beats.filter((b) => b.kind === "gate").map((b) => b.low);

await check("a first look tries a gate well off, closes in on a cell, and ends on the agent's", async () => {
    for (let seed = 0; seed < 40; seed += 1) {
        const { beats, idle } = show.plan(spec(seed));
        const lows = gates(beats);
        assert.equal(lows.at(-1), 900, `seed ${seed} ends on the gate`);
        assert.ok(lows.some((v) => Math.abs(v - 900) > 4000 * 0.03), `seed ${seed} tries one well off`);
        assert.ok(lows.every((v) => v >= 0 && v < 4000), `seed ${seed} stays on the slider`);
        // Close enough to see a few cells and their neighbours change.
        assert.ok(beats.some((b) => b.kind === "glide" && b.span <= 420), `seed ${seed} closes in`);
        assert.ok([...beats, ...idle].every((b) => b.ms > 0), `seed ${seed} takes its time`);
    }
});

await check("while it waits, a look moves the view and never the gate", async () => {
    // A nudge of the gate and back read as the marker being gated again.
    for (let seed = 0; seed < 40; seed += 1) {
        for (const look of [1, 2, 3]) {
            const { idle } = show.plan(spec(seed, { look, from: look > 1 ? 860 : 300 }));
            assert.equal(gates(idle).length, 0, `seed ${seed}, look ${look}`);
        }
    }
});

await check("a gate is moved slowly enough to watch the cells change", async () => {
    for (let seed = 0; seed < 40; seed += 1) {
        const { beats, ms } = show.plan(spec(seed));
        for (const beat of beats.filter((b) => b.kind === "gate")) {
            assert.ok(beat.ms >= 900, `seed ${seed}: a drag of ${beat.ms} ms`);
        }
        assert.ok(ms >= 12000, `seed ${seed}: a first look of ${ms} ms`);
    }
});

await check("markers are not shown with the same moves; a marker always with its own", async () => {
    const kinds = new Set();
    const firstWay = new Set();
    for (let seed = 0; seed < 12; seed += 1) {
        const choreography = show.plan(spec(seed));
        kinds.add(`${choreography.temper}/${choreography.camera}`);
        firstWay.add(gates(choreography.beats)[0] > 900 ? "strict" : "loose");
    }
    assert.equal(kinds.size, 12, "every temperament with every way of moving the view");
    assert.deepEqual([...firstWay].sort(), ["loose", "strict"]);
    assert.deepEqual(plain(show.plan(spec(5))), plain(show.plan(spec(5))));
    assert.notDeepEqual(plain(show.plan(spec(5)).beats),
                        plain(show.plan(spec(5, { look: 2, from: 900 })).beats));
});

await check("a later look refines from where the slider is", async () => {
    for (let seed = 0; seed < 20; seed += 1) {
        const first = show.plan(spec(seed));
        const later = show.plan(spec(seed, { look: 3, from: 860 }));
        assert.equal(gates(later.beats).at(-1), 900);
        assert.ok(later.beats.length < first.beats.length, `seed ${seed}`);
    }
});

/** The controller stand-in, recording every gate it is painted. */
function painting() {
    const made = controller();
    const painted = [];
    const quiet = [];
    const real = made.self.setGateRange;
    made.self.setGateRange = function (values, event, options) {
        painted.push(values[0]);
        quiet.push(Boolean(options && options.quiet));
        return real.call(this, values, event);
    };
    return Object.assign(made, { painted, quiet });
}

const SHOW = { seed: 3, look: 1, cells: CELLS, field: FIELD, stops: [1100, 1500] };

await check("a look with a show is acknowledged at once, then played a frame at a time", async () => {
    const { self, painted, quiet } = painting();
    const answer = offer("preview_gate", { marker: "CD8", low: 900, show: SHOW });
    const result = plain(await answer.value);
    assert.equal(result.show.played, true);
    assert.equal(result.saved, false);
    await advance(150000);
    assert.ok(painted.length > 20, `painted frame by frame (${painted.length})`);
    assert.ok(Math.min(...painted) < 800 || Math.max(...painted) > 1000, "a try off the gate");
    assert.equal(painted.at(-1), 900);
    assert.ok(quiet.every(Boolean), "a frame is not counted as a person's brush");
    assert.deepEqual(plain(self.gatingList.gating_channels.CD8), [300, 4000], "never saved");
    assert.ok(self.agentPreview, "still a preview");
    assert.equal(show.playing(), false, "over once its idle moves are done");
});

await check("a newer look, or a hand on the gate, ends the one playing", async () => {
    let { painted } = painting();
    offer("preview_gate", { marker: "CD8", low: 900, show: SHOW });
    await advance(3000);
    offer("preview_gate", { marker: "CD8", low: 1200 });
    const before = painted.length;
    await advance(60000);
    assert.equal(painted.length, before, "nothing painted over the newer look");
    assert.equal(painted.at(-1), 1200);

    ({ painted } = painting());
    const slider = { closest: () => null };
    sandbox.__elements = { gate_marker_section: { contains: (target) => target === slider } };
    offer("preview_gate", { marker: "CD8", low: 900, show: SHOW });
    await advance(2500);
    assert.equal(show.playing(), true);
    for (const fn of documentListeners.pointerdown || []) fn({ target: slider });
    assert.equal(show.playing(), false);
    const held = painted.length;
    await advance(60000);
    assert.equal(painted.length, held, "what the hand sets stands");
    sandbox.__elements = {};
});

await check("a gate the session writes while a look plays is the gate the page saves", async () => {
    // ImageLSP23362, 2026-10-30: a look still playing on Ki-67 put back the
    // gate it found when it began (none) over the one the session had just
    // written, and the page's next save wrote that.
    const { self, saved } = painting();
    offer("preview_gate", { marker: "CD8", low: 900, show: SHOW });
    await advance(4000);
    assert.equal(show.playing(), true);
    // What reloadFromServer leaves: the stored list re-read, the preview over.
    self.gatingList.gating_channels.CD8 = [1234, 4000];
    self.agentPreview = null;
    await advance(150000);
    assert.deepEqual(plain(self.gatingList.gating_channels.CD8), [1234, 4000]);
    await self.persistGatingList();
    assert.deepEqual(saved.at(-1).channels.CD8, [1234, 4000]);
    assert.deepEqual(saved.at(-1).selections.CD8, [1234, 4000]);
});

await check("a look playing is put back on the slider once the gates are re-read", async () => {
    const { self, painted } = painting();
    const result = plain(await offer("preview_gate", { marker: "CD8", low: 900, show: SHOW }).value);
    await advance(result.show.ms + 1500);
    assert.equal(show.playing(), true, "still in its idle moves");
    const before = painted.length;
    for (const fn of windowListeners["plexora:agent-state-changed"] || []) {
        fn({ detail: { plugin: "gating", kind: "gates_saved" } });
    }
    await advance(10);
    assert.equal(self.reloaded, 1);
    assert.equal(painted.length, before + 1);
    assert.equal(painted.at(-1), 900);
    show.stop();
});

await check("a look says how long its moves take, and has made them by then", async () => {
    const { painted } = painting();
    const result = plain(await offer("preview_gate", { marker: "CD8", low: 900, show: SHOW }).value);
    assert.ok(result.show.ms >= 12000, `${result.show.ms} ms`);
    await advance(result.show.ms);
    assert.equal(painted.at(-1), 900, "on the agent's gate by the time it said");
    show.stop();
});

await check("a later look at a marker goes on from where its last one ended", async () => {
    for (const fn of windowListeners["plexora:agent-state-changed"] || []) {
        fn({ detail: { plugin: "gating", kind: "gating.session",
                       payload: { session_id: "gs_2", event: "started" } } });
    }
    const { self, painted } = painting();
    offer("preview_gate", { marker: "CD8", low: 900, show: SHOW });
    await advance(150000);
    // Another marker in between: CD8 is no longer on the slider.
    offer("preview_gate", { marker: "CD3", low: 700, show: { ...SHOW, seed: 8 } });
    await advance(5000);
    assert.equal(self.gateMarker, "CD3");
    const before = painted.length;
    offer("preview_gate", { marker: "CD8", low: 950, show: { ...SHOW, look: 2 } });
    await advance(150000);
    const again = painted.slice(before);
    assert.ok(again.length > 5);
    assert.ok(again.every((v) => v > 600), `from where CD8 was (${Math.min(...again)}), `
        + "not from its stored gate (300)");
    assert.equal(again.at(-1), 950);
});

await check("an agent moving the panel to a marker saves nothing", async () => {
    const { self } = controller();
    const calls = [];
    self.setGateMarker = function (name, options = {}) {
        calls.push([name, plain(options)]);
        this.gateMarker = name;
    };
    offer("set_active_marker", { marker: "CD3" });
    offer("preview_gate", { marker: "CD8", low: 900 });
    assert.deepEqual(calls, [["CD3", { persist: false }], ["CD8", { persist: false }]]);
});

await check("re-reading the saved gates keeps the marker on screen", async () => {
    // Every gate a session wrote re-read the list, and the panel jumped to
    // the marker the list last had active -- ending the look on screen.
    const { self } = controller();
    const picked = [];
    Object.assign(self, {
        sidebar: { isRestoring: () => false },
        loadProvenance() {},
        populateGateSelect() {},
        dataLayer: { getFullChannelName: (n) => n, getShortChannelName: (n) => n },
        setGateMarker(name) { picked.push(name); this.gateMarker = name; },
        applySavedGating: proto.applySavedGating,
        reloadFromServer: proto.reloadFromServer,
    });
    self.api.getSavedGatingList = async () => [
        { channel: "CD3", gate_start: 250, gate_end: 3000, gate_active: true },
        { channel: "CD8", gate_start: 640, gate_end: 4000, gate_active: false },
    ];
    self.gateMarker = "CD8";
    assert.equal(await self.reloadFromServer(), true);
    assert.deepEqual(picked, ["CD8"]);
    assert.deepEqual(plain(self.gatingList.gating_channels.CD8), [640, 4000]);
    // With nothing on screen, the list's active marker, as at page load.
    picked.length = 0;
    self.gateMarker = null;
    await self.reloadFromServer();
    assert.deepEqual(picked, ["CD3"]);
});

await check("a marker an automatic run gated keeps its dot when its gate is gone", async () => {
    const { self } = controller();
    Object.assign(self, {
        sidebar: { formatValue: (v) => String(v) },
        classifier: null,
        hasCustomGate: (name) => name === "CD3",
        describeGateIndicator: proto.describeGateIndicator,
        provenance: { CD8: { method: "ai_panel", status: "accepted", confidence: "moderate",
                             written_low: 7.04, written_high: 9.0 } },
    });
    self.gatingList.gating_channels.CD3 = [250, 3000];
    // Gated by a run, then put back to its full range: still a dot, saying so.
    assert.match(self.describeGateIndicator("CD8"), /^No gate now · gated before at 7\.04/);
    // Gated now: the gate, as before.
    assert.match(self.describeGateIndicator("CD3"), /^Gated 250–3000/);
    // Never gated, or cleared (no record): no dot.
    self.provenance = {};
    assert.equal(self.describeGateIndicator("CD8"), null);
});

await check("with smooth motion off, the gate is set at once", async () => {
    const { painted } = painting();
    sandbox.PlexoraChannelFade = { smooth: () => false };
    try {
        const result = plain(await offer("preview_gate",
                                         { marker: "CD8", low: 900, show: SHOW }).value);
        assert.equal(result.show.played, false);
        assert.deepEqual(painted, [900]);
        assert.equal(show.playing(), false);
    } finally {
        delete sandbox.PlexoraChannelFade;
    }
});

console.log(`${passed.length} checks passed`);
