/**
 * Does Cell Explorer read a time series' new frame's values when the frame on
 * screen changes?
 *
 * A time series' object table has one row per cell per frame, and the values a
 * column colours by are fetched for one frame (`t=`, CellExplorerApi.values,
 * cached per frame). With a label volume per frame the same label id is a
 * different cell on the next frame, so stepping the Depth card to another frame
 * has to fetch that frame's rows and build the colours from them -- not rebuild
 * the table from the rows of the frame that was showing before.
 *
 * The controller is built from a plugin context whose `view.tableFrame()` and
 * `view.onChange()` are stand-ins this probe drives, and whose `fetch` answers
 * the values route with a payload that depends on the `t=` asked for.
 *
 * Run directly:
 *   node tests/js/cell_explorer_frame_values_probe.mjs cellExplorerColors.js ...
 * Writes a JSON report to stderr; exit 0 = the new frame's values coloured it.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/cell_explorer/static");
const CORE_VIEWS = join(REPO, "plexora/client/src/js/views");
const SCRIPTS = process.argv.slice(2);
const CORE_WIDGETS = ["popoverPortal.js", "searchableSelect.js",
                      "colorSwatchPicker.js", "slider.js", "gradientRange.js"];
const MOUNTS = ["cell_explorer_variable", "cell_explorer_legend",
                "cell_explorer_summary", "cell_explorer_summary_categories",
                "cell_explorer_summary_cells", "cell_explorer_search_row",
                "cell_explorer_search", "cell_explorer_search_toggle",
                "cell_explorer_search_clear"];

/** Each frame's rows for column "phenotype": label id 1 is Tumor (code 0) on
 *  frame 0 and CD8 T (code 1) on frame 1, because per-frame segmentation
 *  numbers each frame's cells afresh. */
const FRAMES = {
    0: [[1, 0], [2, 1]],
    1: [[1, 1], [2, 0]],
};

function categoricalPayload(rows) {
    const buffer = new ArrayBuffer(rows.length * 6);
    const view = new DataView(buffer);
    rows.forEach(([id, code], i) => {
        view.setUint32(i * 6, id, true);
        view.setUint16(i * 6 + 4, code, true);
    });
    return buffer;
}

const fetched = [];

async function fakeFetch(url) {
    const text = String(url);
    const query = new URLSearchParams(text.includes("?") ? text.split("?")[1] : "");
    if (text.includes("api/values")) {
        const t = query.get("t");
        fetched.push({ column: query.get("column"), t });
        const rows = FRAMES[t ?? "0"] || [];
        return {
            ok: true, status: 200,
            headers: { get: (name) => ({ "X-Value-Kind": "categorical",
                                         "X-Cell-Count": String(rows.length) })[name] ?? null },
            arrayBuffer: async () => categoricalPayload(rows),
            json: async () => ({}),
        };
    }
    return { ok: true, status: 200, json: async () => ({}) };
}

function browserGlobals() {
    const node = () => ({
        style: { setProperty() {} },
        dataset: {}, hidden: false, value: "",
        innerHTML: "", placeholder: "", type: "", className: "", title: "",
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
        querySelectorAll: () => [], querySelector: () => null,
        remove() {}, focus() {}, select() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 0, width: 0 }),
        children: [],
        _text: "",
        get textContent() { return this._text; },
        set textContent(value) { this._text = value; if (!value) this.children.length = 0; },
        append(...nodes) { this.children.push(...nodes); },
        appendChild(child) { this.children.push(child); return child; },
        handlers: {},
        addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
        removeEventListener() {},
        closest: () => null,
    });
    const mounts = new Map(MOUNTS.map((id) => [id, node()]));
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
        // A real turn of the host's event loop, so every promise chain the
        // controller started (fetch, arrayBuffer, decode) has settled.
        __settle: () => new Promise((resolve) => setImmediate(resolve)),
        AbortController: class AbortController { constructor() { this.signal = {}; } abort() {} },
        fetch: fakeFetch,
        document: {
            getElementById: (id) => mounts.get(id) || null,
            querySelectorAll: () => [],
            querySelector: () => null,
            createElement: () => node(),
            addEventListener() {}, removeEventListener() {},
            body: node(),
        },
        window: {
            Plexora: { registerPlugin() {} },
            PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
            addEventListener() {}, removeEventListener() {},
            setTimeout: () => 1, clearTimeout: () => {},
        },
    };
}

const ctx = createContext(browserGlobals());
const problems = [];

for (const name of CORE_WIDGETS) {
    runInContext(readFileSync(join(CORE_VIEWS, name), "utf8"), ctx, { filename: name });
}
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

let result = null;
try {
    result = await runInContext(`(async () => {
        const frameBox = { t: 0 };
        const viewHandlers = [];
        const luts = [];
        const c = new CellExplorerSidebarController({
            url: (p) => p, datasource: 'd', dataset: { schema: {} }, onCleanup() {},
            viewer: { setCellColorLUT(owner, lut) { if (lut) luts.push(lut); return true; } },
            view: {
                tableFrame: () => frameBox.t,
                onChange(fn) { viewHandlers.push(fn); return () => {}; },
            },
        });
        c.setup();
        c.state.descriptors = [{ name: 'phenotype', kind: 'categorical', n: 2, n_missing: 0,
            categories: [{ value: 'Tumor', count: 1 }, { value: 'CD8 T', count: 1 }],
            colors: { 'Tumor': '#ff0000', 'CD8 T': '#0000ff' } }];
        const rgb = (lut, id) => lut ? Array.from(lut.colors.slice(id * 4, id * 4 + 3)) : null;

        await c.select('phenotype', { persist: false });
        for (let i = 0; i < 5; i += 1) await __settle();
        __flushFrames();
        const frame0 = { cell1: rgb(luts.at(-1), 1), codes: Array.from(c.state.data.codes) };

        // The Depth card steps to frame 1: core moves the table's frame, then
        // announces the view changed.
        frameBox.t = 1;
        viewHandlers.forEach((fn) => fn({}));
        for (let i = 0; i < 5; i += 1) await __settle();
        __flushFrames();
        for (let i = 0; i < 5; i += 1) await __settle();
        __flushFrames();
        const frame1 = { cell1: rgb(luts.at(-1), 1), codes: Array.from(c.state.data?.codes || []) };
        c.destroy();
        return { handlers: viewHandlers.length, frame0, frame1 };
    })()`, ctx);
} catch (error) {
    problems.push(`the frame change could not be exercised: ${error.stack || error.message}`);
}

if (result) {
    const tAsked = fetched.map((entry) => entry.t);
    if (!tAsked.includes("1")) {
        problems.push(`stepping to frame 1 never asked for frame 1's values: values() was `
            + `asked for t=${JSON.stringify(tAsked)}`);
    }
    if (JSON.stringify(result.frame1.codes) !== JSON.stringify([1, 0])) {
        problems.push(`after stepping to frame 1 the panel still holds frame 0's rows: codes `
            + `${JSON.stringify(result.frame1.codes)}, expected [1,0]`);
    }
    if (JSON.stringify(result.frame1.cell1) !== JSON.stringify([0, 0, 255])) {
        problems.push(`label 1 on frame 1 is coloured ${JSON.stringify(result.frame1.cell1)} `
            + "(the previous frame's Tumor red); frame 1's row for it is CD8 T [0,0,255]");
    }
}

console.error(JSON.stringify({ fetched, result, problems }, null, 2));
process.exit(problems.length ? 1 : 0);
