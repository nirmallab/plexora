/**
 * The one action on the channel contrast line: Auto, and the way back from it.
 *
 * Auto is the only destructive control in the channel panel. It replaces the
 * window on screen with a GaussianMixture fit, and on a channel somebody has
 * already tuned by eye that window is the only copy of a number they cannot
 * get back by pressing Auto again -- the fit is deterministic, so it returns
 * the same answer it just gave. The button therefore takes the pair down
 * before it runs and turns into an undo of itself.
 *
 * What is worth pinning here is not that an icon changes. It is:
 *
 *   - that the stored pair is the pair that was ON SCREEN, exactly, including
 *     a value the user typed rather than one a fit produced;
 *   - that reverting restores the two flags that say where that range came
 *     from, so a channel that had never been levelled is free to level itself
 *     again the next time it is switched on;
 *   - that reverting is NOT a manual edit: it must not stamp the restored
 *     numbers over `markerRangeOverrides`, or pin the channel against future
 *     auto-levelling, or touch the colour, the marker or whether it is on;
 *   - that a fit which moved nothing leaves no undo behind, so the icon never
 *     offers to restore the range already showing;
 *   - that the button cannot be pressed twice while the fit is in flight,
 *     which would start a second fit and overwrite the stored pair with the
 *     auto result.
 *
 * The methods are extracted from viewerSidebar.js and run as written, so this
 * measures shipped code rather than a reimplementation that could agree with
 * itself while the app is wrong. No jsdom in this repo; the button below is
 * the subset those methods touch.
 *
 * Run directly:  node tests/js/channel_auto_revert_probe.mjs
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const relPath = "plexora/client/src/js/views/viewerSidebar.js";

/**
 * Slice one method out of a class body by brace matching.
 *
 * Tracks line/block comments and quotes so a brace inside prose or a string
 * cannot end the match early -- these methods sit under long explanatory
 * comment blocks, which is exactly where a naive scanner goes wrong.
 */
function extractMethod(source, name) {
    const signature = new RegExp(`\\n    (?:async )?${name}\\(`).exec(source);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    // Past the parameter list before looking for the body's `{`. One of these
    // methods is `syncSlotAutoButton(slot, button, options = {})`, and a
    // scanner that took the first brace it saw would match that default and
    // close it again on the very next character.
    let p = source.indexOf("(", start);
    for (let parens = 0; p < source.length; p++) {
        if (source[p] === "(") parens++;
        else if (source[p] === ")" && --parens === 0) break;
    }
    let i = source.indexOf("{", p);
    if (i < 0) throw new Error(`no body for ${name}`);

    let depth = 0;
    let state = "code";
    for (; i < source.length; i++) {
        const c = source[i];
        const next = source[i + 1];
        if (state === "line") {
            if (c === "\n") state = "code";
            continue;
        }
        if (state === "block") {
            if (c === "*" && next === "/") { state = "code"; i++; }
            continue;
        }
        if (c === "/" && next === "/") { state = "line"; i++; continue; }
        if (c === "/" && next === "*") { state = "block"; i++; continue; }
        if (c === '"' || c === "'" || c === "`") {
            const quoteChar = c;
            i++;
            for (; i < source.length; i++) {
                if (source[i] === "\\") { i++; continue; }
                if (source[i] === quoteChar) break;
            }
            continue;
        }
        if (c === "{") depth++;
        else if (c === "}") {
            depth--;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`unterminated body for ${name}`);
}

const METHODS = [
    "onSlotAutoClick", "revertSlotRange", "syncSlotAutoButton",
    "setSlotRange", "normalizeRange", "updateSlotReadout", "sizeRangeFields",
];

// setSlotRange reaches for it; the names are all this probe needs of it.
globalThis.ChannelList = { events: { BRUSH_MOVE: "BRUSH_MOVE" } };

/* ------------------------------------------------------------ stand-ins -- */

/** The subset of a <button> that syncSlotAutoButton writes to. */
function makeButton() {
    const classes = new Set();
    return {
        dataset: {},
        disabled: false,
        title: "",
        innerHTML: "",
        attributes: {},
        classList: {
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            contains(name) { return classes.has(name); },
        },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        /** What the glyph is, in the terms the panel is described in. */
        get icon() {
            const match = /fa-([a-z0-9-]+)"/.exec(this.innerHTML);
            return match ? match[1] : "";
        },
    };
}

const source = await readFile(path.join(repoRoot, relPath), "utf8");
const body = METHODS.map((name) => extractMethod(source, name)).join(",\n");
// Object-method shorthand is the same grammar as a class method body, so the
// extracted text drops straight in with no rewriting.
const methods = new Function(`return {\n${body}\n};`)();

/**
 * A sidebar with one slot, and an `autoChannel` the caller supplies.
 *
 * `fit` stands in for the whole two-pass GMM path: everything it does that
 * matters to this file is write `slot.range` and the two flags, so a function
 * that does exactly that is the honest stand-in.
 */
function makeSidebar({ range = [12, 240], fit = null, ...slotState } = {}) {
    const button = makeButton();
    const slot = {
        index: 0,
        name: "CD45",
        color: { r: 35, g: 136, b: 255 },
        colorHex: "#2388ff",
        enabled: true,
        visible: true,
        expanded: true,
        range: [...range],
        userColorChanged: false,
        userRangeChanged: false,
        autoLeveled: false,
        autoLeveling: false,
        preAutoRange: null,
        ...slotState,
    };
    const sidebar = {
        ...methods,
        button,
        slot,
        brushed: [],
        saves: 0,
        fits: 0,
        channelSlots: [slot],
        markerRangeOverrides: new Map(),
        channelList: { image_channels: {} },
        // What the two numbers and the handles are showing.
        channelSlotSliders: new Map([[0, {
            shown: null,
            set(values) { this.shown = [...values]; },
        }]]),
        eventHandler: { trigger: (name, packet) => sidebar.brushed.push([name, packet]) },
        // `syncSlotAutoButton` resolves its button through the row, which is
        // now found by this instance's own prefixed id rather than by a
        // document-wide selector (see tests/js/sidebar_scoping_probe.mjs).
        slotRow: () => ({ querySelector: () => button }),
        scheduleSaveChannels() { sidebar.saves += 1; },
        async autoChannel(index, options) {
            sidebar.fits += 1;
            // The real one checks this before doing anything; a stand-in that
            // ignored it would hide a caller that forgot to force.
            assert.ok(options?.force, "the button's own Auto always forces the fit");
            // Pressed a second time while the first is still running, the
            // button must not be able to reach this at all.
            assert.equal(sidebar.fits, 1, "only one fit per press");
            if (fit) await fit(sidebar, index);
        },
    };
    return sidebar;
}

/* ----------------------------------------------------------------- 1 --- */
// A slot that has never been levelled shows the Auto wand and says so in its
// tooltip -- no visible text, which is the whole reason the row now fits on
// one line beside the slider.
{
    const sidebar = makeSidebar();
    sidebar.syncSlotAutoButton(sidebar.slot, sidebar.button);
    assert.equal(sidebar.button.dataset.state, "auto");
    assert.equal(sidebar.button.icon, "wand-magic-sparkles");
    assert.equal(sidebar.button.title, "Auto contrast");
    assert.equal(sidebar.button.attributes["aria-label"], "Auto contrast");
    assert.ok(!/>[^<]*[A-Za-z]/.test(sidebar.button.innerHTML),
        "the icon carries no persistent text label");
    console.log("the resting state is a labelless Auto icon with a tooltip");
}

/* ----------------------------------------------------------------- 2 --- */
// The exact pair on screen is taken down, and the icon becomes the way back.
// The typed 1234 is the case that matters: a value off the slider's own log
// grid, which the control must return unrounded.
{
    const sidebar = makeSidebar({
        range: [1234, 51001],
        userRangeChanged: true,
        fit: (s) => { s.slot.range = [880, 62000]; },
    });
    await sidebar.onSlotAutoClick(0);

    assert.deepEqual(sidebar.slot.range, [880, 62000], "the fit landed");
    assert.deepEqual(sidebar.slot.preAutoRange.range, [1234, 51001],
        "the pair from before the fit is held, exactly");
    assert.equal(sidebar.button.dataset.state, "revert");
    assert.equal(sidebar.button.icon, "rotate-left");
    assert.equal(sidebar.button.title, "Restore previous range");
    assert.equal(sidebar.button.disabled, false, "and can be pressed");
    console.log("Auto stores the exact window it is about to replace, then offers it back");
}

/* ----------------------------------------------------------------- 3 --- */
// Revert puts the pair back, on the slider as well as in the model, and the
// button returns to Auto.
{
    const sidebar = makeSidebar({
        range: [1234, 51001],
        userRangeChanged: true,
        autoLeveled: false,
        fit: (s) => { s.slot.range = [880, 62000]; s.slot.autoLeveled = true; },
    });
    await sidebar.onSlotAutoClick(0);
    const savesAfterAuto = sidebar.saves;

    sidebar.revertSlotRange(0);
    assert.deepEqual(sidebar.slot.range, [1234, 51001], "the window is back, exactly");
    assert.deepEqual(sidebar.channelSlotSliders.get(0).shown, [1234, 51001],
        "and the two numbers and handles are showing it");
    assert.equal(sidebar.slot.preAutoRange, null, "with nothing left to restore");
    assert.equal(sidebar.button.dataset.state, "auto");
    assert.equal(sidebar.button.icon, "wand-magic-sparkles");
    assert.ok(sidebar.saves > savesAfterAuto, "and the restored window is persisted");

    const brush = sidebar.brushed.at(-1);
    assert.equal(brush[0], "BRUSH_MOVE", "the image is repainted through the usual event");
    assert.deepEqual(brush[1].dataRange, [1234, 51001]);
    console.log("Revert restores the exact window and returns the icon to Auto");
}

/* ----------------------------------------------------------------- 4 --- */
// It restores where the range CAME FROM as well as the range. A channel that
// had never been levelled must be free to level itself again the next time it
// is switched on, exactly as it would have been had Auto never been pressed.
{
    const sidebar = makeSidebar({
        range: [0, 255],
        userRangeChanged: false,
        autoLeveled: false,
        fit: (s) => {
            s.slot.range = [18, 96];
            s.slot.autoLeveled = true;
            s.slot.userRangeChanged = false;
        },
    });
    await sidebar.onSlotAutoClick(0);
    assert.equal(sidebar.slot.autoLeveled, true, "the fit marked the slot levelled");

    sidebar.revertSlotRange(0);
    assert.equal(sidebar.slot.autoLeveled, false, "reverting un-marks it");
    assert.equal(sidebar.slot.userRangeChanged, false,
        "and does not pretend the user set the restored range");
    console.log("Revert restores where the window came from, not only the window");
}

/* ----------------------------------------------------------------- 5 --- */
// Revert is not a manual edit and not a mode: nothing but the range moves.
{
    const sidebar = makeSidebar({
        range: [40, 200],
        userRangeChanged: true,
        fit: (s) => { s.slot.range = [18, 96]; },
    });
    // The window the user last set BY HAND, which is a different thing from
    // the window on screen and must survive an Auto and a Revert untouched.
    sidebar.markerRangeOverrides.set("CD45", [40, 200]);
    const before = JSON.stringify({
        color: sidebar.slot.color,
        colorHex: sidebar.slot.colorHex,
        name: sidebar.slot.name,
        enabled: sidebar.slot.enabled,
        visible: sidebar.slot.visible,
        expanded: sidebar.slot.expanded,
        userColorChanged: sidebar.slot.userColorChanged,
    });

    await sidebar.onSlotAutoClick(0);
    sidebar.revertSlotRange(0);

    assert.equal(JSON.stringify({
        color: sidebar.slot.color,
        colorHex: sidebar.slot.colorHex,
        name: sidebar.slot.name,
        enabled: sidebar.slot.enabled,
        visible: sidebar.slot.visible,
        expanded: sidebar.slot.expanded,
        userColorChanged: sidebar.slot.userColorChanged,
    }), before, "colour, marker, visibility and expansion are untouched");
    assert.deepEqual(sidebar.markerRangeOverrides.get("CD45"), [40, 200],
        "and the remembered manual window is not overwritten by the revert");
    console.log("Revert alters the intensity range and nothing else about the channel");
}

/* ----------------------------------------------------------------- 6 --- */
// A fit that moved nothing -- no stats for the channel, or the marker changed
// under it -- leaves no undo behind. A Revert icon here would be a button
// offering to restore the range already on screen.
{
    const sidebar = makeSidebar({ range: [12, 240], fit: null });
    await sidebar.onSlotAutoClick(0);
    assert.equal(sidebar.slot.preAutoRange, null, "nothing was replaced, so nothing is held");
    assert.equal(sidebar.button.dataset.state, "auto");
    assert.equal(sidebar.button.disabled, false, "and the button is usable again");
    console.log("an Auto that changes nothing leaves no Revert behind");
}

/* ----------------------------------------------------------------- 7 --- */
// The fit takes about a second the first time a channel is levelled. A second
// press during it would read the still-unset `preAutoRange`, start a second
// fit, and store the auto result as the thing to revert TO.
{
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const sidebar = makeSidebar({
        range: [12, 240],
        fit: async (s) => { await held; s.slot.range = [18, 96]; },
    });
    const pressed = sidebar.onSlotAutoClick(0);
    assert.equal(sidebar.button.dataset.state, "busy");
    assert.equal(sidebar.button.disabled, true, "the button is out of reach while it fits");

    release();
    await pressed;
    assert.equal(sidebar.button.disabled, false);
    assert.equal(sidebar.button.dataset.state, "revert");
    assert.deepEqual(sidebar.slot.preAutoRange.range, [12, 240],
        "and what it holds is the window from before the fit");
    console.log("the button cannot be pressed again while the fit is in flight");
}

/* ----------------------------------------------------------------- 8 --- */
// The two numbers are as wide as the domain's digits and no wider. Fixed,
// because a width that tracked the text would resize the track between the
// boxes on the tick where 999 becomes 1000 -- the handle would slide out from
// under the pointer mid-drag.
{
    const widths = [];
    const slider = { el: { style: { setProperty: (n, v) => widths.push([n, v]) } } };
    methods.sizeRangeFields(slider, 255);
    methods.sizeRangeFields(slider, 65535);
    assert.deepEqual(widths, [
        ["--plx-number-width", "calc(3ch + 8px)"],
        ["--plx-number-width", "calc(5ch + 8px)"],
    ], "three characters in the byte domain, five in HD");
    console.log("the inline numbers are sized to the domain, not to their contents");
}

/* ----------------------------------------------------------------- 9 --- */
// Enter takes the field away again, and it is the SLIDER that does it: the
// behaviour belongs to a `.plx-slider.is-plain-numbers`, not to this panel, and
// the gating threshold's line wants exactly the same thing. All this file has
// left to check is that the panel still asks for it -- tests/js/slider_probe.mjs
// owns the behaviour.
{
    assert.ok(/slider\.blurFieldsOnEnter\(\)/.test(source),
        "the contrast window stopped asking the slider to give its numbers back as text");
    console.log("Enter returns a typed number to its text appearance; Escape does not");
}
