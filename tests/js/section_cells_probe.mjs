/**
 * A 3-D mask's cells in the XZ / YZ section panes: views/sectionCells.js.
 *
 * What is pinned: the panes draw cells only for a 3-D mask; and a mask a data
 * node could not vouch for when the page opened -- slow to answer, or still
 * converting it, so `/volume/info` said it was not (yet) 3-D -- gets its cells
 * in the panes once the page learns otherwise (`plexora:labels-changed`, from
 * main.js `trackLabelPlanes`), reading the cut already on screen, once.
 *
 * The script is run from source in a vm against stand-ins for the panes, the
 * view state and fetch.
 *
 * Run directly:  node tests/js/section_cells_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/sectionCells.js");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

function boot({ labels3d = true, open = true } = {}) {
    const listeners = new Map();
    const win = {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(fn);
        },
        removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
        dispatchEvent(event) {
            [...(listeners.get(event.type) || [])].forEach((fn) => fn(event));
            return true;
        },
    };
    const fetched = [];
    win.fetch = (url) => {
        fetched.push(String(url));
        return new Promise(() => {});
    };
    const context = createContext({ console, Math, Object, Array, Number, String, Boolean, JSON,
                                    Map, Set, Promise, Error, Uint16Array, Uint32Array, URLSearchParams,
                                    window: win });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "sectionCells.js" });

    const overlays = new Map();
    const sections = {
        enabled: open,
        frame: open ? { cx: 100, cy: 80, x0: 0, x1: 200, y0: 0, y1: 160, scale: 1 } : null,
        addOverlay(id, provider) {
            overlays.set(id, provider);
            return () => overlays.delete(id);
        },
        paintOverlays() {},
    };
    const viewState = { labelsAre3d: () => labels3d, get: () => ({ t: 0 }) };
    const cells = win.PlexoraSectionCells.attach(sections, { imageViewer: {}, viewState,
                                                             datasource: "demo" });
    return {
        cells, overlays, fetched,
        listening: (type) => (listeners.get(type)?.size || 0),
        /** What main.js `trackLabelPlanes` announces when the mask is 3-D. */
        becomes3d() {
            labels3d = true;
            win.dispatchEvent({ type: "plexora:labels-changed", detail: { ndim: 3 } });
        },
        emit: (type, detail) => win.dispatchEvent({ type, detail }),
    };
}

{
    const app = boot({ labels3d: false });
    check("a mask not known to be 3-D draws nothing in the panes",
        app.overlays.size === 0 && app.fetched.length === 0);
    app.becomes3d();
    check("...and its cells the moment it is known to be 3-D",
        app.overlays.has("core:cells"));
    check("...reading the cut already on screen at once, both axes",
        app.fetched.filter((u) => u.includes("section_cells")).length === 2,
        JSON.stringify(app.fetched));
    check("...after which it stops listening for the news",
        app.listening("plexora:labels-changed") === 0);
    app.becomes3d();
    check("...and a second announcement adds nothing",
        app.overlays.size === 1 && app.fetched.length === 2, `${app.fetched.length}`);
}

{
    const app = boot({ labels3d: true, open: false });
    check("a 3-D mask known at load draws in the panes", app.overlays.has("core:cells"));
    check("...reading nothing while the panes are closed", app.fetched.length === 0);
    check("...and waits for no news", app.listening("plexora:labels-changed") === 0);
}

{
    const app = boot({ labels3d: false });
    app.emit("plexora:labels-changed", { ndim: 3 });
    check("news that is not of a 3-D mask leaves the panes alone",
        app.overlays.size === 0 && app.listening("plexora:labels-changed") === 1);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
