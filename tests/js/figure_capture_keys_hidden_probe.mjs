/**
 * Figure Builder's bare C and S stand down while a routed page is over the
 * viewer.
 *
 *     node tests/js/figure_capture_keys_hidden_probe.mjs
 *
 * The app router hides the viewer behind a routed page (Settings, the Figure
 * Canvas) and announces it with `plexora:viewer-hidden`. The dock's C and the
 * capture tool's S are bound to the document, so on the Figure Canvas C armed
 * capture on the hidden viewer and S put a capture of it into the bin. The
 * bookmark key, the dataset keys and the channel keys already follow the two
 * events; these now do too.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILE = join(REPO, "plexora/plugins/figure_builder/static/figureCaptureTool.js");

const listeners = {};
const context = createContext({
    console, Object, Array, Math, Number, String, Promise, JSON,
    document: { activeElement: null },
    addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
});
context.window = context;
runInContext(readFileSync(FILE, "utf8"), context, { filename: FILE });
const Tool = runInContext("FigureCaptureTool", context);
const fire = (name) => (listeners[name] || []).forEach((fn) => fn({ type: name }));
const key = (k) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false });

const failures = [];
if (!Tool.isShootKey(key("s"))) failures.push("S is not the shoot key with the viewer shown");
fire("plexora:viewer-hidden");
if (!Tool.standDown()) failures.push("standDown() is false with a routed page over the viewer");
if (Tool.isShootKey(key("s"))) failures.push("S still shoots with the viewer hidden");
fire("plexora:viewer-shown");
if (Tool.standDown()) failures.push("standDown() stays true after the viewer is shown again");

if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
