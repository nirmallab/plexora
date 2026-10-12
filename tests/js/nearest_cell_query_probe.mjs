/**
 * The right-click pick carries what is on screen, and nothing else does.
 *
 * On a z-stack whose measured object table holds one row per cell with its
 * plane extent, `getNearestCell` sends the planes on screen (`z0`, `z1`) so
 * the server keeps to the cells on them; on a time series it sends the frame
 * (`t`). A flat project's request is exactly what it always was.
 *
 * Run directly: `node tests/js/nearest_cell_query_probe.mjs`
 * Exit 0 = every case passed.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/services/dataLayer.js"), "utf8");

const requests = [];
const context = createContext({
  URLSearchParams,
  console,
  datasource: "stack",
  plexoraUrl: (path) => `/${path}`,
  PlexoraDataset: { resolveSchema: () => ({ x: "X", y: "Y" }) },
  fetch: async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => ({}) };
  },
});
runInContext(`${source}\nglobalThis.DataLayer = DataLayer;`, context);

const failures = [];
function check(name, ok) {
  if (!ok) failures.push(name);
}

async function ask(setup) {
  const layer = new context.DataLayer({}, []);
  setup(layer);
  requests.length = 0;
  await layer.getNearestCell(12.5, 7);
  return new URL(requests[0], "http://x").searchParams;
}

const flat = await ask(() => {});
check("flat: the same four parameters as ever",
      [...flat.keys()].join(",") === "point_x,point_y,datasource");

const plane = await ask((layer) => { layer.tablePlanes = [4, 4]; });
check("plane: z0 and z1 are the plane", plane.get("z0") === "4" && plane.get("z1") === "4");
check("plane: no frame", !plane.has("t"));

const slab = await ask((layer) => { layer.tablePlanes = [2, 7]; });
check("slab: z0..z1", slab.get("z0") === "2" && slab.get("z1") === "7");

const both = await ask((layer) => {
  layer.tablePlanes = [3, 3];
  layer.tableFrame = { column: "t", t: 2 };
});
check("frame and plane together", both.get("t") === "2" && both.get("z0") === "3");

const volume = await ask((layer) => { layer.tablePlanes = null; });
check("3-D view: no planes", !volume.has("z0") && !volume.has("z1"));

if (failures.length) {
  console.error(`nearest cell query: ${failures.length} failed\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
console.log("nearest cell query: ok");
