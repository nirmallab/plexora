/**
 * Import Project's "Save to" change: views/projectImport.js moveTo().
 *
 * Changing the place re-asks the server for the preview (a name taken there is
 * not taken here; the free space is that disk's). Two quick changes put two
 * previews in flight, and they can come back in either order. The one that
 * lands last must not be allowed to win when it was asked first: the preview
 * would show another location's statuses under the chosen one, and the first
 * reply clearing "re-reading" re-enabled Import while the second was still
 * out -- whose late reply then redrew the preview over a running import.
 *
 * moveTo is lifted out of the shipped source and run against stand-ins for
 * what it calls, so a version that drops the ordering fails here.
 *
 * Run directly:  node tests/js/project_import_reread_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/views/projectImport.js"), "utf8");

const OPENS = "    async function moveTo(ui, location) {";
const start = source.indexOf(OPENS);
if (start < 0) throw new Error("moveTo not found in projectImport.js");
const end = source.indexOf("\n    }\n", start);
const body = source.slice(start, end + "\n    }\n".length);

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${JSON.stringify(detail)}` : ""}`);
    if (!condition) failures.push(name);
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

function world() {
    const held = new Map();
    const rendered = [];
    const feet = [];
    const context = createContext({ Promise, Object, Array, String, Error });
    context.held = held;
    context.rendered = rendered;
    context.feet = feet;
    runInContext(`
        var current = null;
        function transport() {
            return { preview: (src, location) => new Promise((resolve, reject) => {
                held.set(location, { resolve, reject });
            }) };
        }
        function choicesFor() { return [["new"], ["replace"]]; }
        function defaultChoice() { return "new"; }
        function renderPreview(ui) { ui.phase = "preview"; rendered.push(ui.preview.location); }
        function paintPreviewFoot(ui) { feet.push(Boolean(ui.rereading)); }
        ${body}
        globalThis.moveTo = moveTo;
        globalThis.setCurrent = (ui) => { current = ui; };
    `, context);
    const ui = {
        phase: "preview", source: { path: "/x/a.plexora" }, location: "lib",
        preview: { location: "lib", projects: [] }, choices: {},
        shell: { setStatus() {} },
    };
    context.setCurrent(ui);
    const answer = (location) => held.get(location).resolve({ location, projects: [{ id: "p", status: "new" }] });
    return { ui, held, rendered, feet, moveTo: (loc) => context.moveTo(ui, loc), answer };
}

{
    const w = world();
    w.moveTo("scratch");
    w.moveTo("lab");
    w.answer("lab");
    await settle();
    w.answer("scratch");
    await settle();
    check("a preview asked for an earlier place, landing last, does not replace the chosen place's",
        w.ui.preview.location === "lab" && w.ui.location === "lab", w.rendered);
}

{
    const w = world();
    w.moveTo("scratch");
    w.moveTo("lab");
    w.answer("scratch");
    await settle();
    check("the earlier reply does not clear re-reading while the later one is still out",
        w.ui.rereading === true && !w.rendered.includes("scratch"), { rereading: w.ui.rereading, rendered: w.rendered });
    w.answer("lab");
    await settle();
    check("...and the later one, when it lands, is the preview shown",
        w.ui.rereading === false && w.ui.preview.location === "lab", w.rendered);
}

{
    const w = world();
    w.moveTo("lab");
    // The import was started meanwhile (by any route): the dialog is running.
    w.ui.phase = "running";
    w.answer("lab");
    await settle();
    check("a preview that lands after an import started does not redraw over it",
        w.ui.phase === "running" && w.rendered.length === 0, w.rendered);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
