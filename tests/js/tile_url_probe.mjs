/**
 * The URL every tile in the application is fetched from.
 *
 * `getTileUrl` is the hottest function in the client -- one call per tile per
 * channel per viewport step -- and it grew a second thing to put in the query
 * string when a tile can come straight from a data node. The HD flag was
 * written as a bare `"?q=hd"`, which is a second `?` the moment anything else
 * is there, so the joining is the part worth pinning.
 *
 * Extracted from the shipped source rather than reimplemented: the point is to
 * measure the function the app actually runs, and a copy here could agree with
 * itself while the viewer fetches nonsense.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const source = await readFile(
    path.join(repoRoot, "plexora/client/src/js/views/viewerManager.js"), "utf8");

/** Slice one top-level function out by brace matching. */
function extractFunction(text, name) {
    const signature = new RegExp(`\\nfunction ${name}\\(`).exec(text);
    if (!signature) throw new Error(`function ${name} not found`);
    const start = signature.index + 1;
    let i = text.indexOf("{", start);
    let depth = 0;
    for (; i < text.length; i += 1) {
        if (text[i] === "{") depth += 1;
        else if (text[i] === "}") {
            depth -= 1;
            if (depth === 0) return text.slice(start, i + 1);
        }
    }
    throw new Error(`no body for ${name}`);
}

const tileQuality = { hd: false };
const context = createContext({ tileQuality });
runInContext(extractFunction(source, "getTileUrl"), context);
runInContext(extractFunction(source, "getTileKey"), context);

/** A stand-in tile source: only the fields getTileUrl reads. */
function tileSource(overrides) {
    return Object.assign({
        tileFormat: 16,
        src: "/generated/data/demo/demo_0/",
        toTileLevels: () => ({ inputTile: { level: 3, x: 4, y: 5 } }),
    }, overrides);
}

function urlFor(overrides) {
    return context.getTileUrl.call(tileSource(overrides), 3, 4, 5);
}

// -- the ordinary project: nothing has changed ---------------------------

tileQuality.hd = false;
assert.equal(
    urlFor({}),
    "/generated/data/demo/demo_0/3/4_5.png",
    "a local tile at default quality must be exactly the URL it always was");

tileQuality.hd = true;
assert.equal(
    urlFor({}),
    "/generated/data/demo/demo_0/3/4_5.png?q=hd",
    "the HD flag must still be the whole query when there is nothing else");

tileQuality.hd = true;
assert.equal(
    urlFor({ tileFormat: 32 }),
    "/generated/data/demo/demo_0/3/4_5.png",
    "a label tile ignores HD, so its URL -- and OSD's URL-keyed cache -- never "
    + "churns when HD is flipped");

// -- a tile straight from a data node ------------------------------------

const NODE_SRC = "http://compute-3:8642/node/v1/image/slide/tile/slide_0/";
const NODE_QUERY = "t=abc123&tw=1024&th=1024";

tileQuality.hd = false;
assert.equal(
    urlFor({ src: NODE_SRC, srcQuery: NODE_QUERY }),
    `${NODE_SRC}3/4_5.png?${NODE_QUERY}`,
    "a node tile carries its token and the project's tile grid");

tileQuality.hd = true;
assert.equal(
    urlFor({ src: NODE_SRC, srcQuery: NODE_QUERY }),
    `${NODE_SRC}3/4_5.png?q=hd&${NODE_QUERY}`,
    "HD and the node's own parameters are joined with '&', not with a second '?'");

// The exact regression the join exists to prevent.
tileQuality.hd = true;
assert.ok(
    !urlFor({ src: NODE_SRC, srcQuery: NODE_QUERY }).includes("?q=hd?"),
    "two '?' in one URL would send the whole node query as part of the value");
assert.equal(
    (urlFor({ src: NODE_SRC, srcQuery: NODE_QUERY }).match(/\?/g) || []).length,
    1,
    "exactly one '?' in any tile URL");

tileQuality.hd = false;
assert.equal(
    urlFor({ src: NODE_SRC, srcQuery: "", tileFormat: 32 }),
    `${NODE_SRC}3/4_5.png`,
    "an empty srcQuery adds nothing -- absent and empty mean the same thing");

// -- a plane or projection of a volume ------------------------------------

tileQuality.hd = false;
assert.equal(
    urlFor({ view: "z12" }),
    "/generated/data/demo/demo_0/3/4_5.png?view=z12",
    "a volume's channel tile names its plane");

tileQuality.hd = true;
assert.equal(
    urlFor({ view: "z4-19max", hd: true }),
    "/generated/data/demo/demo_0/3/4_5.png?q=hd&view=z4-19max",
    "the view joins HD with '&'");

assert.equal(
    urlFor({ view: "z4-19p99.5" }),
    "/generated/data/demo/demo_0/3/4_5.png?q=hd&view=z4-19p99.5",
    "a percentile token passes through unescaped -- it is URL-safe by design");

assert.equal(
    urlFor({ view: "z12", tileFormat: 32, hd: false }),
    "/generated/data/demo/demo_0/3/4_5.png",
    "a 2-D mask is the same picture on every plane, so its tile never names one "
    + "and its cache record is shared by every plane");

tileQuality.hd = false;
assert.equal(
    urlFor({ view: "" }),
    "/generated/data/demo/demo_0/3/4_5.png",
    "a planar image's view is empty and its URL is exactly what it was");

// -- the GPU texture key ---------------------------------------------------

function keyFor(overrides) {
    return context.getTileKey.call(tileSource(Object.assign({ srcIdx: 2 }, overrides)), 3, 4, 5);
}
assert.equal(keyFor({}), "16-2-3-4-5", "a planar key is the key it always was");
assert.notEqual(keyFor({ view: "z12" }), keyFor({ view: "z13" }),
    "plane 13 must never bind plane 12's texture (glInit.js caches on this key)");
assert.equal(keyFor({ view: "z12" }), "16-2-z12-3-4-5");

console.log("tile URL probe OK");
