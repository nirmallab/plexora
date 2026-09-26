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
    document: { getElementById: () => null },
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
    assert.equal(say({ method: "gmm", status: "accepted", confidence: "high" }),
                 "Set automatically · high confidence");
    assert.equal(say({ method: "ai_refined", status: "accepted", confidence: "low",
                       state: "manual_review_recommended" }),
                 "Refined by an agent · low confidence · needs review");
    assert.equal(say({ method: "manual", status: "locked" }), "Locked");
    assert.equal(say({ method: "transfer_aligned", status: "approved", confidence: "moderate" }),
                 "Approved · Carried from the reference image · moderate confidence");
});

console.log(`${passed.length} checks passed`);
