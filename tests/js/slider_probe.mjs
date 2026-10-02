/**
 * The one slider, run from source against a hand-made DOM.
 *
 * What is worth testing here is not that a thumb moves. It is the four places
 * a shared primitive can quietly corrupt a value on its way between a panel
 * and a file:
 *
 *   - the step grid, where `0 + 12 * 0.01` is 0.12000000000000001 and that is
 *     the number a panel would persist;
 *   - the two ends of a range, which must never cross and, where a consumer
 *     asks for it, must stay a step apart -- a zero-width window divides by
 *     zero in the thing reading it;
 *   - the log scale, where the handle holds a position on a 1000-step grid
 *     and the value must NOT be read back off it, or a channel window shown
 *     as 1234 saves as 1231.7;
 *   - the two callbacks, because every consumer of this file splits cheap
 *     per-tick work from an expensive commit along exactly that line, and a
 *     primitive that fired `onChange` per pixel would put a hundred entries
 *     in the figure builder's undo history for one drag.
 *
 * No jsdom in this repo, so the DOM below is the subset slider.js touches. It
 * is deliberately strict: `className` is the one store of classes, so a
 * `classList.toggle` that drifted from it would show up here.
 *
 * Run directly:  node tests/js/slider_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/slider.js");

/* ------------------------------------------------------------------ DOM -- */

function makeStyle() {
    const held = new Map();
    return {
        held,
        setProperty(name, value) { held.set(name, String(value)); },
        removeProperty(name) { held.delete(name); },
        getPropertyValue(name) { return held.get(name) ?? ""; },
    };
}

function makeNode(tag) {
    const attributes = {};
    const listeners = new Map();
    const node = {
        tagName: String(tag).toUpperCase(),
        className: "",
        id: "",
        type: "",
        value: "",
        min: "",
        max: "",
        step: "",
        disabled: false,
        hidden: false,
        textContent: "",
        blurs: 0,
        blur() { node.blurs += 1; },
        htmlFor: "",
        parentNode: null,
        childNodes: [],
        attributes,
        style: makeStyle(),
        classList: {
            add(...names) {
                const set = classSet(node);
                for (const name of names) set.add(name);
                writeClasses(node, set);
            },
            remove(...names) {
                const set = classSet(node);
                for (const name of names) set.delete(name);
                writeClasses(node, set);
            },
            toggle(name, on) {
                const set = classSet(node);
                if (on === undefined ? set.has(name) : !on) set.delete(name);
                else set.add(name);
                writeClasses(node, set);
            },
            contains(name) { return classSet(node).has(name); },
        },
        setAttribute(name, value) { attributes[name] = String(value); },
        getAttribute(name) { return attributes[name] ?? null; },
        removeAttribute(name) { delete attributes[name]; },
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        appendChild(child) {
            if (child.parentNode) child.parentNode.childNodes =
                child.parentNode.childNodes.filter((n) => n !== child);
            child.parentNode = node;
            node.childNodes.push(child);
            return child;
        },
        append(...kids) { for (const kid of kids) node.appendChild(kid); },
        insertBefore(child, reference) {
            if (child.parentNode) child.parentNode.childNodes =
                child.parentNode.childNodes.filter((n) => n !== child);
            child.parentNode = node;
            const at = reference ? node.childNodes.indexOf(reference) : -1;
            if (at < 0) node.childNodes.push(child);
            else node.childNodes.splice(at, 0, child);
            return child;
        },
        remove() {
            if (!node.parentNode) return;
            node.parentNode.childNodes =
                node.parentNode.childNodes.filter((n) => n !== node);
            node.parentNode = null;
        },
        get firstChild() { return node.childNodes[0] ?? null; },
        /** Deliver an event the way the browser would. */
        fire(type, init = {}) {
            for (const fn of listeners.get(type) ?? []) fn({ type, ...init });
        },
    };
    return node;
}

const classSet = (node) =>
    new Set(String(node.className).split(/\s+/).filter(Boolean));
const writeClasses = (node, set) => { node.className = [...set].join(" "); };

const context = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Error,
    window: {},
    document: { createElement: (tag) => makeNode(tag) },
});
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "slider.js" });
const PlexoraSlider = context.PlexoraSlider;

/* --------------------------------------------------------------- harness -- */

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/** A slider plus a log of what it told its caller. */
function build(options = {}, mount = null) {
    const inputs = [];
    const changes = [];
    const slider = new PlexoraSlider(mount, {
        ...options,
        onInput: (value, end) => inputs.push({ value, end }),
        onChange: (value, end) => changes.push({ value, end }),
    });
    return { slider, inputs, changes, el: slider.el, nodes: slider.nodes };
}

/** Drag a handle to a position the input would hold, as the browser does:
 *  write the value, then fire `input`. */
function drag(slider, which, held) {
    const input = slider.nodes.inputs[which];
    input.value = String(held);
    input.fire("input");
}

const cssVar = (slider, name) => Number(slider.el.style.getPropertyValue(name));
const fieldText = (slider, which) => slider.nodes.fields[which].input.value;

/** Type into a field and commit it, the way blur or Enter does. */
function type(slider, which, text) {
    const input = slider.nodes.fields[which].input;
    input.fire("focus");
    input.value = text;
    input.fire("input");
    input.fire("change");
}

const near = (a, b, tolerance = 1e-9) => Math.abs(a - b) <= tolerance;

/* ----------------------------------------------------------- the picture -- */

{
    const { slider, el } = build({ min: 0, max: 100, step: 1, value: 25, unit: "%" });
    const classes = (node) => String(node.className).split(/\s+/);
    const track = el.childNodes[0];
    check("a slider is a row of a track and a number box, and no box of its own",
        classes(el).includes("plx-slider") && el.childNodes.length === 2
        && classes(track).includes("plx-slider-track")
        && classes(el.childNodes[1]).includes("plx-number-group"),
        `got ${el.childNodes.map((n) => n.className)}`);
    check("the track holds a rail, a fill and one range input",
        track.childNodes.length === 3
        && classes(track.childNodes[0]).includes("plx-slider-rail")
        && classes(track.childNodes[1]).includes("plx-slider-fill")
        && track.childNodes[2].type === "range",
        `got ${track.childNodes.map((n) => n.className)}`);
    check("a single slider's fill starts at the left edge and never moves",
        cssVar(slider, "--plx-lo") === 0 && near(cssVar(slider, "--plx-hi"), 0.25),
        `lo=${cssVar(slider, "--plx-lo")} hi=${cssVar(slider, "--plx-hi")}`);
    check("the unit is a span beside the box, not text inside it",
        slider.nodes.units.high.textContent === "%"
        && fieldText(slider, "high") === "25",
        `unit=${slider.nodes.units.high.textContent} box=${fieldText(slider, "high")}`);
    check("a range gets two inputs, two boxes and the lower box first",
        (() => {
            const two = build({ mode: "range", min: 0, max: 10, low: 2, high: 8 }).slider;
            return classes(two.el).includes("is-range")
                && two.el.childNodes.length === 3
                && classes(two.el.childNodes[0]).includes("plx-number-group")
                && classes(two.el.childNodes[2]).includes("plx-number-group")
                && two.nodes.track.childNodes.length === 4;
        })(),
        "[ lower ] ---o===o--- [ upper ]");
    check("fields can be turned off without turning off the slider",
        (() => {
            const bare = build({ field: false, min: 0, max: 1, step: 0.1 }).slider;
            return bare.el.childNodes.length === 1 && !bare.nodes.fields.high;
        })());
}

/* -------------------------------------------------------- bounds and step -- */

{
    const above = build({ min: 0, max: 10, step: 1, value: 40 }).slider;
    const below = build({ min: 5, max: 10, step: 1, value: -3 }).slider;
    check("a value outside the extent is clamped at construction, both ways",
        above.get() === 10 && below.get() === 5,
        `${above.get()} / ${below.get()}`);

    const held = build({ min: 0, max: 1, step: 0.01, value: 0.123 }).slider;
    check("a value handed in off the step grid is kept, not snapped",
        held.get() === 0.123 && fieldText(held, "high") === "0.123",
        `${held.get()} shown as ${fieldText(held, "high")}`);

    const dusty = build({ min: 0, max: 1, step: 0.01, value: 0 }).slider;
    drag(dusty, "high", 0 + 12 * 0.01);
    check("a drag lands on the step grid with no float dust",
        dusty.get() === 0.12 && fieldText(dusty, "high") === "0.12",
        `${dusty.get()} shown as ${fieldText(dusty, "high")}`);
    drag(dusty, "high", 0.4471);
    check("a drag off the grid is put back on it",
        dusty.get() === 0.45, String(dusty.get()));

    const tidy = build({ min: 0, max: 1, step: "any", value: 0.1 + 0.2 }).slider;
    check("float dust is taken off a value that is not snapped",
        tidy.get() === 0.3 && fieldText(tidy, "high") === "0.30",
        `${tidy.get()} shown as ${fieldText(tidy, "high")}`);

    const free = build({ min: 0, max: 1, step: "any", value: 0.1234 }).slider;
    check("step 'any' keeps what it was given",
        free.get() === 0.1234, String(free.get()));

    check("decimals follow the step unless they are given",
        PlexoraSlider.decimalsFor(0.01) === 2 && PlexoraSlider.decimalsFor(1) === 0
        && PlexoraSlider.decimalsFor(0.5) === 1 && PlexoraSlider.decimalsFor(20) === 0,
        "0.01 -> 2, 1 -> 0, 0.5 -> 1, 20 -> 0");
    check("format returns something type=number will take back",
        PlexoraSlider.format(1234.5, 2) === "1234.50"
        && PlexoraSlider.format(20, 0) === "20",
        `${PlexoraSlider.format(1234.5, 2)} / ${PlexoraSlider.format(20, 0)}`);
}

/* -------------------------------------------------------- the two events -- */

{
    const { slider, inputs, changes } = build({ min: 0, max: 100, step: 1, value: 0 });
    drag(slider, "high", 30);
    check("a drag tick is one onInput and no onChange",
        inputs.length === 1 && inputs[0].value === 30 && changes.length === 0,
        `${inputs.length} input / ${changes.length} change`);
    check("the tick moved the fill and the box with it",
        near(cssVar(slider, "--plx-hi"), 0.3) && fieldText(slider, "high") === "30",
        `hi=${cssVar(slider, "--plx-hi")} box=${fieldText(slider, "high")}`);
    drag(slider, "high", 31);
    drag(slider, "high", 32);
    check("a hundred ticks are still no commit",
        changes.length === 0, `${changes.length}`);
    slider.nodes.inputs.high.fire("change");
    check("release is the one commit",
        changes.length === 1 && changes[0].value === 32, JSON.stringify(changes));
    check("a single slider reports no end",
        inputs[0].end === undefined && changes[0].end === undefined);
}

{
    const { slider, inputs, changes } = build({ min: 0, max: 100, step: 1, value: 10 });
    slider.set(60);
    check("set() without silent is a commit: both callbacks, once each",
        inputs.length === 1 && changes.length === 1 && slider.get() === 60,
        `${inputs.length} / ${changes.length}`);
    slider.set(20, { silent: true });
    check("a silent set tells nobody",
        inputs.length === 1 && changes.length === 1 && slider.get() === 20,
        `${inputs.length} / ${changes.length}`);
    check("a silent set still repaints and rewrites the box",
        near(cssVar(slider, "--plx-hi"), 0.2) && fieldText(slider, "high") === "20",
        `hi=${cssVar(slider, "--plx-hi")} box=${fieldText(slider, "high")}`);
}

/* -------------------------------------------------------------- the box -- */

{
    const { slider, inputs, changes } = build({ min: 0, max: 20, step: 1, value: 6 });
    type(slider, "high", "13");
    check("a typed number commits once and moves the handle",
        slider.get() === 13 && slider.nodes.inputs.high.value === "13"
        && changes.length === 1,
        `${slider.get()} / ${changes.length} commits`);
    type(slider, "high", "99");
    check("a typed number past the end clamps and the box says so",
        slider.get() === 20 && fieldText(slider, "high") === "20",
        `${slider.get()} shown as ${fieldText(slider, "high")}`);

    const decimal = build({ min: 0, max: 1, step: 0.01, value: 0.5 }).slider;
    type(decimal, "high", "0.377");
    check("a typed number keeps its decimals rather than snapping to the step",
        decimal.get() === 0.377 && fieldText(decimal, "high") === "0.377",
        `${decimal.get()} shown as ${fieldText(decimal, "high")}`);

    // The thresholding bug: a gate whose marker range makes the track step in
    // whole numbers, and a threshold typed with two decimals.
    const whole = build({ mode: "range", min: 0, max: 1000, step: 1, low: 0, high: 1000 });
    type(whole.slider, "low", "7.42");
    check("a typed 7.42 on a step-1 track is 7.42, not 7",
        whole.slider.get()[0] === 7.42 && fieldText(whole.slider, "low") === "7.42"
        && whole.changes.length === 1 && whole.changes[0].value[0] === 7.42,
        `${JSON.stringify(whole.slider.get())} shown as ${fieldText(whole.slider, "low")}`);
    whole.slider.set([12.345, 900], { silent: true });
    check("set() keeps decimals too, so a gate reloaded from disk survives",
        whole.slider.get()[0] === 12.345 && fieldText(whole.slider, "low") === "12.345",
        `${JSON.stringify(whole.slider.get())} shown as ${fieldText(whole.slider, "low")}`);
    check("the box still shows the step's decimals where the value has fewer",
        fieldText(whole.slider, "high") === "900", fieldText(whole.slider, "high"));

    const pct = build({ min: 0, max: 100, step: 1, value: 100, unit: "%", decimals: 0 }).slider;
    type(pct, "high", "37.5");
    check("an opacity typed as 37.5% is 37.5",
        pct.get() === 37.5 && fieldText(pct, "high") === "37.5",
        `${pct.get()} shown as ${fieldText(pct, "high")}`);

    const count = build({ min: 0, max: 40, step: 1, value: 20, integer: true });
    type(count.slider, "high", "20.6");
    check("an integer slider rounds a typed number, and the box says so",
        count.slider.get() === 21 && fieldText(count.slider, "high") === "21"
        && count.changes[0].value === 21,
        `${count.slider.get()} shown as ${fieldText(count.slider, "high")}`);
    count.slider.set(12.2, { silent: true });
    check("and rounds a set value the same way",
        count.slider.get() === 12 && fieldText(count.slider, "high") === "12",
        `${count.slider.get()}`);
    const logCount = build({ mode: "range", scale: "log", min: 1, max: 65535,
                             integer: true, low: 1, high: 65535 }).slider;
    drag(logCount, "low", 500);
    check("an integer log slider holds a whole number after a drag",
        Number.isInteger(logCount.get()[0]), String(logCount.get()[0]));
}

/* ------------------------------------------ an entry is not rewritten -- */

/* The gate panel echoes every preview straight back through `set()`, after
   its own normalising. Before this, "7." came back as "7" under the caret. */
{
    let slider = null;
    slider = new PlexoraSlider(null, {
        mode: "range", min: 0, max: 1000, step: 1, low: 0, high: 1000,
        onInput: (value) => slider?.set(value, { silent: true }),
    });
    const box = slider.nodes.fields.low.input;
    box.fire("focus");
    box.value = "7.";
    box.fire("input");
    check("an echo of a preview does not rewrite the box being typed in",
        box.value === "7.", box.value);
    box.value = "7.42";
    box.fire("input");
    box.fire("change");
    check("and the entry commits as typed",
        slider.get()[0] === 7.42 && box.value === "7.42",
        `${JSON.stringify(slider.get())} shown as ${box.value}`);
    slider.set([3, 1000], { silent: true });
    check("after the commit an outside set lands again",
        box.value === "3", box.value);
}

/* ------------------------------------------------------------- pasting -- */

{
    const parse = PlexoraSlider.parseText;
    check("pasted text: a decimal comma, grouping, a unit and spaces",
        parse("7,42") === 7.42 && parse("1,234") === 1234 && parse("1,234.5") === 1234.5
        && parse("1.234,5") === 1234.5 && parse(" 37.5 % ") === 37.5
        && parse("0,5") === 0.5 && parse("2e-3") === 0.002 && parse("-4.2") === -4.2,
        [parse("7,42"), parse("1,234"), parse("1,234.5"), parse("1.234,5"),
         parse(" 37.5 % "), parse("0,5"), parse("2e-3"), parse("-4.2")].join(" "));
    check("pasted text that is not a number is not one",
        Number.isNaN(parse("abc")) && Number.isNaN(parse("")) && Number.isNaN(parse("1.2.3")));

    const { slider, changes } = build({ min: 0, max: 1000, step: 1, value: 10 });
    const box = slider.nodes.fields.high.input;
    let prevented = 0;
    const paste = (text) => box.fire("paste", {
        clipboardData: { getData: () => text },
        preventDefault: () => { prevented += 1; },
    });
    box.fire("focus");
    paste("7,42");
    check("a paste the browser would refuse is taken over and previewed",
        prevented === 1 && box.value === "7.42" && slider.get() === 7.42
        && changes.length === 0,
        `${box.value} / ${slider.get()} / ${changes.length}`);
    box.fire("blur");
    check("leaving the box commits the pasted number",
        changes.length === 1 && changes[0].value === 7.42, JSON.stringify(changes));
    box.fire("focus");
    paste("12.5");
    check("a plain number is left to the browser, which knows where the caret is",
        prevented === 1, String(prevented));
    paste("not a number");
    check("a paste that is no number at all leaves the box alone",
        prevented === 2 && box.value === "7.42", box.value);
}

/* --------------------------------------------------- a box past the track -- */

/* `fieldMax` is the transcripts panel's point size: a track worth dragging
   through -- 1 to 20 pixels is a dusting to dots that touch -- and a box that
   still has to be able to say 40, because a figure at print size needs a dot
   somebody measured rather than one they dragged to. Everything below is that
   one control: the two bounds are different numbers, the value above the track
   is the typed one and not the pinned one, and the thumb tells the truth about
   itself in the only place it can. */
{
    const { slider, changes } = build(
        { min: 1, max: 20, step: 1, value: 6, fieldMax: 100 });
    const track = slider.nodes.inputs.high;
    const box = slider.nodes.fields.high.input;
    check("the box is bounded by the ceiling and the track by the track",
        box.max === "100" && track.max === "20",
        `box ${box.max} / track ${track.max}`);

    type(slider, "high", "40");
    check("a typed number past the end of the track is kept, not clamped",
        slider.get() === 40 && fieldText(slider, "high") === "40"
        && changes.length === 1,
        `${slider.get()} shown as ${fieldText(slider, "high")}`);
    check("the thumb pins at the end of its travel and the fill fills",
        track.value === "20" && near(cssVar(slider, "--plx-hi"), 1),
        `handle at ${track.value}, fill ${cssVar(slider, "--plx-hi")}`);
    check("aria-valuetext carries the number a pinned thumb cannot report",
        track.getAttribute("aria-valuetext") === "40",
        `${track.getAttribute("aria-valuetext")} -- aria-valuenow can only say 20`);

    type(slider, "high", "1000");
    check("the ceiling is still a ceiling",
        slider.get() === 100 && fieldText(slider, "high") === "100",
        `${slider.get()} -- a typo may not send a consumer somewhere absurd`);

    slider.set(40, { silent: true });
    check("a value above the track survives a silent set, which is a reload",
        slider.get() === 40 && fieldText(slider, "high") === "40",
        `${slider.get()} -- a panel restoring 40 from disk must not show 20`);

    drag(slider, "high", 12);
    check("touching the rail takes the rail's answer",
        slider.get() === 12 && fieldText(slider, "high") === "12",
        `${slider.get()}`);

    const plain = build({ min: 1, max: 20, step: 1, value: 6 }).slider;
    type(plain, "high", "40");
    check("without a ceiling the box clamps at the track, as it always did",
        plain.get() === 20 && plain.nodes.fields.high.input.max === "20",
        `${plain.get()} / box max ${plain.nodes.fields.high.input.max}`);
}

{
    const { slider, inputs, changes } = build({ min: 0, max: 20, step: 1, value: 6 });
    const box = slider.nodes.fields.high.input;
    box.fire("focus");
    box.value = "";
    box.fire("input");
    box.fire("change");
    check("an emptied box puts back what was there and commits nothing",
        slider.get() === 6 && fieldText(slider, "high") === "6"
        && changes.length === 0,
        `${slider.get()} / ${changes.length} commits`);
}

{
    const { slider, inputs, changes } = build({ min: 0, max: 20, step: 1, value: 6 });
    const box = slider.nodes.fields.high.input;
    box.fire("focus");
    box.value = "15";
    box.fire("input");
    check("a keystroke previews: the handle moves, nothing commits",
        slider.get() === 15 && inputs.length === 1 && changes.length === 0,
        `${slider.get()} / ${inputs.length} input / ${changes.length} change`);
    box.fire("keydown", { key: "Escape" });
    check("Escape puts back the number that was there on focus",
        slider.get() === 6 && fieldText(slider, "high") === "6"
        && changes.length === 0,
        `${slider.get()} / ${changes.length} commits`);
}

/* ----------------------------------------------------------- two handles -- */

{
    const { slider, inputs, changes } = build(
        { mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 });
    drag(slider, "low", 90);
    check("the low end cannot be dragged past the high end",
        slider.get()[0] === 80 && slider.nodes.inputs.low.value === "80",
        JSON.stringify(slider.get()));
    check("a range reports which end moved",
        inputs[inputs.length - 1].end === "low", inputs[inputs.length - 1].end);
    drag(slider, "high", 10);
    check("the high end cannot be dragged past the low end",
        slider.get()[1] === 80, JSON.stringify(slider.get()));

    const gapped = build(
        { mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80, minGap: 1 }).slider;
    drag(gapped, "low", 90);
    check("with a minimum gap the two ends are held apart, not met",
        gapped.get()[0] === 79 && gapped.get()[1] === 80,
        JSON.stringify(gapped.get()));
    drag(gapped, "high", 0);
    check("and held apart from the other side too",
        gapped.get()[0] === 79 && gapped.get()[1] === 80,
        JSON.stringify(gapped.get()));

    const met = build({ mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 }).slider;
    drag(met, "low", 100);
    const tops = ["low", "high"].filter(
        (which) => met.nodes.inputs[which].classList.contains("is-top"));
    check("when the two thumbs meet exactly one of them is on top",
        tops.length === 1 && tops[0] === "low",
        `${tops} -- at the right-hand end it is the low handle that can still move`);

    const typed = build({ mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 });
    type(typed.slider, "low", "95");
    check("a typed low end past the high end clamps and commits once",
        typed.slider.get()[0] === 80 && typed.changes.length === 1
        && typed.changes[0].end === "low",
        `${JSON.stringify(typed.slider.get())} / ${typed.changes.length}`);
    check("a range hands its caller a pair, not a number",
        Array.isArray(typed.changes[0].value) && typed.changes[0].value.length === 2,
        JSON.stringify(typed.changes[0].value));
}

/* ------------------------------------------------------------------ log -- */

{
    const { slider, inputs } = build(
        { mode: "range", scale: "log", min: 1, max: 65535, step: 1, low: 1, high: 65535 });
    check("a log handle holds a position, not a value",
        slider.nodes.inputs.low.min === "0"
        && slider.nodes.inputs.low.max === String(PlexoraSlider.LOG_STEPS)
        && slider.nodes.inputs.low.step === "1",
        `${slider.nodes.inputs.low.min}..${slider.nodes.inputs.low.max}`);

    slider.set([100, 5000], { silent: true });
    check("a set value survives the round trip through the position exactly",
        slider.get()[0] === 100 && slider.get()[1] === 5000,
        `${JSON.stringify(slider.get())} -- a panel showing 1234 must not save 1231.7`);
    check("and the box shows the value, not the position",
        fieldText(slider, "low") === "100.00", fieldText(slider, "low"));

    const grid = Math.pow(65535, 1 / PlexoraSlider.LOG_STEPS) - 1;
    const held = Number(slider.nodes.inputs.low.value);
    drag(slider, "low", held);
    check("a drag lands within one grid step of where the value was",
        Math.abs(slider.get()[0] - 100) / 100 <= grid * 1.001,
        `${slider.get()[0]} vs 100, grid is ${(grid * 100).toFixed(3)}%`);
    check("a log handle spells the real number out for a screen reader",
        slider.nodes.inputs.low.getAttribute("aria-valuetext") !== null,
        `aria-valuenow would announce the position`);

    const half = build({ scale: "log", min: 1, max: 100, step: 1, value: 10 }).slider;
    check("the fill sits where the thumb is, on a log scale too",
        near(cssVar(half, "--plx-hi"), 0.5, 1e-3), String(cssVar(half, "--plx-hi")));
}

/* --------------------------------------------------------------- bounds -- */

{
    const { slider, inputs, changes } = build(
        { mode: "range", min: 0, max: 100, step: 1, low: 10, high: 90 });
    slider.setBounds({ min: 0, max: 50 });
    check("a new extent re-clamps the values it no longer contains",
        slider.get()[1] === 50 && slider.nodes.inputs.high.max === "50",
        JSON.stringify(slider.get()));
    check("a new extent is the app talking, so it tells nobody",
        inputs.length === 0 && changes.length === 0,
        `${inputs.length} / ${changes.length}`);
    check("the boxes learn the new extent as well",
        slider.nodes.fields.high.input.max === "50",
        slider.nodes.fields.high.input.max);
    slider.setBounds({ min: 7, max: 7 });
    check("an empty extent disables the control rather than drawing a lie",
        slider.el.classList.contains("is-disabled")
        && slider.nodes.inputs.high.disabled === true,
        "a constant column has no window to set");
    slider.setBounds({ min: 0, max: 10 });
    check("and a real extent brings it back",
        !slider.el.classList.contains("is-disabled")
        && slider.nodes.inputs.high.disabled === false);
}

{
    const { slider } = build({ min: 0, max: 10, step: 1, value: 5 });
    slider.setDisabled(true);
    check("disabling reaches the input and the box",
        slider.el.classList.contains("is-disabled")
        && slider.nodes.inputs.high.disabled === true
        && slider.nodes.fields.high.input.disabled === true);
    slider.setDisabled(false);
    check("and undisabling reaches both back",
        !slider.el.classList.contains("is-disabled")
        && slider.nodes.inputs.high.disabled === false
        && slider.nodes.fields.high.input.disabled === false);

    slider.setUnit("µm");
    check("a unit can be set after the fact",
        slider.nodes.units.high.textContent === "µm"
        && slider.nodes.units.high.hidden === false);
    slider.setUnit("");
    check("and taken away again, hiding the span rather than leaving a gap",
        slider.nodes.units.high.hidden === true);
}

/* ------------------------------------------------- identity and adoption -- */

{
    const { slider } = build({
        min: 0, max: 1, step: 0.01, value: 0.5,
        id: "adjust_gamma", fieldId: "adjust_gamma_value", label: "Gamma",
    });
    check("a single slider keeps the id the template gave it and labels it",
        slider.nodes.inputs.high.id === "adjust_gamma"
        && slider.nodes.fields.high.input.id === "adjust_gamma_value"
        && slider.el.childNodes[0].htmlFor === "adjust_gamma",
        `${slider.nodes.inputs.high.id} / ${slider.el.childNodes[0].htmlFor}`);

    const two = build({
        mode: "range", min: 0, max: 1, low: 0, high: 1, label: "Contrast window",
        ids: { low: "slot_min_0", high: "slot_max_0" },
        fieldIds: { low: "slot_min_value_0", high: "slot_max_value_0" },
        ariaLabels: ["Lower", "Upper"],
    }).slider;
    check("a range keeps four ids and names each end for a screen reader",
        two.nodes.inputs.low.id === "slot_min_0"
        && two.nodes.fields.high.input.id === "slot_max_value_0"
        && two.nodes.inputs.low.getAttribute("aria-label") === "Lower"
        && two.nodes.inputs.high.getAttribute("aria-label") === "Upper");
    check("two inputs cannot share one <label for>, so the row is a group",
        two.el.getAttribute("role") === "group"
        && two.el.getAttribute("aria-label") === "Contrast window");

    const tinted = build({ min: 0, max: 1, accent: "var(--accent-gate)", fieldWidth: 40 }).slider;
    check("accent and field width land as custom properties, not as rules",
        tinted.el.style.getPropertyValue("--plx-slider-accent") === "var(--accent-gate)"
        && tinted.el.style.getPropertyValue("--plx-number-width") === "40px");
}

{
    // The eleven template-staged sliders are adopted, not replaced: the id a
    // golden records, the `<label for>` pointing at it and the attributes a
    // test matches all stay exactly where they were written.
    const parent = makeNode("div");
    const staged = makeNode("input");
    staged.type = "range";
    staged.id = "transcripts_size";
    staged.min = "1";
    staged.max = "20";
    staged.step = "1";
    staged.value = "6";
    parent.appendChild(staged);
    const sibling = makeNode("span");
    parent.appendChild(sibling);

    const { slider } = build({ fieldId: "transcripts_size_value" }, staged);
    check("adoption reads the extent off the element it found",
        slider.min === 1 && slider.max === 20 && slider.get() === 6,
        `${slider.min}..${slider.max} at ${slider.get()}`);
    check("adoption keeps the staged id and its place in the row",
        slider.nodes.inputs.high === staged && staged.id === "transcripts_size"
        && parent.childNodes[0] === slider.el && parent.childNodes[1] === sibling,
        `${parent.childNodes.map((n) => n.className || n.tagName)}`);
    check("the adopted element ends up inside the track",
        staged.parentNode === slider.nodes.track
        && String(staged.className).includes("plx-range"),
        staged.className);
}

/* ------------------------------------------------- pointer versus keyboard -- */

{
    const { slider } = build({ min: 0, max: 10, step: 1, value: 5 });
    const input = slider.nodes.inputs.high;
    input.fire("pointerdown");
    check("a pointer press marks the drag and marks the focus as the mouse's",
        input.classList.contains("is-dragging")
        && input.classList.contains("is-pointer-focus"),
        "the CSS hides the keyboard ring while that second class is on");
    input.fire("pointerup");
    check("letting go ends the drag but leaves the focus as the mouse's",
        !input.classList.contains("is-dragging")
        && input.classList.contains("is-pointer-focus"),
        "clearing it here would redraw the very rectangle this replaces");
    input.fire("keydown", { key: "ArrowRight" });
    check("an arrow key hands focus back to the keyboard, and the ring with it",
        !input.classList.contains("is-pointer-focus"));
    input.fire("pointerdown");
    input.fire("blur");
    check("blur clears both",
        !input.classList.contains("is-dragging")
        && !input.classList.contains("is-pointer-focus"));
}

/* ------------------------------------------------------------ standalone -- */

{
    const commits = [];
    const previews = [];
    const field = PlexoraSlider.numberField({
        id: "transcripts_bin_value", min: 1, max: 400, decimals: 0, unit: "µm",
        value: 20,
        constrain: (v) => Math.min(400, Math.max(1, Math.round(v))),
        onInput: (v) => previews.push(v),
        onCommit: (v) => commits.push(v),
    });
    check("the box can be had without a track",
        field.input.className === "plx-number"
        && field.el.className === "plx-number-group"
        && field.input.value === "20" && field.unit.textContent === "µm");
    check("the box takes any number, so that 0.37 can be typed into a 0.5 grid",
        field.input.step === "any", field.input.step);
    field.input.fire("focus");
    field.input.value = "25";
    field.input.fire("input");
    check("a keystroke in a standalone box previews too",
        previews.length === 1 && commits.length === 0);
    field.input.fire("change");
    check("and blur commits it once",
        commits.length === 1 && commits[0] === 25 && field.get() === 25,
        JSON.stringify(commits));
    field.input.fire("focus");
    field.input.value = "900";
    field.input.fire("change");
    check("a standalone box clamps through the constraint it was given",
        field.get() === 400 && field.input.value === "400", field.input.value);
}

/* ------------------------------------------- numbers that read as text -- */

/* Every slider's boxes are drawn as plain text until they are clicked
   (`.plx-slider .plx-number`), so every one has to give the text back when
   the entry is over. The field already commits on Enter, but it keeps focus,
   and focus is the entire difference between a number that reads as a label
   and one that reads as an input. Only Enter: Escape is the field's own, and
   blurring on it too would take the box away from somebody restarting their
   entry.

   With no modifier and no method call: this used to be an opt-in two callers
   asked for, and every other slider drew a box that never went away. */
{
    const slider = new PlexoraSlider(null, {
        mode: "range", min: 0, max: 255, low: 10, high: 200,
    });
    check("returning the boxes to text is the slider's own, with nothing to ask",
        typeof slider.blurFieldsOnEnter === "undefined");
    for (const which of ["low", "high"]) {
        const input = slider.nodes.fields[which].input;
        input.fire("keydown", { key: "ArrowUp" });
        check(`${which}: an arrow key keeps the field open`, input.blurs === 0);
        input.fire("keydown", { key: "Escape" });
        check(`${which}: Escape is the field's own, and keeps it`, input.blurs === 0);
        input.fire("keydown", { key: "Enter" });
        check(`${which}: Enter gives the number back as text`, input.blurs === 1);
    }
}

/* ------------------------------------------------ fields somewhere else -- */

/* Two boxes and the gaps around them are about a third of a 300px sidebar, so
   inline they leave the track too short to aim with. The channel contrast
   window and the gate both hand the slider a row of their own -- the one that
   already carries their Auto button -- and keep the whole of the slider's line
   for the track. The boxes are still the slider's: same clamp, same commit. */
{
    const header = makeNode("div");
    const { slider } = build({
        mode: "range", min: 0, max: 100, low: 10, high: 90, fieldsSlot: header,
    });
    check("fields can be built into a row the caller owns",
        header.childNodes.length === 2
            && header.childNodes[0] === slider.nodes.fields.low.el
            && header.childNodes[1] === slider.nodes.fields.high.el,
        `${header.childNodes.length} nodes in the slot`);
    check("and then the slider's own row is nothing but the track",
        slider.el.childNodes.length === 1
            && slider.el.childNodes[0] === slider.nodes.track,
        `${slider.el.childNodes.length} nodes in the row`);

    slider.nodes.fields.low.input.value = "40";
    slider.nodes.fields.low.input.fire("change");
    check("a relocated box still drives the handle it belongs to",
        slider.get()[0] === 40 && slider.nodes.inputs.low.value === "40",
        `${slider.get()}`);

    slider.destroy();
    check("destroy takes the relocated boxes out of the caller's row too",
        header.childNodes.length === 0,
        "left behind they would go on listening inside somebody else's row");
}

/* ------------------------------------------ destroy gives back what it took -- */

/* ADOPTION IS A LOAN. The `<input type="range">` belongs to the template: it
   carries the id a `<label for>` points at, a golden file records it, and
   `bindSlider` looks it up again on the next bind. Adoption moves it inside
   the slider's root, so a destroy that just drops the root takes the page's
   own markup with it -- and the next bind finds nothing, returns early, and
   the control is gone for good with nothing in the console.

   That is not hypothetical. It is what emptied three rows of the transcripts
   panel: `paintTree()` destroyed the sliders on every gene-list rebuild, and
   the rebuild runs on load. */
{
    const row = makeNode("div");
    const label = makeNode("label");
    const staged = makeNode("input");
    staged.type = "range"; staged.id = "transcripts_size";
    staged.min = "1"; staged.max = "20"; staged.step = "1"; staged.value = "6";
    row.appendChild(label);
    row.appendChild(staged);

    const slider = new PlexoraSlider(staged, { fieldId: "transcripts_size_value" });
    check("adoption puts the root where the element stood",
        row.childNodes.length === 2 && row.childNodes[1] === slider.el
            && slider.nodes.track.childNodes.includes(staged),
        `${row.childNodes.length} in the row`);

    slider.destroy();
    check("destroy hands the adopted element back to the page",
        row.childNodes.length === 2 && row.childNodes[1] === staged,
        `row now holds [${row.childNodes.map((n) => n.tagName || n.type).join(", ")}]`);
    check("and the slider's own root is gone with it",
        !row.childNodes.includes(slider.el),
        "the borrowed input survives; everything the slider made does not");
    check("so the panel can find its element and bind again",
        staged.id === "transcripts_size" && staged.value === "6",
        "id and staged value intact, which is what the next bind needs");
}

/* ---------------------------------------------------- display at rest -- */

// `display` is what a box says while nobody is in it, and nothing else: the
// gate typed as 7.428173 reads 7.43 at rest, every decimal on focus, and the
// value held, emitted and committed is never the short one.
{
    const doc = context.document;
    const short = (v) => v.toFixed(2);
    const { slider, inputs, changes } = build({
        mode: "range", min: 0, max: 10, step: 0.01, low: 1, high: 9,
        format: (v) => PlexoraSlider.show(v, 2), display: short,
    });
    const box = slider.nodes.fields.low.input;

    doc.activeElement = box;
    box.fire("focus");
    box.value = "7.428173";
    box.fire("input");
    box.fire("change");
    doc.activeElement = null;
    box.fire("blur");
    check("a typed gate keeps every decimal it was typed with",
        slider.get()[0] === 7.428173 && changes.at(-1)?.value[0] === 7.428173,
        `held ${slider.get()[0]}, committed ${changes.at(-1)?.value[0]}`);
    check("at rest the box shows two decimals",
        box.value === "7.43", `box reads ${box.value}`);

    doc.activeElement = box;
    box.fire("focus");
    check("clicked, the box shows the whole stored number",
        box.value === "7.428173", `box reads ${box.value}`);
    check("and widens to hold it while it is being edited",
        /8ch/.test(box.style.getPropertyValue("width")), `width ${box.style.getPropertyValue("width")}`);
    const before = changes.length;
    doc.activeElement = null;
    box.fire("blur");
    check("leaving it untouched commits nothing and puts the short form back",
        changes.length === before && box.value === "7.43" && slider.get()[0] === 7.428173,
        `${changes.length - before} commits, box ${box.value}`);
    check("and gives the width back",
        !box.style.getPropertyValue("width"), `width ${box.style.getPropertyValue("width")}`);

    drag(slider, "high", 8.5);
    check("a drag writes the short form into a box nobody is in",
        fieldText(slider, "high") === "8.50" && inputs.at(-1)?.value[1] === 8.5,
        `box ${fieldText(slider, "high")}`);

    const plain = build({ min: 0, max: 10, step: 0.01, value: 1 }).slider;
    type(plain, "high", "7.428173");
    check("without display a box is unchanged: it always shows the whole number",
        fieldText(plain, "high") === "7.428173", `box ${fieldText(plain, "high")}`);
    delete doc.activeElement;
}

/* ------------------------------------------------ one step from a button -- */

{
    // An arrow key on a focused handle moves the input by its step, which the
    // browser then reports as `input` and `change`. nudge() is that, for a
    // button standing in for the key -- so each case below is held against
    // what the key itself would have done.
    const { slider, inputs, changes } = build(
        { mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 });
    const moved = slider.nudge("low", 1);
    check("a nudge moves one step, the way an arrow key does",
        moved === true && slider.get()[0] === 21 && slider.get()[1] === 80
        && inputs.length === 1 && changes.length === 1
        && inputs[0].end === "low" && changes[0].end === "low"
        && slider.nodes.inputs.low.value === "21" && fieldText(slider, "low") === "21",
        `low ${slider.get()[0]}, ${inputs.length} ticks, ${changes.length} commits`);
    const keyed = build({ mode: "range", min: 0, max: 100, step: 1, low: 21, high: 80 }).slider;
    drag(keyed, "low", 20);
    keyed.nodes.inputs.low.fire("change");
    slider.nudge("low", -1);
    check("and down is the same step the other way",
        slider.get()[0] === 20 && keyed.get()[0] === 20, `nudged ${slider.get()[0]}, keyed ${keyed.get()[0]}`);

    const typed = build({ mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 });
    typed.slider.set([7.42, 80], { silent: true });
    typed.slider.nudge("low", 1);
    check("a nudge from off the grid lands on it",
        typed.slider.get()[0] === 8, `got ${typed.slider.get()[0]} -- the browser holds 7.42 as 7, and steps to 8`);

    const floor = build({ mode: "range", min: 0, max: 100, step: 1, low: 0, high: 80 });
    const atFloor = floor.slider.nudge("low", -1);
    check("at the end of its travel a nudge does nothing and says so",
        atFloor === false && floor.slider.get()[0] === 0
        && !floor.inputs.length && !floor.changes.length,
        `returned ${atFloor}, ${floor.inputs.length} ticks, ${floor.changes.length} commits`);

    const pinned = build({ mode: "range", min: 0, max: 100, step: 1, low: 79, high: 80 });
    const first = pinned.slider.nudge("low", 1);
    const second = pinned.slider.nudge("low", 1);
    check("the low end nudged into the high end stops there and then stays",
        first === true && second === false && pinned.slider.get()[0] === 80
        && pinned.slider.get()[1] === 80 && pinned.changes.length === 1,
        `${first}/${second}, low ${pinned.slider.get()[0]}, ${pinned.changes.length} commits`);

    const fine = build({ mode: "range", min: 0, max: 1, step: 0.01, low: 0.12, high: 0.9 }).slider;
    fine.nudge("low", 1);
    check("a decimal step lands without float dust",
        fine.get()[0] === 0.13, String(fine.get()[0]));

    const off = build({ mode: "range", min: 0, max: 100, step: 1, low: 20, high: 80 });
    off.slider.setDisabled(true);
    const refused = off.slider.nudge("low", 1);
    check("a disabled slider ignores a nudge",
        refused === false && off.slider.get()[0] === 20 && !off.inputs.length && !off.changes.length,
        `returned ${refused}, low ${off.slider.get()[0]}`);

    const free = build({ mode: "range", min: 0, max: 1, step: "any", low: 0.2, high: 0.8 });
    check("a step 'any' slider has no grid to step along",
        free.slider.nudge("low", 1) === false && free.slider.get()[0] === 0.2 && !free.changes.length);

    const log = build({ mode: "range", scale: "log", min: 1, max: 65535, step: 1, low: 100, high: 5000 }).slider;
    const position = Number(log.nodes.inputs.low.value);
    const before = log.get()[0];
    log.nudge("low", 1);
    check("on a log scale a nudge is one position of the grid",
        Number(log.nodes.inputs.low.value) === position + 1 && log.get()[0] > before,
        `position ${position} -> ${log.nodes.inputs.low.value}, value ${before} -> ${log.get()[0]}`);
}

/* ---------------------------------------------------------------- teardown -- */

{
    const parent = makeNode("div");
    const { slider } = build({ min: 0, max: 1 }, parent);
    check("a mount gets the slider appended to it",
        parent.childNodes.length === 1 && parent.childNodes[0] === slider.el);
    slider.destroy();
    check("destroy takes the whole control out of the document",
        parent.childNodes.length === 0,
        "every listener is on a node inside it, so there is nothing else to undo");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
