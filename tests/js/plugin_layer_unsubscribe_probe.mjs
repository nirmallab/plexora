/**
 * ctx.layers.onLayerChange subscriptions are all released at plugin teardown
 * (main.js pluginLayerApi).
 *
 * The documented contract is "released automatically at teardown". A plugin
 * that subscribes more than once (two panels, or a controller re-bound) must
 * not leave the earlier subscriptions behind: the stack would go on calling a
 * torn-down plugin's handlers on every layer change.
 *
 * pluginLayerApi is lifted out of the shipped main.js and run against a
 * stand-in layer stack.
 *
 * Run directly:  node tests/js/plugin_layer_unsubscribe_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/main.js"), "utf8");
const OPENS = "    function pluginLayerApi(definition, record) {";
const start = source.indexOf(OPENS);
if (start < 0) throw new Error("pluginLayerApi not found in main.js");
const end = source.indexOf("\n    }\n", start);
const body = source.slice(start, end + "\n    }\n".length);

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "ok" : "FAIL"} - ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const subscribers = new Set();
const layerStack = {
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    claim() {},
    layers: () => [],
};
const context = createContext({ Map, Set, Promise, Object, Array });
context.seaDragonViewer = { layerStack, onViewportChange: () => () => {} };
runInContext(`function layerRecord(layer) { return layer; }\n${body}\nglobalThis.pluginLayerApi = pluginLayerApi;`,
             context);

const record = { cleanups: [] };
const api = context.pluginLayerApi({ name: "demo" }, record);
const calls = [];
api.onLayerChange(() => calls.push("first"));
api.onLayerChange(() => calls.push("second"));
check("two subscriptions are both live", subscribers.size === 2, String(subscribers.size));
record.cleanups.forEach((fn) => fn());
check("teardown releases every onLayerChange subscription, not only the last",
      subscribers.size === 0, String(subscribers.size));

const again = { cleanups: [] };
const api2 = context.pluginLayerApi({ name: "demo2" }, again);
const off = api2.onLayerChange(() => {});
off();
check("the unsubscribe a plugin is handed still works on its own", subscribers.size === 0);
again.cleanups.forEach((fn) => fn());
check("...and running it again at teardown is harmless", subscribers.size === 0);

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
