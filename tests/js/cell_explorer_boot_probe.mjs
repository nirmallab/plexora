/**
 * Does the Cell Explorer plugin's client actually come up?
 *
 * A plugin's client is the list of plain <script> tags named by `PLUGIN.scripts`
 * (plexora/api/plugin.py), and nothing in the Python suite ever runs them:
 * pytest renders the panel's HTML and stops there. So the whole client can be
 * broken -- a file missing from the tuple, a class that throws the moment it is
 * constructed, a registration that never happens -- and every server-side test
 * still passes, with the failure appearing only as a panel that renders and
 * does nothing.
 *
 * What this checks, in the order it matters:
 *   1. every declared file parses and runs;
 *   2. each one defines the global the others reach for;
 *   3. exactly one plugin registers, under the right name, claiming the cell
 *      layer -- which is the whole point of this plugin;
 *   4. the selection provider it hands core is the inert one it must be;
 *   5. a controller can be built from a real plugin context -- the first moment
 *      any of the constructors actually run;
 *   6. the two CORE widgets this panel is built out of really do take the
 *      options it passes them, and give their popovers back afterwards;
 *   7. the lookup table builder produces what core reads.
 *
 * (6) loads core's searchableSelect.js and colorSwatchPicker.js into the same
 * context rather than stubbing them. Stubs would agree with whatever this panel
 * asked for; the real classes are the ones that have to. Both park an element on
 * <body> that outlives the panel's markup, so the release path is checked too --
 * a leak there grows with how much the tool is used, which is exactly the kind
 * of thing no screenshot shows.
 *
 * (7) is here rather than in a renderer probe because it is the seam: core's
 * side of it is pinned by tests/js/cell_color_probe.mjs, and the two only agree
 * if this end emits the same shape.
 *
 * Run directly:
 *   node tests/js/cell_explorer_boot_probe.mjs cellExplorerColors.js ...
 * Exit 0 = every file loaded, the plugin registered and a controller built.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/cell_explorer/static");
const CORE_VIEWS = join(REPO, "plexora/client/src/js/views");
const SCRIPTS = process.argv.slice(2);

/** Core widgets base.html loads for every viewer page, which this panel builds
 *  on rather than growing its own. Loaded first, as the browser does. */
const CORE_WIDGETS = ["searchableSelect.js", "colorSwatchPicker.js"];

/** Panel elements the controller mounts a core widget into. Handed real stand-in
 *  nodes so the mounting actually happens -- returning null for these is how an
 *  earlier version of this probe passed while never constructing either. */
const MOUNTS = ["cell_explorer_variable", "cell_explorer_legend"];

const registered = [];

/** A browser stand-in no wider than what these files touch at load time. */
function browserGlobals() {
    const node = () => ({
        style: { setProperty() {} },
        dataset: {}, hidden: false, value: "",
        innerHTML: "", placeholder: "", type: "", className: "", title: "",
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
        querySelectorAll: () => [], querySelector: () => null,
        remove() {},
        focus() {}, select() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 0, width: 0 }),

        // Children are kept, and clearing textContent drops them, as the real
        // DOM does -- otherwise a container that re-renders keeps handing back
        // the previous render's rows.
        children: [],
        _text: "",
        get textContent() { return this._text; },
        set textContent(value) {
            this._text = value;
            if (!value) this.children.length = 0;
        },
        append(...nodes) { this.children.push(...nodes); },
        appendChild(child) { this.children.push(child); return child; },

        // Handlers are kept rather than dropped, so a probe can fire one. The
        // legend's row click is a real behaviour with a real way to get it
        // wrong: two handlers for one gesture toggle twice, and a guard that
        // is too broad stops the eye working at all.
        handlers: {},
        addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
        removeEventListener() {},
        /** Dispatch a click on this element, optionally as if it bubbled up
         *  from `target` -- which is how the eye inside a row arrives. */
        click(target) {
            (this.handlers.click || []).forEach(
                (fn) => fn({ target: target || this, stopPropagation() {} }));
        },
        /** Enough of closest() to answer "is this the swatch": matches this
         *  element's own class name, which is all the legend asks. */
        closest(selector) {
            const wanted = selector.startsWith(".") ? selector.slice(1) : null;
            return wanted && String(this.className).split(" ").includes(wanted)
                ? this : null;
        },
    });
    const mounts = new Map(MOUNTS.map((id) => [id, node()]));
    // A frame queue this probe drives by hand, so "coalesced into one frame"
    // is something that can be asserted rather than assumed.
    const frames = new Map();
    let nextFrame = 0;
    return {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, RangeError,
        Uint8Array, Uint16Array, Uint32Array, Float32Array, DataView, ArrayBuffer,
        Infinity, NaN, URLSearchParams, parseInt, parseFloat, isNaN, isFinite,
        setTimeout: () => 1, clearTimeout: () => {},
        requestAnimationFrame: (fn) => { nextFrame += 1; frames.set(nextFrame, fn); return nextFrame; },
        cancelAnimationFrame: (id) => { frames.delete(id); },
        __flushFrames: () => {
            const due = [...frames.values()];
            frames.clear();
            due.forEach((fn) => fn());
            return due.length;
        },
        AbortController: class AbortController { constructor() { this.signal = {}; } abort() {} },
        fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
        document: {
            getElementById: (id) => mounts.get(id) || null,
            querySelectorAll: () => [],
            querySelector: () => null,
            createElement: () => node(),
            addEventListener() {}, removeEventListener() {},
            body: node(),
        },
        window: {
            Plexora: { registerPlugin: (definition) => registered.push(definition) },
            PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
            addEventListener() {}, removeEventListener() {},
            // Core's widgets defer their hide through window.setTimeout rather
            // than the bare global.
            setTimeout: () => 1, clearTimeout: () => {},
        },
    };
}

const ctx = createContext(browserGlobals());
const problems = [];
const loaded = [];

for (const name of CORE_WIDGETS) {
    try {
        runInContext(readFileSync(join(CORE_VIEWS, name), "utf8"), ctx, { filename: name });
    } catch (error) {
        problems.push(`core widget ${name} failed to load: ${error.message}`);
    }
}

for (const name of SCRIPTS) {
    try {
        runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
        loaded.push(name);
    } catch (error) {
        problems.push(`${name} failed to load: ${error.message}`);
        break;   // everything after it would fail for the same reason
    }
}

if (!problems.length) {
    // Loading without throwing is not the same as working: a file can define its
    // class fine and still refer to a name that does not exist yet when the
    // class is actually used.
    runInContext(
        "globalThis.__names = { CellExplorerColors: typeof CellExplorerColors,"
        + " CellExplorerApi: typeof CellExplorerApi,"
        + " CellExplorerState: typeof CellExplorerState,"
        + " CellExplorerLegend: typeof CellExplorerLegend,"
        + " CellExplorerContinuous: typeof CellExplorerContinuous,"
        + " CellExplorerSidebarController: typeof CellExplorerSidebarController };",
        ctx);
    for (const [name, kind] of Object.entries(ctx.__names)) {
        if (kind === "undefined") problems.push(`${name} was never defined`);
    }

    if (registered.length !== 1) {
        problems.push(`expected exactly one plugin registration, got ${registered.length}`);
    } else if (registered[0].name !== "cell_explorer") {
        problems.push(`registered under the wrong name: ${registered[0].name}`);
    } else if (registered[0].ownsCellLayer !== true) {
        problems.push("cell_explorer must claim the cell layer -- it is the whole point");
    }

    try {
        runInContext(
            "globalThis.__built = (() => {"
            + " const c = new CellExplorerSidebarController({ url: (p) => p,"
            + "   datasource: 'd', viewer: {}, dataset: { schema: {} }, onCleanup() {} });"
            + " c.setup();"
            + " globalThis.__controller = c;"
            + " return { column: c.state.column, generation: c.state.generation,"
            + "          opacity: c.state.settings.display.opacity,"
            + "          picker: c.variableSelect instanceof SearchableSelect,"
            + "          trigger: c.variableSelect.trigger,"
            + "          menuSearch: c.variableSelect.field !== c.variableSelect.anchor,"
            + "          cacheLimit: CellExplorerApi.CACHE_LIMIT }; })();",
            ctx);
    } catch (error) {
        problems.push(`a controller could not be built: ${error.message}`);
    }

    if (ctx.__built && !ctx.__built.picker) {
        problems.push("Colour-by must be core's SearchableSelect, not a select of its own");
    }
    if (ctx.__built && ctx.__built.trigger !== "button") {
        problems.push("Colour-by shares its line with two other controls, so it has to be "
            + "the button shape rather than a full-width field");
    }
    if (ctx.__built && !ctx.__built.menuSearch) {
        problems.push("the colour-by dropdown must carry its own search field -- a trigger "
            + "that is also the search box gives no sign it can be typed into");
    }

    // A run of changes inside one frame costs one lookup-table rebuild. Dragging
    // a colour in the native picker fires `input` tens of times a second, and
    // each one rebuilds a table with an entry per cell id and re-renders every
    // label tile on screen.
    try {
        runInContext(
            "globalThis.__recolor = (() => {"
            + " let applied = 0;"
            + " const c = new CellExplorerSidebarController({ url: (p) => p,"
            + "   datasource: 'd', dataset: { schema: {} }, onCleanup() {},"
            + "   viewer: { setCellColorLUT() { applied += 1; return true; } } });"
            + " c.setup();"
            + " c.state.descriptors = [{ name: 'phenotype', kind: 'categorical',"
            + "   categories: [{ value: 'Tumor' }] }];"
            + " c.state.column = 'phenotype';"
            + " c.state.data = { kind: 'categorical', ids: new Uint32Array([1, 2]),"
            + "   codes: new Uint16Array([0, 0]) };"
            + " for (let i = 0; i < 25; i += 1) c.recolor();"
            + " const duringDrag = applied;"
            + " const frames = __flushFrames();"
            + " return { duringDrag, afterFrame: applied, frames }; })();",
            ctx);
    } catch (error) {
        problems.push(`the recolour path could not be exercised: ${error.message}`);
    }

    const recolor = ctx.__recolor;
    if (recolor) {
        if (recolor.duringDrag !== 0) {
            problems.push("recolour must wait for a frame -- it repainted "
                + `${recolor.duringDrag} times mid-drag`);
        }
        if (recolor.frames !== 1) {
            problems.push(`25 changes scheduled ${recolor.frames} frames, expected 1`);
        }
        if (recolor.afterFrame !== 1) {
            problems.push(`the frame applied ${recolor.afterFrame} lookup tables, expected 1`);
        }
    }

    // The legend's colours are core's picker too, and every row builds one. They
    // hold a popover on <body> and a pair of document listeners, so a render
    // that does not hand back the previous batch leaks one set per row per
    // keystroke of the category filter.
    try {
        runInContext(
            "globalThis.__legend = (() => {"
            + " const c = globalThis.__controller;"
            + " const descriptor = { name: 'phenotype', n_missing: 2, categories:"
            + "   [{ value: 'Tumor', count: 3 }, { value: 'CD8 T', count: 1 }] };"
            + " c.legend.render(descriptor, { colors: {}, hidden: [] });"
            + " const built = c.legend.pickers.length;"
            + " const core = c.legend.pickers.every((p) => p instanceof ColorSwatchPicker);"
            + " c.legend.render(descriptor, { colors: {}, hidden: [] });"
            + " const afterRerender = c.legend.pickers.length;"
            + " c.destroy();"
            + " return { built, core, afterRerender,"
            + "          afterDestroy: c.legend.pickers.length }; })();",
            ctx);
    } catch (error) {
        problems.push(`the legend could not be rendered: ${error.message}`);
    }

    const legend = ctx.__legend;
    if (legend) {
        // Two categories plus the Unassigned row the missing values earn.
        if (legend.built !== 3) {
            problems.push(`expected a colour picker per row, got ${legend.built}`);
        }
        if (!legend.core) {
            problems.push("a category's colour must be picked with core's ColorSwatchPicker");
        }
        if (legend.afterRerender !== legend.built) {
            problems.push(
                `re-rendering left ${legend.afterRerender} pickers where ${legend.built} `
                + "rows exist -- the previous batch was not released");
        }
        if (legend.afterDestroy !== 0) {
            problems.push("tearing the panel down must release every picker");
        }
    }

    // Hiding a category is the thing people do most in this list, and the eye
    // is a 17-pixel target at the far end of the row. The whole row does it --
    // except the swatch, which means something else.
    try {
        runInContext(
            "globalThis.__rowClick = (() => {"
            + " const c = new CellExplorerSidebarController({ url: (p) => p,"
            + "   datasource: 'd', dataset: { schema: {} }, onCleanup() {},"
            + "   viewer: { setCellColorLUT() { return true; } } });"
            + " c.setup();"
            + " const toggles = [];"
            + " c.legend.handlers.onVisibility = (label, hidden) => toggles.push(hidden);"
            + " c.legend.render({ name: 'phenotype', n_missing: 0,"
            + "   categories: [{ value: 'Tumor', count: 3 }] }, { colors: {}, hidden: [] });"
            + " const row = document.getElementById('cell_explorer_legend').children[0];"
            + " const [swatch, , , eye] = row.children;"
            + " row.click();"
            + " const onBody = toggles.length;"
            + " row.click(eye);"
            + " const onEye = toggles.length;"
            + " row.click(swatch);"
            + " const onSwatch = toggles.length;"
            + " c.destroy();"
            + " return { onBody, onEye, onSwatch,"
            + "          swatchClass: swatch.className }; })();",
            ctx);
    } catch (error) {
        problems.push(`the legend row could not be clicked: ${error.message}`);
    }

    const rowClick = ctx.__rowClick;
    if (rowClick) {
        if (rowClick.swatchClass !== "cex-swatch") {
            problems.push(`the swatch mount is class ${rowClick.swatchClass}, `
                + "which is what the row's guard tests for");
        }
        if (rowClick.onBody !== 1) {
            problems.push(`clicking the row toggled ${rowClick.onBody} times, expected 1`);
        }
        if (rowClick.onEye !== 2) {
            problems.push("the eye must still toggle exactly once -- it carries no "
                + "handler of its own, so its click reaches the row and nothing else");
        }
        if (rowClick.onSwatch !== 2) {
            problems.push("clicking the colour swatch must not hide the category");
        }
    }

    // The seam with core. cell_color_probe.mjs pins how the renderer reads these
    // shapes; this is the end that emits them, and a mismatch is a viewer that
    // draws nothing with nothing to say about why.
    try {
        runInContext(
            "globalThis.__lut = (() => {"
            + " const ids = new Uint32Array([1, 2, 3]);"
            + " const codes = new Uint16Array([0, 1, 65535]);"
            + " const dense = CellExplorerColors.buildLUT({ kind: 'categorical', ids, codes,"
            + "   categories: [{ value: 'Tumor' }, { value: 'CD8 T' }],"
            + "   colors: { Tumor: '#ff0000' }, hidden: new Set(['CD8 T']) });"
            + " const values = new Float32Array([0, 0.5, NaN]);"
            + " const ramp = CellExplorerColors.buildLUT({ kind: 'continuous', ids,"
            + "   values, domain: [0, 1], palette: 'viridis' });"
            + " const sparse = CellExplorerColors.buildLUT({ kind: 'categorical',"
            + "   ids: new Uint32Array([9000000]), codes: new Uint16Array([0]),"
            + "   categories: [{ value: 'Tumor' }], colors: {}, hidden: new Set() });"
            + " const blank = CellExplorerColors.buildLUT({ kind: 'continuous', ids,"
            + "   values, domain: [0, 1], palette: 'viridis', blank: true });"
            + " return {"
            + "   blankIsTable: blank.colors instanceof Uint8Array,"
            + "   blankLength: blank.colors.length,"
            + "   blankDrawsNothing: blank.colors.every((byte) => byte === 0),"
            + "   maxId: dense.maxId,"
            + "   isTypedArray: dense.colors instanceof Uint8Array,"
            + "   length: dense.colors.length,"
            + "   overridden: [dense.colors[4], dense.colors[5], dense.colors[6], dense.colors[7]],"
            + "   hiddenAlpha: dense.colors[11],"
            + "   missingAlpha: dense.colors[15],"
            + "   rampLowAlpha: ramp.colors[7],"
            + "   rampNaNAlpha: ramp.colors[15],"
            + "   sparseIsMap: sparse.map instanceof Map,"
            + "   sparseHasEntry: Boolean(sparse.map && sparse.map.get(9000000)),"
            + "   sparseHasNoColors: sparse.colors === undefined,"
            + " }; })();",
            ctx);
    } catch (error) {
        problems.push(`the lookup table could not be built: ${error.message}`);
    }

    const lut = ctx.__lut;
    if (lut) {
        if (!lut.isTypedArray) problems.push("a dense LUT must be a Uint8Array");
        if (lut.maxId !== 3) problems.push(`dense LUT maxId was ${lut.maxId}, expected 3`);
        if (lut.length !== 16) problems.push(`dense LUT length was ${lut.length}, expected 4*(maxId+1)`);
        if (String(lut.overridden) !== "255,0,0,255") {
            problems.push(`a chosen colour was not used: got ${lut.overridden}`);
        }
        if (lut.hiddenAlpha !== 0) problems.push("a hidden category must be alpha 0");
        if (lut.missingAlpha === 0) {
            problems.push("missing values are drawn as Unassigned by default, not hidden");
        }
        if (lut.rampLowAlpha !== 255) problems.push("a real value must be opaque");
        if (lut.rampNaNAlpha !== 0) problems.push("NaN must be alpha 0, not the bottom of the ramp");
        if (!lut.sparseIsMap) problems.push("a high cell id must produce the sparse form");
        if (!lut.sparseHasEntry) problems.push("the sparse form lost its only entry");
        if (!lut.sparseHasNoColors) problems.push("the sparse form must not also allocate a dense table");
        // Hiding a numeric overlay is a table of the right shape with nothing
        // in it. A null LUT would mean "this plugin is not colouring cells",
        // and core would draw the layer in its own default white.
        if (!lut.blankIsTable || lut.blankLength !== 16) {
            problems.push("a hidden overlay must still be a table of the right shape");
        }
        if (!lut.blankDrawsNothing) {
            problems.push("a hidden overlay must draw nothing at all");
        }
    }
}

const report = {
    order: SCRIPTS,
    loaded,
    registered: registered.map((d) => ({
        name: d.name, ownsCellLayer: d.ownsCellLayer,
        preferredCellMode: d.preferredCellMode || null, hooks: Object.keys(d),
    })),
    provider: (() => {
        if (registered.length !== 1 || !registered[0].createInstance) return null;
        const instance = registered[0].createInstance({});
        return {
            colorCoding: instance.supportsColorCoding(),
            ranges: instance.getColorCodedRanges(),
        };
    })(),
    controller: ctx.__built || null,
    legend: ctx.__legend || null,
    rowClick: ctx.__rowClick || null,
    recolor: ctx.__recolor || null,
    lut: ctx.__lut || null,
    problems,
};

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
