/**
 * Does the ROI plugin's client actually come up?
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
 *   3. exactly one plugin registers, under the right name;
 *   4. a controller can be built from a real plugin context -- which is the
 *      first moment any of the constructors actually run.
 *
 * Note what is NOT claimed: that the tuple's ORDER is load-bearing. It is not,
 * for this plugin. These files reference each other from inside methods and
 * constructors, all of which run after toolLoader has awaited every script, so
 * the bindings resolve whatever sequence they arrived in. (gating's descriptor
 * comment implies otherwise about its own files; the same reasoning applies
 * there.) A file left OUT of the tuple is a real failure, and is caught here.
 *
 * The list is passed in by the Python test, which reads it off the descriptor,
 * so this probes what the server will really send rather than a copy that can
 * drift from it.
 *
 * Run directly:
 *   node tests/js/roi_boot_probe.mjs roiApi.js roiGeometry.js ...
 * Exit 0 = every file loaded, the plugin registered and a controller built.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/roi/static");
const SCRIPTS = process.argv.slice(2);

const registered = [];

/** A browser stand-in no wider than what these files touch at load time. */
function browserGlobals() {
    const node = () => ({
        style: {}, dataset: {}, files: [],
        classList: { add() {}, remove() {}, toggle() {} },
        append() {}, appendChild() {}, remove() {}, setAttribute() {},
        addEventListener() {}, click() {},
    });
    return {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Uint8Array, Infinity, URLSearchParams,
        setTimeout: () => 1, clearTimeout: () => {},
        requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
        fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
        Blob: class Blob {},
        URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
        Path2D: class Path2D { moveTo() {} lineTo() {} closePath() {} },
        OpenSeadragon: {
            CanvasOverlayHd: class {
                constructor() { this._canvasdiv = { remove() {} }; }
                clear() {} resize() {} _updateCanvas() {}
            },
        },
        document: {
            getElementById: () => null,
            querySelectorAll: () => [],
            querySelector: () => null,
            createElement: () => node(),
            addEventListener() {},
            body: node(),
        },
        window: {
            Plexora: { registerPlugin: (definition) => registered.push(definition) },
            crypto: { randomUUID: () => "0000-1111" },
            PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
            addEventListener() {}, removeEventListener() {},
        },
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

if (!problems.length) {
    // Loading without throwing is not the same as working: a file can define its
    // class fine and still refer to a name that does not exist yet when the
    // class is actually used.
    runInContext(
        "globalThis.__names = { RoiApi: typeof RoiApi, RoiGeometry: typeof RoiGeometry,"
        + " RoiStore: typeof RoiStore, RoiRenderer: typeof RoiRenderer,"
        + " RoiInteraction: typeof RoiInteraction, RoiSidebarController: typeof RoiSidebarController };",
        ctx);
    for (const [name, kind] of Object.entries(ctx.__names)) {
        if (kind === "undefined") problems.push(`${name} was never defined`);
    }

    if (registered.length !== 1) {
        problems.push(`expected exactly one plugin registration, got ${registered.length}`);
    } else if (registered[0].name !== "roi") {
        problems.push(`registered under the wrong name: ${registered[0].name}`);
    }

    try {
        runInContext(
            "globalThis.__built = (() => {"
            + " const c = new RoiSidebarController({ url: (p) => p, datasource: 'd',"
            + "   config: { width: 100, height: 100 }, viewer: { viewer: null },"
            + "   onCleanup() {} });"
            + " return { tool: c.tools.tool, state: c.tools.state, status: c.store.status,"
            + "          ready: c.tools.ready }; })();",
            ctx);
    } catch (error) {
        problems.push(`a controller could not be built: ${error.message}`);
    }
}

const report = {
    order: SCRIPTS,
    loaded,
    registered: registered.map((d) => ({
        name: d.name, ownsCellLayer: d.ownsCellLayer, hooks: Object.keys(d),
    })),
    controller: ctx.__built || null,
    problems,
};

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
