/**
 * Figure Builder's object clipboard belongs to one figure.
 *
 *     node tests/js/figure_clipboard_scope_probe.mjs
 *
 * `FigureClipboard` is a module singleton, and the app router keeps the window
 * (and so the module) across routed pages: copy panels in figure A, open
 * figure B, paste -- and B was sent `add_panel` with A's source ids, which the
 * server refuses. Opening a figure (`FigureCanvas`, through `scope`) now empties
 * a clipboard filled in another one, and keeps one filled in the same.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILE = join(REPO, "plexora/plugins/figure_builder/static/figureContextMenu.js");

const context = createContext({ console, Object, Array, JSON, Math, Number, String,
                                document: { addEventListener() {}, removeEventListener() {} } });
context.window = context;
runInContext(readFileSync(FILE, "utf8"), context, { filename: FILE });
const Clipboard = runInContext("FigureClipboard", context);

const failures = [];
Clipboard.scope("fig-a");
Clipboard.put([{ panel_id: "p1", source_id: "src-a" }], []);
Clipboard.scope("fig-a");
if (!Clipboard.hasContent()) failures.push("reopening the same figure emptied its clipboard");
Clipboard.scope("fig-b");
if (Clipboard.hasContent()) failures.push("figure B can paste figure A's panels");
if (Clipboard.take().panels.length) failures.push("take() in figure B returned figure A's panels");

if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
