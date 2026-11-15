/**
 * Audit leads pg-gating-static-5, -6 and -7 (Nov 2026).
 *
 * - static-5: an agent's colour-by and a table change re-read the variable
 *   list only; they used to re-adopt the stored settings, which threw away a
 *   change the save debounce had not written yet.
 * - static-6: a column shown with `persist: false` (carried over, a launch
 *   overlay, an agent, a figure restore) is not saved as the project's
 *   selection by the next unrelated save; one the user picks is.
 * - static-7: a classifier run's poll stops at dispose -- it used to go on
 *   and paint into the reopened panel (same element ids) and layer.
 *
 * The real classes, in a vm context, against a fake API and a bare DOM.
 * Run directly: `node tests/js/audit_leads_3_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CE = join(REPO, "plexora", "plugins", "cell_explorer", "static");
const GATING = join(REPO, "plexora", "plugins", "gating", "static");

const timers = [];
const painted = [];
const sandbox = {
    console, Map, Set, Promise, Number, String, Math, Object, Array, JSON, Error,
    Uint8Array, Int8Array, Float32Array, AbortController,
    document: {
        getElementById: (id) => { painted.push(id); return null; },
        createElement: () => ({}),
        addEventListener: () => {},
        removeEventListener: () => {},
    },
};
sandbox.window = sandbox;
sandbox.setTimeout = (fn) => { timers.push(fn); return timers.length; };
sandbox.clearTimeout = () => {};
sandbox.requestAnimationFrame = () => 1;
sandbox.cancelAnimationFrame = () => {};
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.CSVGatingList = { events: { SELECTION_CHANGED: "selection" } };
createContext(sandbox);
for (const file of [join(CE, "cellExplorerState.js"), join(CE, "cellExplorerAnalysisBridge.js"),
                    join(CE, "cellExplorerSidebarController.js"),
                    join(GATING, "gatingClassifier.js")]) {
    runInContext(readFileSync(file, "utf8"), sandbox, { filename: file });
}
runInContext("globalThis.CellExplorerState = CellExplorerState;"
    + "globalThis.CellExplorerAnalysisBridge = CellExplorerAnalysisBridge;"
    + "globalThis.CellExplorerSidebarController = CellExplorerSidebarController;"
    + "globalThis.GateClassifierControl = GateClassifierControl;", sandbox);

const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

/** A Cell Explorer controller with only its state, its API and no DOM. */
function explorer({ stored = {} } = {}) {
    const saves = [];
    const self = Object.create(sandbox.CellExplorerSidebarController.prototype);
    self.ctx = {};
    self.state = new sandbox.CellExplorerState();
    self.api = {
        state: async () => ({ settings: JSON.parse(JSON.stringify(stored)), revision: 1,
                              ...JSON.parse(JSON.stringify(stored)) }),
        variables: async () => ({ variables: [{ name: "cluster", kind: "categorical" },
                                              { name: "phenotype", kind: "categorical" }] }),
        values: async () => ({ kind: "categorical", categories: [], codes: [] }),
        save: async (revision, settings) => { saves.push(settings); return { revision: 2 }; },
    };
    for (const name of ["render", "applyLUT", "recolor", "restoreOpacity", "restoreMode"]) {
        self[name] = () => {};
    }
    self.state.adopt(stored);
    self.state.descriptors = [{ name: "cluster", kind: "categorical" },
                              { name: "phenotype", kind: "categorical" }];
    const bridge = new sandbox.CellExplorerAnalysisBridge(self);
    return { self, bridge, saves };
}

const passed = [];
async function check(label, fn) {
    timers.length = 0;
    painted.length = 0;
    await fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

await check("an agent's colour-by keeps a change the debounce has not saved yet", async () => {
    const { self, bridge } = explorer({ stored: { selected: "cluster" } });
    self.state.setOpacity(0.3);
    self.scheduleSave();
    assert.equal(self.state.dirty, true);
    await bridge.colorBy("phenotype");
    assert.equal(self.state.settings.display.opacity, 0.3);
    assert.equal(self.state.dirty, true);
});

await check("a table change keeps a change the debounce has not saved yet", async () => {
    const { self, bridge } = explorer({ stored: { selected: "cluster" } });
    self.state.setOpacity(0.2);
    self.scheduleSave();
    await bridge.onChanged({ detail: { plugin: "core", kind: "dataset.changed",
                                       payload: { sections: ["obs:cluster"] } } });
    assert.equal(self.state.settings.display.opacity, 0.2);
    assert.equal(self.state.dirty, true);
});

await check("a column shown with persist:false is not saved as the project's selection",
    async () => {
        const { self, saves } = explorer({ stored: { selected: "cluster" } });
        await self.select("phenotype", { persist: false });
        self.state.setOpacity(0.4);
        self.scheduleSave();
        await self.flushSave();
        assert.equal(saves.length, 1);
        assert.equal(saves[0].selected, "cluster");
    });

await check("a column the user picks is saved as the project's selection", async () => {
    const { self, saves } = explorer({ stored: { selected: "cluster" } });
    await self.select("phenotype");
    await self.flushSave();
    assert.equal(saves.at(-1).selected, "phenotype");
});

await check("a classifier run's poll stops painting once the tool is removed", async () => {
    let asked = 0;
    let refreshed = 0;
    const controller = {
        ctx: { layers: { removeOverlay: () => {} } },
        api: {
            gatingJob: async () => { asked += 1; return { status: "done" }; },
            classifierState: async () => { refreshed += 1; return {}; },
        },
    };
    const self = new sandbox.GateClassifierControl(controller);
    self.mode = "classifier";
    self.marker = "CD8";
    self.job = { id: "j1", marker: "CD8" };
    const following = self.followJob();
    await flush();
    self.dispose();
    painted.length = 0;
    // The poll's wait ends after the tool is gone.
    timers.splice(0).forEach((fn) => fn());
    await following;
    await flush();
    assert.equal(refreshed, 0, "a removed tool asked the server for its state again");
    assert.deepEqual(painted, [], "a removed tool painted into the page");
    assert.equal(asked, 0, "a removed tool polled the job again");
});

console.log(`all checks passed (${passed.length})`);
