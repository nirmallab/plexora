/**
 * The project edit page's Image Scale control (views/projectEdit.js) starts on
 * every visit, routed or loaded.
 *
 * A routed visit (appRouter.js swapping the page in over the viewer) runs no
 * DOMContentLoaded, runs an already-loaded script not at all, and carries the
 * VIEWER's flaskVariables (no `datasetName`). The control must start from the
 * page itself, as a PlexoraPage controller, for the sample the page is of.
 *
 * Run directly:  node tests/js/project_edit_scale_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/projectEdit.js");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "ok" : "FAIL"} - ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const started = [];
const controllers = [];
const nodes = {
    "project-edit": { dataset: { project: JSON.stringify({ name: "tonsil" }) } },
    scale_calibration: { querySelector: () => null },
};
const document = {
    readyState: "complete",
    getElementById: (id) => nodes[id] || null,
    addEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
};
class ScaleCalibration {
    constructor(name, viewer, options) { this.args = { name, viewer, options }; }
    init() { started.push(this.args); }
}
const window = { flaskVariables: { datasource: "tonsil" }, addEventListener() {} };
const context = createContext({
    window, document, console, JSON, ScaleCalibration,
    PlexoraPage: { register: (fn) => controllers.push(fn) },
});
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "projectEdit.js" });

// Every controller the file registered, as pageBoot runs them on a swap; the
// page's main controller needs far more page than this and is not the subject.
const mount = () => controllers.forEach((fn) => { try { fn(); } catch (error) { /* not this one */ } });
mount();
check("a routed visit starts the Image Scale control for the sample the page is of",
      started.length === 1 && started[0].name === "tonsil" && started[0].options.alwaysShow === true,
      JSON.stringify(started));
mount();
check("...and a second routed visit starts it again", started.length === 2);
nodes["project-edit"] = null;
mount();
check("another page starts nothing", started.length === 2);

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
