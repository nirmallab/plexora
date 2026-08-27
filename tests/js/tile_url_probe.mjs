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

console.log("tile URL probe OK");
