/**
 * Does the Figure Builder plugin's client actually come up?
 *
 * A plugin's client is the list of plain <script> tags named by
 * `PLUGIN.scripts` (plexora/api/plugin.py), and nothing in the Python suite ever
 * runs them: pytest renders the panel's HTML and stops there. So the whole
 * client can be broken -- a file missing from the tuple, a class that throws the
 * moment it is constructed, a registration that never happens -- and every
 * server-side test still passes, with the failure appearing only as a panel that
 * renders and does nothing.
 *
 * This plugin adds a wrinkle the others do not have: two of its files SELF-BOOT
 * at the bottom, because they drive pages rather than tool panels. Those boots
 * run in this probe too, against a DOM where their root elements do not exist --
 * which is the case that matters, since every file loads on all three pages
 * and each controller has to stand down politely on the two that are not its
 * own. A controller that assumed its DOM would take the viewer's panel down with
 * it.
 *
 * What this checks, in the order it matters:
 *   1. every declared file parses and runs;
 *   2. each one defines the global the others reach for;
 *   3. the self-booting page controllers no-op when their page is absent;
 *   4. the workspace wires itself up on the page that IS its own, in the state
 *      it really boots in -- document not loaded yet;
 *   5. exactly one plugin registers, under the right name;
 *   6. a sidebar controller can be built from a real plugin context;
 *   7. the mm/label arithmetic the canvas depends on is right, since it is
 *      pure and nothing else in the suite executes it.
 *
 * The list is passed in by the Python test, which reads it off the descriptor,
 * so this probes what the server will really send.
 *
 * Run directly:
 *   node tests/js/figure_builder_boot_probe.mjs figureBuilderApi.js ...
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = process.argv.slice(2);

const registered = [];
const requests = [];

/** A browser stand-in no wider than what these files touch at load time. */
function browserGlobals() {
    const node = () => ({
        style: {}, dataset: {}, files: [],
        classList: { add() {}, remove() {}, toggle() {} },
        append() {}, appendChild() {}, remove() {}, setAttribute() {},
        getAttribute: () => null, addEventListener() {}, click() {},
    });
    const timers = new Map();
    let nextTimer = 1;
    let clockNow = 0;
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Uint8Array, Uint8ClampedArray, Infinity,
        URLSearchParams,
        // A clock the probe drives by hand. Going back to a capture waits for
        // the viewer to be QUIET rather than for it to say it has finished
        // moving, so a setTimeout that never fires would leave the landing
        // hanging -- and a real one would make the test wait out a debounce.
        setTimeout: (fn, ms) => {
            const id = nextTimer;
            nextTimer += 1;
            timers.set(id, { fn: fn, at: clockNow + (ms || 0) });
            return id;
        },
        clearTimeout: (id) => { timers.delete(id); },
        //: Advance the clock, running whatever comes due in the order it does.
        __tick: (ms) => {
            clockNow += ms;
            for (;;) {
                const due = Array.from(timers.entries())
                    .filter(([, t]) => t.at <= clockNow)
                    .sort((a, b) => a[1].at - b[1].at);
                if (!due.length) return;
                for (const [id, timer] of due) {
                    timers.delete(id);
                    timer.fn();
                }
            }
        },
        requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
        fetch: async (url) => {
            requests.push(String(url));
            return { ok: true, status: 200, json: async () => ({}) };
        },
        Blob: class Blob {},
        URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
        },
        OpenSeadragon: {
            Rect: class Rect {
                constructor(x, y, width, height) {
                    Object.assign(this, { x, y, width, height });
                }
            },
            Point: class Point {
                constructor(x, y) { this.x = x; this.y = y; }
            },
        },
        document: {
            // readyState is deliberately not "loading": the self-booting files
            // then run their boot immediately, in this probe, which is the
            // whole point of checking them here.
            readyState: "complete",
            getElementById: () => null,
            querySelectorAll: () => [],
            querySelector: () => null,
            createElement: () => node(),
            addEventListener() {}, removeEventListener() {},
            body: node(),
            get activeElement() { return null; },
            title: "",
        },
    };
    globals.window = {
        Plexora: { registerPlugin: (definition) => registered.push(definition) },
        // Distinct on every call, unlike a constant: ids that collide are a
        // real failure mode -- a batch of captures whose panels all share one
        // id is a batch the server rejects -- and a stub that never varies
        // cannot see it.
        crypto: {
            randomUUID: (() => {
                let n = 0;
                return () => (n += 1).toString(16).padEnd(32, "0");
            })(),
        },
        PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
        localStorage: {
            getItem: () => null, setItem() {}, removeItem() {},
        },
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
        // The same hand-driven clock as the bare globals above, because a
        // browser's `window.setTimeout` and its bare one ARE the same function
        // and code in this plugin reaches for both spellings. A window without
        // them is a stub that is missing something every page has, and the
        // failure is a TypeError during teardown rather than anything real.
        setTimeout: (fn, ms) => globals.setTimeout(fn, ms),
        clearTimeout: (id) => globals.clearTimeout(id),
        addEventListener() {}, removeEventListener() {},
        alert() {}, confirm: () => false, prompt: () => null,
        location: { href: "" },
        // Core's two page-lifecycle globals, guaranteed on every page by
        // base.html. Both are stubbed to their real no-viewer behaviour rather
        // than to something convenient:
        //
        //  - register() mounts immediately, which is what PlexoraPage does once
        //    the document has been parsed -- and is what these two controllers
        //    used to do for themselves on DOMContentLoaded.
        //  - go() sets location.href, which is exactly what the real router
        //    does when there is no live viewer to preserve. So the assertions
        //    below still read the destination off window.location and still
        //    describe what a browser would do.
        PlexoraPage: { register: (fn) => fn(), boot() {}, unmount() {} },
        PlexoraRouter: {
            go: (href) => { globals.window.location.href = href; },
            canRoute: () => false,
            datasource: () => "",
        },
    };
    // The files read `window.crypto` and bare `document`/`fetch` alike, which is
    // what a classic script sees in a browser.
    globals.crypto = globals.window.crypto;
    globals.plexoraUrl = globals.window.plexoraUrl;
    globals.PlexoraPage = globals.window.PlexoraPage;
    globals.PlexoraRouter = globals.window.PlexoraRouter;
    return globals;
}
/**
 * A DOM wide enough for the workspace to wire itself to.
 *
 * Every id resolves to a node rather than only the ones the template really
 * has. What is under test here is that the boot path RUNS, and nearly every
 * bind in it is `?.`-guarded, so a node invented for an id nobody renders
 * changes no outcome -- while insisting on the real list would mean restating
 * forty ids that workspace_body.html already owns, in a second place, for
 * nothing.
 */
function workspacePage() {
    const byId = new Map();
    const rect = () => ({ left: 0, top: 0, right: 900, bottom: 700,
                          width: 900, height: 700, x: 0, y: 0 });
    const context2d = () => ({
        setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() {}, fill() {}, fillRect() {}, fillText() {}, save() {}, restore() {},
        translate() {}, rotate() {}, scale() {}, drawImage() {}, putImageData() {},
        createImageData: (w, h) => ({ width: w, height: h,
                                      data: new Uint8ClampedArray(w * h * 4) }),
        measureText: () => ({ width: 8 }),
    });
    const make = (id, tag) => {
        const classes = new Set();
        const attributes = {};
        const element = {
            id: id || "", tagName: String(tag || "div").toUpperCase(),
            className: "", innerHTML: "", textContent: "", value: "", title: "",
            hidden: false, disabled: false, checked: false, width: 0, height: 0,
            style: {}, dataset: {}, files: [], children: [], options: [],
            clientWidth: 900, clientHeight: 700, scrollLeft: 0, scrollTop: 0,
            classList: {
                add: (...names) => names.forEach((name) => classes.add(name)),
                remove: (...names) => names.forEach((name) => classes.delete(name)),
                contains: (name) => classes.has(name),
                toggle: (name, on) => {
                    const want = on === undefined ? !classes.has(name) : !!on;
                    if (want) classes.add(name); else classes.delete(name);
                    return want;
                },
            },
            append: (...kids) => { element.children.push(...kids); },
            appendChild: (kid) => { element.children.push(kid); return kid; },
            insertAdjacentHTML() {}, remove() {}, focus() {}, blur() {}, click() {},
            select() {}, scrollTo() {}, scrollIntoView() {}, showModal() {}, close() {},
            setAttribute: (name, value) => { attributes[name] = String(value); },
            getAttribute: (name) => (name in attributes ? attributes[name] : null),
            removeAttribute: (name) => { delete attributes[name]; },
            addEventListener() {}, removeEventListener() {},
            querySelector: () => null, querySelectorAll: () => [],
            closest: () => null, contains: () => false,
            getBoundingClientRect: rect,
            getContext: () => context2d(),
        };
        return element;
    };
    return {
        readyState: "complete",
        getElementById: (id) => {
            if (!byId.has(id)) byId.set(id, make(id));
            return byId.get(id);
        },
        querySelector: () => null, querySelectorAll: () => [],
        createElement: (tag) => make("", tag),
        addEventListener() {}, removeEventListener() {},
        body: make("body"),
        activeElement: null,
        title: "",
    };
}


const ctx = createContext(browserGlobals());
const problems = [];
const loaded = [];

for (const name of SCRIPTS) {
    try {
        runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
        loaded.push(name);
    } catch (error) {
        problems.push(`${name} failed to load: ${error.message}`);
        break;   // everything after it would fail for the same reason
    }
}

const report = { order: SCRIPTS, loaded, registered: [], problems };

// Every file loaded without throwing, which is the precondition for asking any
// of them a question. Recorded once rather than re-tested as `!problems.length`
// at each block, so that a problem FOUND by one block does not skip the next --
// the checks are independent and each of them is worth its own report line.
const booted = !problems.length;

if (booted) {
    // Loading without throwing is not the same as working: a file can define
    // its class fine and still refer to a name that does not exist yet when the
    // class is actually used.
    runInContext(
        "globalThis.__names = { FigureBuilderApi: typeof FigureBuilderApi,"
        + " FigureSchema: typeof FigureSchema,"
        + " FigureScene: typeof FigureScene,"
        + " FigureCaptureTool: typeof FigureCaptureTool,"
        + " FigureCaptureBoxes: typeof FigureCaptureBoxes,"
        + " FigureCaptureDock: typeof FigureCaptureDock,"
        + " FigureDocumentState: typeof FigureDocumentState,"
        + " FigureConfirm: typeof FigureConfirm,"
        + " FigureRichText: typeof FigureRichText,"
        + " FigureSelection: typeof FigureSelection,"
        + " FigureActions: typeof FigureActions,"
        + " FigureTextPanel: typeof FigureTextPanel,"
        + " FigureCanvas: typeof FigureCanvas,"
        + " FigureTextEditor: typeof FigureTextEditor,"
        + " FigureExportUi: typeof FigureExportUi,"
        + " FigureLibrary: typeof FigureLibrary,"
        + " FigureWorkspace: typeof FigureWorkspace,"
        + " FigureBuilderSidebarController: typeof FigureBuilderSidebarController };",
        ctx);
    for (const [name, kind] of Object.entries(ctx.__names)) {
        if (kind === "undefined") problems.push(`${name} was never defined`);
    }

    // The action registry is the one list the floating bar and the right-click
    // menu both render from, so a duplicate id or an action that cannot run is
    // a menu row that silently does nothing -- which is exactly the class of
    // bug the registry replaced.
    const registry = runInContext(`(() => {
        const seen = {};
        return FigureActions.ALL.map((action) => {
            const duplicate = seen[action.id] === true;
            seen[action.id] = true;
            return {
                id: action.id,
                duplicate: duplicate,
                surfaces: action.surface,
                hasLabel: typeof action.label === "string" && action.label.length > 0,
                hasApplies: typeof action.applies === "function",
                hasEnabled: typeof action.enabled === "function",
                // Every action either runs something or opens a popover. One
                // that does neither is a button with nothing behind it.
                actionable: typeof action.run === "function" || action.popover === true,
                // A bar button needs an icon; an overflow row is text only.
                iconed: !action.surface.includes("bar") || typeof action.icon === "string",
            };
        });
    })()`, ctx);
    for (const entry of registry) {
        if (entry.duplicate) problems.push(`action ${entry.id} is declared twice`);
        if (!entry.hasLabel) problems.push(`action ${entry.id} has no label`);
        if (!entry.hasApplies) problems.push(`action ${entry.id} has no applies()`);
        if (!entry.hasEnabled) problems.push(`action ${entry.id} has no enabled()`);
        if (!entry.actionable) {
            problems.push(`action ${entry.id} neither runs nor opens a popover`);
        }
        if (!entry.iconed) problems.push(`action ${entry.id} is on the bar with no icon`);
        for (const surface of entry.surfaces) {
            if (!["bar", "overflow", "menu"].includes(surface)) {
                problems.push(`action ${entry.id} names an unknown surface ${surface}`);
            }
        }
    }

    // What the two surfaces actually offer, for the two selections that decide
    // the rule.
    //
    // A generic action that cannot run does not sit on the bar greyed; it moves
    // into "More", where the row carries its own label. That was a complaint
    // about a real screen: selecting one caption gave five dead icons -- Align,
    // Distribute, Match size, Layout and Group all need two objects -- while
    // Duplicate and Delete sat behind the overflow, so the bar was mostly noise
    // and nothing on it was worth pressing. What must stay true is that moving
    // an action off the bar never puts it out of reach: bar plus overflow still
    // offer everything `applies` allowed.
    const surfaces = runInContext(`(() => {
        const annotation = (id, type) => ({ annotation_id: id,
                                            type: type || "text", z: 1 });
        const state = {
            document: { annotations: { a1: annotation("a1"), a2: annotation("a2"),
                                       a3: annotation("a3"),
                                       l1: annotation("l1", "line"),
                                       l2: annotation("l2", "arrow") } },
            panel: () => null,
            source: () => null,
        };
        const canvas = { groupFor: () => null };
        const look = (ids) => {
            const sel = FigureSelection.describe(ids, state, canvas);
            const context = { ids: ids, sel: sel, state: state,
                              canvas: canvas, handlers: {} };
            const names = (surface) => FigureActions.forSurface(surface, sel, context)
                .map((action) => action.id);
            return {
                bar: names("bar"),
                overflow: names("overflow"),
                dead: FigureActions.forSurface("bar", sel, context)
                    .filter((action) => !action.isEnabled).map((action) => action.id),
                applicable: FigureActions.ALL
                    .filter((action) => action.applies(sel, context))
                    .filter((action) => action.surface.some(
                        (surface) => surface !== "menu"))
                    .map((action) => action.id),
            };
        };
        return { one: look(["a1"]), two: look(["a1", "a2"]),
                 three: look(["a1", "a2", "a3"]),
                 strokes: look(["l1", "l2"]) };
    })()`, ctx);

    if (surfaces.one.dead.length) {
        problems.push("the bar offers dead buttons on a single text box: "
                      + surfaces.one.dead.join(", "));
    }
    for (const id of ["edit_text", "arrange", "duplicate", "delete"]) {
        if (!surfaces.one.bar.includes(id)) {
            problems.push(`the bar dropped ${id} from a single text box`);
        }
    }
    // Each of these needs more than one object, and `distribute` needs more than
    // two -- an equal gap between two things is the gap they already have.
    for (const [id, needs] of [["align", "two"], ["resize", "two"],
                               ["layout", "two"], ["group", "two"],
                               ["distribute", "three"]]) {
        if (surfaces.one.bar.includes(id)) {
            problems.push(`${id} cannot run on one object and was on the bar for one`);
        }
        if (!surfaces.one.overflow.includes(id)) {
            problems.push(`${id} left the bar and was not caught by the overflow`);
        }
        // ...and comes back to the bar as soon as it can run.
        if (!surfaces[needs].bar.includes(id)) {
            problems.push(`${id} stayed off the bar with ${needs} objects selected`);
        }
    }
    // Two lines are two objects and are not two things to line up: a line's
    // `w_mm`/`h_mm` are the components of a vector rather than a size, so
    // `FigureCanvas.arrangeItems` leaves them out and these four commands have
    // nothing to act on. The predicate used to read `count > 1`, which put all
    // four on the bar, live, doing nothing when pressed.
    for (const id of ["align", "resize", "layout", "distribute"]) {
        if (surfaces.strokes.bar.includes(id)) {
            problems.push(`${id} was live on the bar for a selection of lines,`
                          + " which it cannot arrange");
        }
        if (!surfaces.strokes.overflow.includes(id)) {
            problems.push(`${id} left the bar for a selection of lines and was`
                          + " not caught by the overflow");
        }
    }

    for (const selection of ["one", "two", "three", "strokes"]) {
        const reachable = new Set(
            surfaces[selection].bar.concat(surfaces[selection].overflow));
        for (const id of surfaces[selection].applicable) {
            if (!reachable.has(id)) {
                problems.push(`${id} applies to the ${selection}-object selection`
                              + " but is on neither the bar nor the overflow");
            }
        }

    // Every bar button now carries a WORD as well as an icon, so every action
    // that can reach the bar has to have one short enough to sit UNDER one.
    // "Title, label and numbering" under a glyph is a button as wide as the
    // page, which is what `short` exists to prevent -- and an action that
    // forgets it gets the full label silently.
    const words = runInContext(`(() => FigureActions.ALL
        .filter((action) => action.surface.includes("bar"))
        .map((action) => ({ id: action.id,
                            word: action.short || action.label })))()`, ctx);
    for (const entry of words) {
        if (entry.word.length > 12) {
            problems.push(`${entry.id} prints "${entry.word}" on the bar, which is`
                          + " too long to sit beside an icon -- it needs a `short`");
        }
    }

    // Arrange offers four commands and every one of them has to exist, run and
    // carry an icon. The popover reads this list, and so does the right-click
    // menu, which is the whole reason it is a list rather than two hand-typed
    // rows in each.
    const arrange = runInContext(`(() => FigureActions.ARRANGE.map((id) => {
        const action = FigureActions.byId(id);
        return { id: id, found: Boolean(action),
                 runs: Boolean(action && action.run),
                 iconed: Boolean(action && action.icon),
                 shortcut: Boolean(action && /\\u2318/.test(action.shortcut || "")) };
    }))()`, ctx);
    if (arrange.length !== 4) {
        problems.push(`Arrange offers ${arrange.length} commands, expected 4`);
    }
    for (const entry of arrange) {
        if (!entry.found) problems.push(`Arrange names ${entry.id}, which is not an action`);
        else if (!entry.runs) problems.push(`Arrange's ${entry.id} has nothing to run`);
        else if (!entry.iconed) problems.push(`Arrange's ${entry.id} has no icon`);
        else if (!entry.shortcut) problems.push(`Arrange's ${entry.id} names no shortcut`);
    }

    // No action may carry its shortcut inside its label any more. They were
    // written that way -- `label: "Group  \u2318G"` -- and splitting the key
    // into its own column is what lets a menu range the labels left and the
    // keys right. One left behind would print its key twice: once in the middle
    // of the row as part of the label, once again against the right edge.
    const keys = runInContext(`(() => FigureActions.ALL.map((action) => ({
        id: action.id, label: action.label,
        shortcut: action.shortcut || null })))()`, ctx);
    for (const entry of keys) {
        if (/[\u2318\u21e7\u2325\u232b]/.test(entry.label)) {
            problems.push(`${entry.id}'s label still has the shortcut in it:`
                          + ` "${entry.label}" -- it belongs in \`shortcut\``);
        }
    }

    // The unit table is written out in two places -- the View menu owns the
    // preference, and the Transform popover has to convert without depending on
    // a View menu that may not have been built yet. Two copies of a conversion
    // factor is exactly the kind of thing that drifts silently, so it is pinned
    // rather than trusted.
    const units = runInContext(`(() => {
        const view = FigureViewOptions.UNITS;
        const bar = FigureContextBar.UNITS;
        return Object.keys(view).concat(Object.keys(bar)).map((name) => ({
            name: name,
            same: Boolean(view[name] && bar[name]
                          && view[name].per === bar[name].per
                          && view[name].label === bar[name].label),
        }));
    })()`, ctx);
    for (const unit of units) {
        if (!unit.same) {
            problems.push(`the bar and the View menu disagree about the unit "`
                          + `${unit.name}"`);
        }
    }

    // Z-order, which is the one part of stacking that is arithmetic. `front`
    // and `back` are obvious; the two RELATIVE commands are not, and the bug
    // they exist to avoid -- a pair of adjacent objects swapping places because
    // each member was stepped on its own -- shows up only on the second press.
    const stack = runInContext(`(() => {
        const items = ["a", "b", "c", "d"];
        const move = (chosen, command, from) => FigureCanvas.reordered(
            from || items, new Set(chosen), command, (item) => item);
        const twice = move(["a", "b"], "forward", move(["a", "b"], "forward"));
        return {
            front: move(["b"], "front"),
            back: move(["c"], "back"),
            forward: move(["b"], "forward"),
            backward: move(["c"], "backward"),
            // Already at the top / already at the bottom: nothing to do, and
            // saying so is what stops a no-op writing a revision.
            topmost: move(["d"], "forward"),
            bottommost: move(["a"], "backward"),
            alone: move(["a"], "front", ["a"]),
            // A split selection jumps what sits BETWEEN its halves rather than
            // closing up around it.
            split: move(["a", "c"], "forward"),
            // ...and stays in its own order after two presses.
            block: twice,
        };
    })()`, ctx);
    const said = (value) => (value === null ? "nothing" : value.join(""));
    for (const [name, want] of [["front", "acdb"], ["back", "cabd"],
                                ["forward", "acbd"], ["backward", "acbd"],
                                ["topmost", "nothing"], ["bottommost", "nothing"],
                                ["alone", "nothing"], ["split", "bdac"],
                                ["block", "cdab"]]) {
        if (said(stack[name]) !== want) {
            problems.push(`reordered(${name}) gave ${said(stack[name])}, expected ${want}`);
        }
    }

    }

    // The text sidebar, rendered.
    //
    // Its markup is built from one template literal and read by nobody until it
    // is on screen, so the things that go wrong with it go wrong silently. Two
    // of them already did.
    //
    // The render is caught rather than allowed to throw, because this probe
    // reports by writing JSON at the end: an uncaught error here produces no
    // report at all, and the twenty checks above it are lost along with this
    // one. `test_the_probe_catches_a_file_dropped_from_the_descriptor` runs the
    // whole probe with a file deliberately missing, so that is not a
    // hypothetical.
    const panel = runInContext(`(() => {
        try {
            const root = { innerHTML: "", addEventListener() {},
                           querySelector: () => null, contains: () => false };
            const annotation = {
                annotation_id: "t1", type: "text", text: "Fig. 1a",
                style: { font_family: "Helvetica", font_size_pt: 14,
                         color: "#000000", align: "left", valign: "top",
                         line_height: 1.2, autofit: true },
            };
            const panel = new FigureTextPanel({
                root: root, canvas: null, state: {
                    document: { annotations: { t1: annotation } },
                },
            });
            panel.update(["t1"]);
            return { markup: root.innerHTML };
        } catch (error) {
            return { failed: String(error && error.message) };
        }
    })()`, ctx);

    if (panel.failed) {
        problems.push("the text panel would not render: " + panel.failed);
    } else {
        textPanelChecks(panel.markup);
    }
}

/** Kept out of the block above so that a panel which failed to render at all
 *  reports that once, rather than four times over an empty string. */
function textPanelChecks(panel) {

    // A number input reports \`selectionStart\` as null, so the panel could not
    // put the caret back after its own redraw: typing "20" left 02 in the field,
    // because the second digit went in at offset 0. The platform spinners were
    // already suppressed in CSS, so the type was buying nothing but that.
    const size = /<input[^>]*data-field="size_pt"[^>]*>/.exec(panel)
        || /<input[^>]*id="fb_text_size"[^>]*>/.exec(panel);
    if (!size) {
        problems.push("the text panel has no size field at all");
    } else if (/type="number"/.test(size[0])) {
        problems.push("the size field is type=\"number\" again, which reports"
                      + " selectionStart as null -- typing 20 gives 02");
    }

    // The well is an element that draws nothing but its own colour, so it is
    // invisible when it is broken rather than absent. It went missing once
    // already, to a `width: auto` sorted below an `inline-size: 40px` in the
    // same block -- and now that it paints itself from a custom property rather
    // than from a `value`, an empty one is the same silent nothing.
    const well = /<button[^>]*data-swatch="color"[^>]*>/.exec(panel);
    if (!well) {
        problems.push("the text panel has no colour well");
    } else if (!/--fb-well-color:#[0-9a-f]{6}/i.test(well[0])) {
        problems.push("the text panel's colour well carries no colour to draw");
    }

    // American spellings throughout the interface, which is the user's. The
    // identifiers stay as they are -- `colourPopover`, `shareLegendColours` --
    // because renaming those is a different change and touches no label.
    for (const [wrong, right] of [["Colour", "Color"], ["Centre", "Center"]]) {
        if (panel.includes(wrong)) {
            problems.push(`the text panel says "${wrong}" where it should say`
                          + ` "${right}"`);
        }
    }
}

if (booted) {
    // The page controllers already ran their boot when their file loaded, with
    // no root element present. Reaching here at all means neither threw; this
    // pins that they answered "not my page" rather than half-initialising.
    try {
        runInContext(
            "globalThis.__booted = { library: FigureLibrary.boot(), workspace: FigureWorkspace.boot() };",
            ctx);
        if (ctx.__booted.library !== null || ctx.__booted.workspace !== null) {
            problems.push("a page controller booted on a page that is not its own");
        }
    } catch (error) {
        problems.push(`a page controller threw when its page was absent: ${error.message}`);
    }

    // ...and the page that IS one controller's own.
    //
    // The check above proves the two controllers stand down politely. This
    // proves the one that does not stand down actually comes up -- and comes up
    // in the state it really boots in, which is with `state.load()` still in
    // flight and the document therefore null. The whole of setup() runs in that
    // window: the canvas, the context bar, the View menu's margins and rulers
    // are all wired against a figure that has not arrived. One dereference in
    // there takes the entire page down, silently, because nothing after the
    // throw ever runs -- no tray, no topbar, no load. That shipped once.
    //
    // setup() rather than boot() because boot() also starts the load, and this
    // probe is synchronous: awaiting it would mean either a fixture document
    // restated from the schema or a promise settling after the report was
    // written.
    const emptyDom = ctx.document;
    try {
        ctx.document = workspacePage();
        ctx.window.devicePixelRatio = 1;
        runInContext(
            "globalThis.__workspace = (() => {"
            + " const w = new FigureWorkspace({ figureId: 'fig_probe' });"
            + " w.setup();"
            + " const wired = { canvas: !!w.canvas, contextBar: !!w.contextBar,"
            + "   contextMenu: !!w.contextMenu, viewOptions: !!w.viewOptions,"
            + "   quickEdit: !!w.quickEdit, exportUi: !!w.exportUi };"
            // Rulers and the grid are off by default, so a default boot never
            // reaches the code that draws them -- but a preference is per
            // machine and permanent, so somebody who turned them on once has
            // them on for every figure they open afterwards, including the
            // first paint of an empty one.
            + " w.viewOptions.pick('rulers');"
            + " w.viewOptions.pick('grid');"
            + " const page = w.canvas.page;"
            // A change can arrive with nothing in it: a figure whose read failed
            // emits one too.
            + " w.render();"
            + " w.destroy();"
            + " return { wired, page, document: w.state.document || null }; })();",
            ctx);
        const workspace = ctx.__workspace;
        for (const [part, built] of Object.entries(workspace.wired)) {
            if (!built) problems.push(`the workspace came up without its ${part}`);
        }
        if (workspace.document !== null) {
            problems.push("the workspace had a document before it had loaded one");
        }
        // Null is the answer the whole boot depends on: `render`, `zoomToFit`
        // and the margins all test it, and all of them run before the load
        // returns.
        if (workspace.page !== null) {
            problems.push("the canvas claimed a page with no document loaded");
        }
    } catch (error) {
        problems.push(`the workspace threw while coming up: ${error.message}`);
    } finally {
        ctx.document = emptyDom;
    }


    if (registered.length !== 1) {
        problems.push(`expected exactly one plugin registration, got ${registered.length}`);
    } else if (registered[0].name !== "figure_builder") {
        problems.push(`registered under the wrong name: ${registered[0].name}`);
    } else if (registered[0].ownsCellLayer !== false) {
        problems.push("figure_builder must not claim the cell layer");
    }

    try {
        runInContext(
            "globalThis.__built = (() => {"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 100, height: 100 },"
            + "   viewer: null, onCleanup() {} });"
            + " c.setup();"
            + " return { datasource: c.datasource, figureId: c.figureId,"
            + "          hasApi: typeof c.api.listFigures === 'function' }; })();",
            ctx);
    } catch (error) {
        problems.push(`a controller could not be built: ${error.message}`);
    }

    // Capture mode does not need a figure. Asked as a behaviour rather than
    // read off the source, because "the button is enabled" and "the tool arms"
    // are different claims and only the second one is the feature.
    try {
        runInContext(
            "globalThis.__armed = (() => {"
            + " const osd = { addHandler() {}, removeHandler() {}, canvas: { style: {},"
            + "   getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "   viewer: { viewer: osd }, onCleanup() {} });"
            + " c.toggleCapture();"
            + " const withNoFigure = c.capture.active && c.figureId === null;"
            + " c.toggleCapture();"
            + " const offAgain = c.capture.active === false;"
            + " c.editing = { panelId: 'pnl_1' };"
            + " c.toggleCapture();"
            + " const whileEditing = c.capture.active;"
            + " return { withNoFigure, offAgain, whileEditing }; })();",
            ctx);
        const armed = ctx.__armed;
        if (!armed.withNoFigure) {
            problems.push("capture mode refused to arm without a figure");
        }
        if (!armed.offAgain) problems.push("the toggle did not stand capture mode down");
        // The viewer is showing a panel's borrowed scene while an edit session
        // runs. A capture taken then is a panel of somebody else's view, and
        // nothing on screen would say so.
        if (armed.whileEditing) {
            problems.push("capture mode armed while a panel's view was loaded");
        }
        report.armed = armed;
    } catch (error) {
        problems.push(`capture mode threw: ${error.message}`);
    }

    // Going back to a capture. Two claims, and the second one is the whole
    // reason the first is useful: the viewer moves to the region, and NOTHING
    // about the rendering is put back. A "go back" that restored the captured
    // channels would make "same field, different rendering" -- two panels of
    // one region under two colour schemes -- impossible to reach by the obvious
    // route, and would silently rewrite the user's colours while it did it.
    try {
        runInContext(
            "globalThis.__selection = (() => {"
            + " const fitted = []; let restores = 0; let shift = 0;"
            + " const handlers = {};"
            + " const item = { source: { getImagePixel: (_i, p) => [p.x * 5, p.y * 5] },"
            + "   imageToViewportRectangle: (r) => ({ r, getTopLeft: () => ({ x: r.x / 4000, y: r.y / 3000 }),"
            + "     getBottomRight: () => ({ x: (r.x + r.width) / 4000, y: (r.y + r.height) / 3000 }) }) };"
            + " const osd = { addHandler(n, f) { (handlers[n] = handlers[n] || []).push(f); },"
            + "   removeHandler(n, f) { handlers[n] = (handlers[n] || []).filter((h) => h !== f); },"
            + "   world: { getItemAt: () => item },"
            + "   viewport: { fitBounds: (b) => fitted.push(b.r),"
            + "     pixelFromPoint: (p) => ({ x: p.x * 800 + shift, y: p.y * 600 }) },"
            + "   canvas: { style: {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const fire = (n) => (handlers[n] || []).slice().forEach((fn) => fn());"
            // How the viewer really arrives, and the whole point of this
            // fixture: it says `animation-finish` and then MOVES AGAIN, because
            // OpenSeadragon pulls the viewport back inside its constraints
            // afterwards. Landing the frame on the first of those two arrivals
            // put it three pixels off the region and the lock -- which is what
            // the selection is -- broke before the user could see it was made.
            + " const arrive = () => { shift = 40; fire('animation');"
            + "   shift = 3; fire('animation-finish');"
            + "   shift = 0; fire('animation'); };"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "   viewer: { viewer: osd }, onCleanup() {} });"
            + " const restore = FigureScene.restore;"
            + " FigureScene.restore = async () => { restores += 1; return null; };"
            + " c.captures = [{ id: 'cap_1', panelId: null, url: null,"
            + "     scene: { viewport: { x: 1000, y: 750, w: 2000, h: 1500 } } },"
            + "   { id: 'cap_2', panelId: 'pnl_9', url: null,"
            + "     scene: { viewport: { x: 0, y: 0, w: 400, h: 300 } } }];"
            // NOT armed first. Clicking a capture is what arms it -- everything
            // going back to a region does is invisible with the mode off.
            + " const armedBefore = c.capture.active;"
            + " c.selectCapture('cap_1');"
            // Armed and highlighted the instant it is clicked, before the
            // viewer has gone anywhere: the click has to look like it did
            // something while the flight is still in the air.
            + " const atOnce = { armed: c.capture.active, selected: c.selected };"
            + " arrive(); __tick(200);"
            + " const answer = { armedBefore, atOnce, armedAfter: c.capture.active,"
            + "   selected: c.selected, fitted: fitted.length, restores,"
            + "   frame: c.capture.box, pinned: c.capture.pinned, unattached: c.unattached(),"
            + "   framing: fitted[0] ? { x: fitted[0].x, y: fitted[0].y,"
            + "     w: fitted[0].width, h: fitted[0].height } : null };"
            + " c.selectCapture('no_such_capture');"
            + " answer.stillSelected = c.selected;"
            // A nudge -- the viewer's own settling, a small pan. The region is
            // still somewhere a frame can sit, so the frame follows it and the
            // capture stays selected.
            + " shift = 120; fire('animation');"
            + " answer.afterNudge = { pinned: c.capture.pinned, selected: c.selected,"
            + "   frame: c.capture.box };"
            // Navigating away. The region can no longer be framed inside the
            // viewer, so the lock goes and takes the selection with it.
            + " shift = 700; fire('animation');"
            + " answer.afterMoving = { pinned: c.capture.pinned, selected: c.selected };"
            + " FigureScene.restore = restore;"
            + " return answer; })();",
            ctx);
        const selection = ctx.__selection;
        // Clicking a capture arms the shutter. Without this the click moves the
        // viewer and leaves nothing on screen saying why -- no frame, no lock,
        // and the one control that would show either still switched off.
        if (selection.armedBefore !== false) {
            problems.push("the fixture started with capture mode already on");
        }
        if (selection.armedAfter !== true) {
            problems.push("selecting a capture did not turn capture mode on");
        }
        // Both of them the instant the thumbnail is clicked, while the viewer is
        // still flying: a click whose only visible effect arrives half a second
        // later reads as a click that missed.
        if (selection.atOnce.armed !== true || selection.atOnce.selected !== "cap_1") {
            problems.push(`clicking a capture did not take effect at once: `
                + JSON.stringify(selection.atOnce));
        }
        // And it is still selected once the viewer has finished arriving --
        // including the second, smaller arrival after `animation-finish`, which
        // used to break the lock and take the selection with it.
        if (selection.selected !== "cap_1") {
            problems.push(`selecting a capture did not select it: ${selection.selected}`);
        }
        if (selection.fitted !== 1) {
            problems.push(`the viewer was not put back over the region (${selection.fitted} moves)`);
        }
        if (selection.restores !== 0) {
            problems.push("going back to a capture restored its rendering state");
        }
        // The frame lands ON the region, which is what makes the next capture
        // of it pixel-for-pixel concordant with the first.
        const frame = selection.frame || {};
        if (frame.x !== 200 || frame.y !== 150 || frame.width !== 400 || frame.height !== 300) {
            problems.push(`the frame did not land on the region: ${JSON.stringify(frame)}`);
        }
        // A strip and a canvas that disagree about what exists is worse than
        // either answer, so an id that is not in the strip selects nothing
        // rather than clearing what was.
        if (selection.stillSelected !== "cap_1") {
            problems.push("an unknown capture id changed the selection");
        }
        if (selection.unattached !== 1) {
            problems.push(`the unattached count is wrong: ${selection.unattached}`);
        }
        // With room around it, not filling the window: the capture is 2000x1500
        // and the viewer is put over 4000x3000 of the slide centred on it.
        const framing = selection.framing || {};
        if (framing.x !== 0 || framing.y !== 0 || framing.w !== 4000 || framing.h !== 3000) {
            problems.push(`going back did not leave room around the region: ${JSON.stringify(framing)}`);
        }
        // The lock is what makes the next shot the SAME region rather than a
        // fresh reading off the screen -- a pixel or two out, and only in the
        // file.
        const pin = selection.pinned || {};
        if (pin.x !== 1000 || pin.y !== 750 || pin.w !== 2000 || pin.h !== 1500) {
            problems.push(`the frame did not lock onto the region: ${JSON.stringify(pin)}`);
        }
        // A viewer that moves a little does NOT cost the user the capture they
        // just clicked. The lock is on a region and the frame is how that region
        // is shown, so the frame follows it -- which is also what makes the lock
        // survive the movement nobody asked for: OpenSeadragon's own settling
        // after a flight, a nudge, a resize.
        const nudged = selection.afterNudge || {};
        if (!nudged.pinned || nudged.selected !== "cap_1") {
            problems.push(`a nudge cost the user the capture: ${JSON.stringify(nudged)}`);
        }
        if (!nudged.frame || nudged.frame.x !== 320) {
            problems.push(`the frame did not follow the region: ${JSON.stringify(nudged.frame)}`);
        }
        // And it lets go once the region can no longer be framed at all, taking
        // the strip's active item and the box's highlight with it: all three
        // mean "the shutter will take this one", so they cannot disagree. That
        // is also what keeps a following frame inside the viewer, since
        // #openseadragon_wrapper does not clip.
        if (selection.afterMoving.pinned !== null) {
            problems.push("the frame stayed locked after the viewer moved");
        }
        if (selection.afterMoving.selected !== null) {
            problems.push("the selection outlived the lock it was showing");
        }
        report.selection = selection;
    } catch (error) {
        problems.push(`selecting a capture threw: ${error.message}`);
    }

    // "Figure Canvas" leaves the viewer for the figure's own page. Two claims,
    // and the second is the one that can lose an hour's work: unattached
    // captures are MEMORY, and a navigation ends the memory -- so everything
    // waiting has to be written into the figure BEFORE the page changes, and a
    // write that fails has to stop the navigation rather than carry the
    // captures off the page.
    try {
        runInContext(
            "globalThis.__canvas = (() => {"
            + " const osd = { addHandler() {}, removeHandler() {}, canvas: { style: {},"
            + "   getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const build = () => { const c = new FigureBuilderSidebarController({"
            + "     url: (p) => '/' + p, datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "     viewer: { viewer: osd }, onCleanup() {} });"
            + "   c.figureId = 'fig_abc';"
            + "   c.state = { document: { panels: {}, pages: [{ page_id: 'pg_1' }] } };"
            + "   return c; };"
            + " const order = [];"
            + " const good = build();"
            + " good.attachCaptures = () => { order.push('attached'); return Promise.resolve(true); };"
            + " const bad = build();"
            + " bad.attachCaptures = () => Promise.resolve(false);"
            + " window.location.href = '';"
            + " return good.goToCanvas().then(() => {"
            + "   order.push('href:' + window.location.href);"
            + "   window.location.href = '';"
            + "   return bad.goToCanvas(); }).then(() => ({ order,"
            + "     blockedHref: window.location.href, said: bad.failure })); })();",
            ctx);
        const canvas = await ctx.__canvas;
        if (canvas.order[0] !== "attached") {
            problems.push("the canvas was opened without saving the waiting captures first");
        }
        if (canvas.order[1] !== "href:/plugins/figure_builder/figure/fig_abc") {
            problems.push(`"Figure Canvas" did not go to the figure's page: ${canvas.order[1]}`);
        }
        // A pane could afford to open anyway. A navigation cannot: the strip is
        // the only copy of an unattached capture.
        if (canvas.blockedHref !== "") {
            problems.push("captures that could not be saved were carried off the page anyway");
        }
        if (!canvas.said) {
            problems.push("the canvas refused to open and said nothing about why");
        }
        report.canvas = canvas;
    } catch (error) {
        problems.push(`opening the canvas threw: ${error.message}`);
    }

    // A capture becomes a panel in exactly one place, and a strip full of them
    // becomes one batch -- which is what makes a burst of captures one undo
    // step rather than six. Pure, and nothing else in the suite runs it.
    try {
        runInContext(
            "globalThis.__panels = (() => {"
            + " const source = { source_id: 'src_1', pixel_size: { value: 0.325 } };"
            + " const captures = [1, 2, 3].map((n) => ({ id: 'cap_' + n,"
            + "   scene: { source_id: '', viewport: { x: n, y: 0, w: 10, h: 8 } } }));"
            + " const panels = captures.map((c) =>"
            + "   FigureBuilderSidebarController.panelFor(c, source));"
            + " return { ids: panels.map((p) => p.panel_id),"
            + "   sources: panels.map((p) => p.scene.source_id),"
            + "   placed: panels.filter((p) => p.placement !== null).length,"
            + "   scalebars: panels.every((p) => p.scalebar.visible === true),"
            + "   uncalibrated: FigureBuilderSidebarController.panelFor(captures[0],"
            + "     { source_id: 'src_2', pixel_size: null }).scalebar.visible }; })();",
            ctx);
        const panels = ctx.__panels;
        if (new Set(panels.ids).size !== 3) {
            problems.push(`captures did not get distinct panel ids: ${panels.ids}`);
        }
        if (panels.sources.some((id) => id !== "src_1")) {
            problems.push("a panel's scene was not joined to the figure's source");
        }
        // Composition is a different sitting from exploration: a capture that
        // placed itself on a page would make every capture a layout decision.
        if (panels.placed !== 0) {
            problems.push("a capture placed itself on a page instead of the tray");
        }
        // A bar drawn from an assumed pixel size looks exactly like one that is
        // right, so an uncalibrated source gets none at all.
        if (!panels.scalebars || panels.uncalibrated !== false) {
            problems.push("scale bars did not follow the source's calibration");
        }
        report.panels = panels;
    } catch (error) {
        problems.push(`panelFor threw: ${error.message}`);
    }

    // Pure arithmetic the canvas and the export both depend on, and which
    // nothing else in the suite ever executes. A label sequence that goes
    // A..Z,BA instead of A..Z,AA is wrong in a way no server test can see.
    try {
        runInContext(
            "globalThis.__math = {"
            + " labels: [0, 1, 25, 26, 27].map((i) => FigureSchema.labelFor(i, 'A')),"
            + " lower: FigureSchema.labelFor(0, 'a'),"
            + " numbered: FigureSchema.labelFor(2, 'A1'),"
            + " noScale: FigureSchema.physicalWidthUm({ pixel_size: null }, { w: 100 }),"
            + " width: FigureSchema.physicalWidthUm({ pixel_size: { value: 0.325 } }, { w: 4000 }),"
            + " bar: FigureSchema.scaleBarLength(1300),"
            + " escaped: FigureSchema.escapeHtml('<img src=x onerror=1>') };",
            ctx);
        const math = ctx.__math;
        if (JSON.stringify(math.labels) !== JSON.stringify(["A", "B", "Z", "AA", "AB"])) {
            problems.push(`panel labels are wrong: ${JSON.stringify(math.labels)}`);
        }
        if (math.lower !== "a" || math.numbered !== "A3") {
            problems.push(`label styles are wrong: ${math.lower} / ${math.numbered}`);
        }
        if (math.noScale !== null) {
            problems.push("an uncalibrated source produced a physical width");
        }
        if (Math.abs(math.width - 1300) > 1e-6) {
            problems.push(`physical width is wrong: ${math.width}`);
        }
        if (math.bar !== 250) {
            problems.push(`scale bar length is wrong: ${math.bar}`);
        }
        if (math.escaped.includes("<")) {
            problems.push("escapeHtml let markup through");
        }
        report.math = math;
    } catch (error) {
        problems.push(`figure arithmetic threw: ${error.message}`);
    }
}

// What revision a save actually carries.
//
// It is read when the request goes out, not when the edit was made, and those
// are different moments: the queue serialises saves, so a commit made while an
// earlier one is still in flight would otherwise send the revision from before
// it -- and be answered with a 409 by a server no other session has touched.
// Placing a text box and pressing Escape is exactly that shape, an add and a
// remove in one breath, which is how this reached a user's console.
//
// Reading it late is only safe because the chain guarantees nothing else can
// have written in between; that is the invariant this pins.
if (!problems.length) {
    try {
        const queue = await runInContext(`(async () => {
            const sent = [];
            const answer = (id, baseRevision) => {
                sent.push(baseRevision);
                return Promise.resolve(
                    { ok: true, status: 200, data: { revision: baseRevision + 1 } });
            };
            const state = new FigureDocumentState({
                figureId: "fig_probe",
                api: { patchFigure: answer, replaceFigure: answer },
            });
            state.document = { revision: 7, annotations: {}, pages: {},
                               panels: {}, sources: {}, settings: {} };

            // Three in one turn, none of them awaited -- the way finishDraw's
            // add and the editor's remove arrive.
            const saves = [
                state.commit([{ op: "one" }], () => {}),
                state.commit([{ op: "two" }], () => {}),
                state.commit([{ op: "three" }], () => {}),
            ];
            const stored = await Promise.all(saves);

            // And undo, which goes out as a whole-document replace and had the
            // same bug: two in a row both carried the revision from before the
            // first.
            const undos = [state.undo(), state.undo()];
            const undone = await Promise.all(undos);

            return { sent: sent, stored: stored, undone: undone,
                     status: state.status, revision: state.revision };
        })()`, ctx);

        const expected = [7, 8, 9, 10, 11];
        if (queue.sent.join(",") !== expected.join(",")) {
            problems.push("queued saves sent the revisions "
                          + `[${queue.sent.join(", ")}], expected `
                          + `[${expected.join(", ")}] -- a save carrying a `
                          + "revision an earlier one has already superseded is "
                          + "the 409 the server has no way to tell from a real "
                          + "conflict");
        }
        if (queue.stored.includes(false) || queue.undone.includes(false)) {
            problems.push("a queued save reported failure against a server that "
                          + "accepted every request");
        }
        if (queue.status !== "saved") {
            problems.push(`the queue settled as "${queue.status}", not "saved"`);
        }
        report.queue = queue;
    } catch (error) {
        problems.push(`the save queue threw: ${error.message}`);
    }
}

report.registered = registered.map((d) => ({
    name: d.name, ownsCellLayer: d.ownsCellLayer, hooks: Object.keys(d),
}));
report.controller = ctx.__built || null;
report.requests = requests;
report.problems = problems;

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
