/**
 * ‹ and › on the Thresholding plot: the lower threshold one step at a time.
 *
 * The step itself is PlexoraSlider#nudge, pinned in slider_probe.mjs against
 * what an arrow key does. What is pinned here is the plugin's half, which is
 * deliberately thin: a click reaches the right button, the button asks the
 * gate slider for one step on its LOWER handle -- never computing a step of
 * its own -- and the buttons are off while the slider is.
 *
 * The real methods, called with `.call()` against a hand-built `this` -- the
 * same arrangement as gating_marker_keys_probe.mjs.
 *
 * Run directly: `node tests/js/gating_nudge_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "gating", "static",
                    "gatingSidebarController.js");

let buttons = [];
const queries = [];
const sandbox = {
    console,
    document: {
        querySelectorAll: (sel) => { queries.push(sel); return buttons; },
        addEventListener: () => {},
        removeEventListener: () => {},
    },
};
sandbox.window = sandbox;
sandbox.clearTimeout = () => {};
createContext(sandbox);
runInContext(
    readFileSync(SOURCE, "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;",
    sandbox);
const proto = sandbox.GatingSidebarController.prototype;

/** A gate slider that records what it was asked, and answers `moves`. */
function fakeSlider({ moves = true, disabled = false } = {}) {
    const asked = [];
    return {
        asked,
        el: { classList: { contains: (name) => name === "is-disabled" && disabled } },
        nudge(which, direction) { asked.push([which, direction]); return moves; },
    };
}

function state({ marker = "CD3", slider = fakeSlider() } = {}) {
    const self = { gateMarker: marker, gateSlider: slider };
    for (const name of ["onNudgeClick", "onNudgeKey", "nudgeLowerGate", "syncNudgeButtons"]) {
        self[name] = proto[name];
    }
    return self;
}

/** A click whose target sits inside a button carrying `data-nudge`. */
function clickOn(nudge) {
    const button = nudge === undefined ? null : {
        dataset: { nudge: String(nudge) },
        focused: 0,
        focus() { this.focused += 1; },
    };
    return { button, target: { closest: (sel) => (sel === "[data-nudge]" ? button : null) } };
}

/** A key pressed while a button -- or, with `onButton` false, not -- has focus. */
function keyOn(key, { onButton = true, ...mods } = {}) {
    const event = { ...clickOn(onButton ? 1 : undefined), key, prevented: false, ...mods };
    event.preventDefault = () => { event.prevented = true; };
    return event;
}

const passed = [];
function check(label, fn) {
    buttons = [];
    queries.length = 0;
    fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

check("› asks the slider for one step up on the lower handle", () => {
    const self = state();
    assert.equal(self.nudgeLowerGate(1), true);
    assert.deepEqual(self.gateSlider.asked, [["low", 1]]);
});

check("‹ asks for one step down", () => {
    const self = state();
    self.nudgeLowerGate(-1);
    assert.deepEqual(self.gateSlider.asked, [["low", -1]]);
});

check("a step that went nowhere is reported as one", () => {
    const self = state({ slider: fakeSlider({ moves: false }) });
    assert.equal(self.nudgeLowerGate(-1), false);
});

check("a click on the plot reaches the button under it", () => {
    const self = state();
    self.onNudgeClick(clickOn(1));
    self.onNudgeClick(clickOn(-1));
    assert.deepEqual(self.gateSlider.asked, [["low", 1], ["low", -1]]);
});

check("a click leaves the button focused, so the keys can carry on", () => {
    const self = state();
    const click = clickOn(1);
    self.onNudgeClick(click);
    assert.equal(click.button.focused, 1, "WebKit does not focus a clicked button");
});

check("after a click Right and Up raise, Left and Down lower, one step a key", () => {
    const self = state();
    const keys = ["ArrowRight", "ArrowUp", "ArrowLeft", "ArrowDown"].map((key) => keyOn(key));
    keys.forEach((event) => self.onNudgeKey(event));
    assert.deepEqual(self.gateSlider.asked, [["low", 1], ["low", 1], ["low", -1], ["low", -1]]);
    assert.ok(keys.every((event) => event.prevented), "the sidebar does not scroll as well");
});

check("a key at the end of the track is still taken", () => {
    const self = state({ slider: fakeSlider({ moves: false }) });
    const event = keyOn("ArrowDown");
    self.onNudgeKey(event);
    assert.equal(event.prevented, true);
});

check("other keys, modified arrows and keys elsewhere on the plot are left alone", () => {
    const self = state();
    const left = [keyOn("Enter"), keyOn("x"), keyOn("ArrowRight", { onButton: false }),
                  ...["metaKey", "ctrlKey", "altKey", "shiftKey"].map((mod) => keyOn("ArrowLeft", { [mod]: true }))];
    left.forEach((event) => self.onNudgeKey(event));
    assert.equal(self.gateSlider.asked.length, 0);
    assert.ok(left.every((event) => !event.prevented));
});

check("a click elsewhere on the plot does nothing", () => {
    const self = state();
    self.onNudgeClick(clickOn(undefined));
    self.onNudgeClick({ target: {} });
    assert.equal(self.gateSlider.asked.length, 0);
});

check("with no marker or no slider a click is a no-op", () => {
    const bare = state({ marker: null });
    assert.equal(bare.nudgeLowerGate(1), false);
    assert.equal(bare.gateSlider.asked.length, 0);
    const unbuilt = state({ slider: null });
    assert.equal(unbuilt.nudgeLowerGate(1), false);
});

check("the buttons are off while the slider is", () => {
    buttons = [{ disabled: false }, { disabled: false }];
    state({ slider: fakeSlider({ disabled: true }) }).syncNudgeButtons();
    assert.deepEqual(buttons.map((b) => b.disabled), [true, true]);
    assert.equal(queries[0], "#gate_distribution_plot [data-nudge]");
    state().syncNudgeButtons();
    assert.deepEqual(buttons.map((b) => b.disabled), [false, false]);
    state({ slider: null }).syncNudgeButtons();
    assert.deepEqual(buttons.map((b) => b.disabled), [true, true]);
});

console.log(`all checks passed (${passed.length})`);
