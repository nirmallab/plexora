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
const toasts = [];
const sandbox = {
    console,
    Promise,
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
    },
    addEventListener: (type, fn) => (windowListeners[type] = windowListeners[type] || []).push(fn),
};
sandbox.window = sandbox;
createContext(sandbox);
runInContext(readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;"
    + "\nglobalThis.describeGateProvenance = describeGateProvenance;", sandbox);
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

console.log(`${passed.length} checks passed`);
