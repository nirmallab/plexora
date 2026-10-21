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
        drag(phase, paths, at) {
            if (at) pointed = at;
            return send("plexora:native-drag", { phase, paths, x: 1, y: 1 });
        },
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

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
