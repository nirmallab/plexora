/**
 * A file dropped on a path field in the desktop app (services/browsePicker.js
 * attachDropTarget), against a stubbed page.
 *
 * What has to hold:
 *
 *   - **Nothing in a browser.** A browser drop is bytes, never a path, so a
 *     browser tab's path boxes are exactly what they were.
 *   - **The box under the pointer lights, and only while the file is one it
 *     would take.** A file it would not take lights it refused; a drag of
 *     several over a one-path box lights nothing, because that drop belongs
 *     to the Import dialog.
 *   - **A drop fills the box the way Browse does** -- value, then input, keyup
 *     and change -- and is claimed, so the Import dialog stays shut.
 *   - **A file from this computer goes into a box set to This computer.** A
 *     field with a Local/Remote handle is moved back; one with its own switch
 *     set to Remote says why it will not take it.
 *   - **A modal shuts out the page under it**, for drops as for clicks.
 *   - **A drop walks outward past a zone that is not its target** -- several
 *     files over a one-path row go to the dialog around it -- and stops at a
 *     zone that refuses, or that says "not mine" (`accepts` -> false) for
 *     the zone around it to take.
 *   - **The label beside the pointer** says what the drop will do, from the
 *     zone's `hint` and the plan (`provideDropPlan`), asked once per drag.
 *
 * Run directly:  node tests/js/path_drop_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SRC = join(REPO, "plexora/client/src/js/services");
const TYPES = readFileSync(join(SRC, "explorer/explorerTypes.js"), "utf8");
const PICKER = readFileSync(join(SRC, "browsePicker.js"), "utf8");

let failures = 0;
function check(label, condition) {
    if (condition) {
        console.log(`ok - ${label}`);
    } else {
        failures += 1;
        console.log(`FAIL - ${label}`);
    }
}

class Event {
    constructor(type, init = {}) {
        this.type = type;
        this.bubbles = Boolean(init.bubbles);
    }
}

class El {
    constructor(tag, { classes = [], parent = null } = {}) {
        this.tagName = tag.toUpperCase();
        this.cls = new Set(classes);
        this.attrs = {};
        this.parentElement = parent;
        this.fired = [];
        this.listeners = {};
        this.value = "";
        this.disabled = false;
        this.readOnly = false;
        this.isModal = false;
        this.open = false;
        this.style = {};
        this.dataset = {};
        this.textContent = "";
        this.offsetWidth = 120;
        this.offsetHeight = 22;
        this.id = "";
        this.children = [];
    }
    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        if (child.id && this.registry) this.registry[child.id] = child;
        return child;
    }
    get classList() {
        const set = this.cls;
        return {
            add: (...names) => names.forEach((name) => set.add(name)),
            remove: (...names) => names.forEach((name) => set.delete(name)),
            contains: (name) => set.has(name),
            toggle: (name, on) => {
                const want = on === undefined ? !set.has(name) : Boolean(on);
                if (want) set.add(name); else set.delete(name);
                return want;
            },
        };
    }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    removeAttribute(name) { delete this.attrs[name]; }
    hasAttribute(name) { return name in this.attrs; }
    is(selector) {
        if (selector === "[data-plx-drop]") return this.hasAttribute("data-plx-drop");
        if (selector.startsWith(".")) return this.cls.has(selector.slice(1));
        return false;
    }
    matches(selector) { return selector === ":modal" ? this.isModal : this.is(selector); }
    closest(selector) {
        for (let node = this; node; node = node.parentElement) {
            if (node.is(selector)) return node;
        }
        return null;
    }
    contains(other) {
        for (let node = other; node; node = node.parentElement) {
            if (node === this) return true;
        }
        return false;
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    dispatchEvent(event) { this.fired.push(event.type); return true; }
}

function makeWorld({ desktop = true } = {}) {
    const html = new El("html");
    const body = new El("body", { parent: html });
    const byId = {};
    body.registry = byId;
    const listeners = {};
    const dialogs = [];
    const toasts = [];
    let pointed = body;
    const document = {
        documentElement: html,
        body,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        dispatchEvent(event) {
            (listeners[event.type] || []).forEach((fn) => fn(event));
            return !event.defaultPrevented;
        },
        elementFromPoint: () => pointed,
        getElementById: (id) => byId[id] || null,
        createElement: (tag) => new El(tag),
        querySelectorAll: (selector) => (selector === "dialog[open]"
            ? dialogs.filter((dialog) => dialog.open) : []),
    };
    const handles = new Map();
    const window = {
        document,
        setTimeout,
        PlexoraDesktop: desktop ? { platform: "windows" } : null,
        PlexoraToast: { show: (options) => toasts.push(options) },
        PlexoraDataLocation: { handleOf: (input) => handles.get(input) || null },
        innerWidth: 1000,
        innerHeight: 800,
    };
    window.window = window;
    const context = createContext({
        window, document, Event, console, setTimeout, Intl,
    });
    runInContext(TYPES, context);
    runInContext(PICKER, context);

    function send(type, detail) {
        const event = {
            type, detail, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
        };
        document.dispatchEvent(event);
        return event;
    }

    return {
        context, document, html, body, dialogs, toasts, handles, listeners,
        el: (tag, options = {}) => new El(tag, { parent: body, ...options }),
        /** A field the way every core form draws one: row > input + button. */
        field(options = {}, { parent = body, rowClass = "import-field-row" } = {}) {
            const row = new El("div", { classes: [rowClass], parent });
            const input = new El("input", { classes: ["form-control"], parent: row });
            const button = new El("button", { classes: ["browse-button"], parent: row });
            context.attachBrowseButton(button, input, options);
            return { row, input, button };
        },
        point(element) { pointed = element; },
        drag(phase, paths, at, x = 1, y = 1) {
            if (at) pointed = at;
            return send("plexora:native-drag", { phase, paths, x, y });
        },
        hint: () => byId.plx_drop_hint || null,
        drop(paths, at) {
            if (at) pointed = at;
            return send("plexora:native-drop", { paths, x: 1, y: 1 });
        },
    };
}

const SLIDE = "C:\\data\\slide.ome.tif";
const TABLE = "C:\\data\\cells.csv";

// -- outside the app -----------------------------------------------------------

{
    const world = makeWorld({ desktop: false });
    const zone = world.el("div");
    const detach = world.context.attachDropTarget(zone, { onDrop: () => {} });
    const { row } = world.field({ mode: "any", filter: "image" });
    check("outside the app attachDropTarget attaches nothing",
          !zone.hasAttribute("data-plx-drop") && !row.hasAttribute("data-plx-drop")
          && !world.listeners["plexora:native-drop"] && typeof detach === "function");
}

// -- a field in the app ----------------------------------------------------------

{
    const world = makeWorld();
    const { row, input } = world.field({ mode: "any", filter: "image" });
    check("attachBrowseButton makes the field's row a drop zone", row.hasAttribute("data-plx-drop"));

    world.drag("enter", [SLIDE], input);
    check("a box under the pointer lights while one file is dragged over it",
          row.cls.has("is-drop-over") && world.html.cls.has("has-drop-target"));
    world.drag("over", [], world.body);
    check("and unlights when the drag moves off it",
          !row.cls.has("is-drop-over") && !world.html.cls.has("has-drop-target"));
    world.drag("over", [], input);
    check("an over phase with no paths keeps what the enter phase brought",
          row.cls.has("is-drop-over"));
    world.drag("leave", [], null);
    check("and when the drag leaves the window",
          !row.cls.has("is-drop-over") && !world.html.cls.has("has-drop-target"));

    world.drag("enter", [SLIDE], input);
    const dropped = world.drop([SLIDE], input);
    check("one path dropped on a box fills it and fires input, keyup and change",
          input.value === SLIDE && ["input", "keyup", "change"].every((t) => input.fired.includes(t)));
    check("and the drop is claimed so the Import dialog stays shut", dropped.defaultPrevented);
    check("the drop unlights the box it filled", !row.cls.has("is-drop-over"));

    input.value = "";
    world.drag("enter", [SLIDE, TABLE], input);
    const both = world.drop([SLIDE, TABLE], input);
    check("two paths over a one-box field light nothing and are not claimed",
          !row.cls.has("is-drop-over") && !both.defaultPrevented && input.value === "");

    world.drag("enter", [TABLE], input);
    const lit = row.cls.has("is-drop-refused") && !row.cls.has("is-drop-over");
    const table = world.drop([TABLE], input);
    check("a suffix the box rejects lights it refused and the drop says why",
          lit && table.defaultPrevented && input.value === ""
          && world.toasts.length === 1 && /takes an image/.test(world.toasts[0].note));

    const run = "C:\\runs\\xenium_output\\";
    world.drop([run], input);
    check("a name without a suffix is taken", input.value === run);

    world.drag("enter", [SLIDE], input);
    world.drop([SLIDE], world.body);
    check("the drop unlights the box whether or not it was claimed",
          !row.cls.has("is-drop-over") && !world.html.cls.has("has-drop-target"));

    input.value = "";
    input.disabled = true;
    const busy = world.drop([SLIDE], input);
    check("a box that cannot be typed in lets the drop through",
          !busy.defaultPrevented && input.value === "");
}

// -- what kind of box ------------------------------------------------------------

{
    const world = makeWorld();
    const { input } = world.field({ mode: "directory" }, { rowClass: "settings-input-row" });
    const file = world.drop(["C:\\data\\slide.tif"], input);
    const refused = file.defaultPrevented && input.value === ""
        && /folder/.test(world.toasts[0]?.note || "");
    world.drop(["D:\\plexora-data"], input);
    check("a folder box refuses a file name", refused && input.value === "D:\\plexora-data");

    const csv = world.field({ mode: "any", filter: "data" });
    world.drop(["C:\\data\\sample.ome.zarr"], csv.input);
    check("a store folder is taken by a box that reads stores",
          csv.input.value === "C:\\data\\sample.ome.zarr");
}

// -- modals ----------------------------------------------------------------------

{
    const world = makeWorld();
    const under = world.field({ mode: "any", filter: "image" });
    const dialog = world.el("dialog");
    dialog.open = true;
    dialog.isModal = true;
    world.dialogs.push(dialog);
    const inside = world.field({ mode: "any", filter: "image" }, { parent: dialog });
    const missed = world.drop([SLIDE], under.input);
    world.drop([SLIDE], inside.input);
    check("with a modal open only boxes inside it count",
          !missed.defaultPrevented && under.input.value === "" && inside.input.value === SLIDE);
}

// -- which machine -----------------------------------------------------------------

{
    const world = makeWorld();
    const steps = [];
    const { input } = world.field({ mode: "any", filter: "data", node: () => "hpc" });
    world.handles.set(input, {
        isLocal: () => false,
        setWhere: (where) => { steps.push(`where:${where}`); input.value = ""; },
    });
    input.dispatchEvent = (event) => { steps.push(event.type); return true; };
    world.drop([TABLE], input);
    check("a box set to another machine is switched to Local before it is filled",
          steps[0] === "where:local" && input.value === TABLE && steps.includes("change"));

    const own = world.field({ mode: "file", filter: "channels", node: () => "hpc" });
    const refused = world.drop(["C:\\data\\markers.csv"], own.input);
    check("a box with its own Remote switch set to Remote refuses with a reason",
          refused.defaultPrevented && own.input.value === ""
          && /another machine/.test(world.toasts.at(-1)?.note || ""));
}

// -- a zone that takes several ------------------------------------------------------

{
    const world = makeWorld();
    const page = world.el("div");
    const card = world.el("section", { parent: page });
    const heard = [];
    let got = null;
    world.context.attachDropTarget(page, {
        filter: "any", mode: "any", count: "many",
        onOver: (lit) => heard.push(lit),
        onDrop: (paths) => { got = paths; },
    });
    world.drag("enter", [SLIDE, TABLE], card);
    world.drag("leave", [], null);
    check("onOver hears the zone light and go out", heard.join() === "true,false");
    const many = world.drop([SLIDE, TABLE], card);
    check("a zone that takes several is handed every path",
          many.defaultPrevented && Array.isArray(got) && got.length === 2);
}

// -- walking outward -----------------------------------------------------------

{
    const world = makeWorld();
    const dialog = world.el("dialog");
    let got = null;
    world.context.attachDropTarget(dialog, {
        filter: "any", mode: "any", count: "many",
        onDrop: (paths) => { got = paths; },
    });
    const { row, input } = world.field({ mode: "any", filter: "any" }, { parent: dialog });
    world.drag("enter", [SLIDE, TABLE], input);
    const lit = dialog.cls.has("is-drop-over") && !row.cls.has("is-drop-over");
    const both = world.drop([SLIDE, TABLE], input);
    check("several files over a one-path row light and go to the zone around it",
          lit && both.defaultPrevented && Array.isArray(got) && got.length === 2
          && input.value === "");

    got = null;
    world.drop([SLIDE], input);
    check("one file over the row is still the row's", input.value === SLIDE && got === null);
}

{
    const world = makeWorld();
    const viewer = world.el("div");
    const panel = world.el("section", { parent: viewer });
    let viewerGot = null;
    let panelGot = null;
    world.context.attachDropTarget(viewer, {
        filter: "any", mode: "any", count: "many",
        onDrop: (paths) => { viewerGot = paths; },
    });
    world.context.attachDropTarget(panel, {
        filter: "any", mode: "file", count: "one",
        accepts: (path, name) => /\.geojson$/i.test(name),
        onDrop: (path) => { panelGot = path; },
    });
    world.drop([SLIDE], panel);
    check("a zone that says a file is not its kind passes it to the zone around it",
          panelGot === null && Array.isArray(viewerGot) && viewerGot[0] === SLIDE);
    world.drop(["C:\\data\\regions.geojson"], panel);
    check("and keeps the files that are", /regions/.test(panelGot || ""));
}

{
    const world = makeWorld();
    const dialog = world.el("dialog");
    world.context.attachDropTarget(dialog, {
        filter: "any", mode: "file", count: "one",
        several: "Drop one file.",
        onDrop: () => {},
    });
    const event = world.drop([SLIDE, TABLE], dialog);
    check("a one-file zone with `several` refuses a drag of several with its reason",
          event.defaultPrevented && /one file/.test(world.toasts.at(-1)?.note || ""));
}

{
    const world = makeWorld();
    const { row } = world.field({ mode: "any", filter: "any", drop: false });
    check("drop: false leaves the row unregistered", !row.hasAttribute("data-plx-drop"));
}

// -- the label and the plan ------------------------------------------------------

{
    const world = makeWorld();
    const viewer = world.el("div");
    let asked = 0;
    let resolve = null;
    world.context.provideDropPlan(() => {
        asked += 1;
        return new Promise((done) => { resolve = done; });
    });
    let dropped = null;
    world.context.attachDropTarget(viewer, {
        filter: "any", mode: "any", count: "many",
        hint: (paths, { plan }) => (plan ? { label: plan.summary.hint, tone: "ask" }
                                         : "Add to this sample"),
        onDrop: (paths, { plan }) => { dropped = plan; },
    });
    world.drag("enter", [SLIDE], viewer, 100, 200);
    world.drag("over", [], viewer, 110, 210);
    world.drag("over", [], viewer, 120, 220);
    const hint = world.hint();
    check("the zone's label is drawn beside the pointer",
          hint && hint.cls.has("is-shown") && hint.textContent === "Add to this sample"
          && hint.style.left === "138px" && hint.style.top === "242px");
    check("the plan is asked for once per drag", asked === 1);
    resolve({ summary: { hint: "Attach segmentation mask to slide" } });
    await new Promise((done) => setTimeout(done, 0));
    check("the label is redrawn when the plan lands",
          hint.textContent === "Attach segmentation mask to slide" && hint.dataset.tone === "ask");
    world.drop([SLIDE], viewer);
    check("the plan reaches the drop", dropped && dropped.summary.hint.startsWith("Attach"));
    check("and the label goes with the drop", !hint.cls.has("is-shown"));
    world.drag("enter", [SLIDE], viewer);
    check("the next drag asks again", asked === 2);
    world.drag("leave", [], null);
    check("leaving the window takes the label away", !hint.cls.has("is-shown"));
}

{
    const world = makeWorld();
    const { input } = world.field({ mode: "any", filter: "image" });
    world.drag("enter", [TABLE], input, 50, 50);
    const hint = world.hint();
    check("a refused box's label says why, in the refused tone",
          hint && /takes an image/.test(hint.textContent) && hint.dataset.tone === "refused");
}

{
    const world = makeWorld();
    const dialog = world.el("dialog");
    dialog.open = true;
    dialog.isModal = true;
    world.dialogs.push(dialog);
    world.context.attachDropTarget(dialog, {
        filter: "any", mode: "any", count: "many",
        hint: () => "Load it", onDrop: () => {},
    });
    world.drag("enter", [SLIDE], dialog);
    check("a zone inside a modal lights itself and draws no label",
          dialog.cls.has("is-drop-over") && !(world.hint() && world.hint().cls.has("is-shown")));
}

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
